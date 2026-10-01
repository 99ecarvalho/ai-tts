"""Piper text-to-speech HTTP service.

Endpoints:
  GET  /health       -> {status, voice, voice_present, loaded}
  POST /synthesize   -> audio/wav stream

Loads the model lazily on the first request (about 1 second to warm up).
Set DEFAULT_VOICE to select a voice (path: /voices/<name>.onnx).
"""
from __future__ import annotations

import io
import logging
import os
import time
import wave
from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response


VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/voices"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "pt_BR-faber-medium")

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger("tts")


_voice_cache: dict[str, "object"] = {}


def _load_voice(name: str):
    if name in _voice_cache:
        return _voice_cache[name]
    from piper.voice import PiperVoice  # type: ignore

    onnx = VOICES_DIR / f"{name}.onnx"
    cfg = VOICES_DIR / f"{name}.onnx.json"
    if not onnx.exists() or not cfg.exists():
        raise RuntimeError(f"voice {name} is not installed in {VOICES_DIR}")
    t0 = time.monotonic()
    voice = PiperVoice.load(str(onnx), config_path=str(cfg))
    log.info("tts.voice_loaded", voice=name, elapsed_sec=round(time.monotonic() - t0, 2))
    _voice_cache[name] = voice
    return voice


app = FastAPI(title="agent-framework tts", version="0.1.0")


@app.get("/health")
async def health():
    onnx = VOICES_DIR / f"{DEFAULT_VOICE}.onnx"
    return {
        "status": "ok" if onnx.exists() else "degraded",
        "voice": DEFAULT_VOICE,
        "voice_present": onnx.exists(),
        "loaded": DEFAULT_VOICE in _voice_cache,
    }


@app.post("/synthesize")
async def synthesize(payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 5000:
        raise HTTPException(status_code=413, detail="text exceeds 5000 characters")
    voice_name = (payload.get("voice") or DEFAULT_VOICE).strip()
    try:
        voice = _load_voice(voice_name)
    except Exception as e:
        log.exception("tts.voice_load_failed", voice=voice_name)
        raise HTTPException(status_code=500, detail=f"voice load: {e}")
    t0 = time.monotonic()
    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wav:
            voice.synthesize(text, wav)
    except Exception as e:
        log.exception("tts.synth_failed", text_len=len(text))
        raise HTTPException(status_code=500, detail=f"synth: {e}")
    audio = buf.getvalue()
    log.info(
        "tts.synth_ok", text_len=len(text), audio_bytes=len(audio),
        elapsed_sec=round(time.monotonic() - t0, 2), voice=voice_name,
    )
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
