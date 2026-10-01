"""Piper text-to-speech HTTP service.

Endpoints:
  GET  /health       -> {status, voice, voice_present, loaded}
  GET  /voices       -> {default, voices}
  POST /synthesize   -> audio/wav (the complete file in the response body)

Loads the model lazily on the first request (about 1 second to warm up),
or at startup when PRELOAD_VOICE=1.
Set DEFAULT_VOICE to select a voice (path: /voices/<name>.onnx).
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field


VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/voices"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "pt_BR-faber-medium")
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "5000"))
PRELOAD_VOICE = os.environ.get("PRELOAD_VOICE", "").lower() in {"1", "true", "yes"}

# Voice names map directly to file names, so only allow characters that
# cannot escape VOICES_DIR (no "/", no "..").
VOICE_NAME_PATTERN = r"^[A-Za-z0-9_-]+$"

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


class VoiceNotFound(Exception):
    pass


_voice_cache: dict[str, "object"] = {}
_voice_lock = threading.Lock()


def _voice_files(name: str) -> tuple[Path, Path]:
    return VOICES_DIR / f"{name}.onnx", VOICES_DIR / f"{name}.onnx.json"


def _voice_installed(name: str) -> bool:
    onnx, cfg = _voice_files(name)
    return onnx.is_file() and cfg.is_file()


def _load_voice(name: str):
    voice = _voice_cache.get(name)
    if voice is not None:
        return voice
    with _voice_lock:
        # Another request may have loaded it while we waited for the lock.
        if name in _voice_cache:
            return _voice_cache[name]
        if not _voice_installed(name):
            raise VoiceNotFound(name)
        from piper.voice import PiperVoice  # type: ignore

        onnx, cfg = _voice_files(name)
        t0 = time.monotonic()
        voice = PiperVoice.load(str(onnx), config_path=str(cfg))
        log.info("tts.voice_loaded", voice=name, elapsed_sec=round(time.monotonic() - t0, 2))
        _voice_cache[name] = voice
        return voice


@asynccontextmanager
async def lifespan(app: FastAPI):
    if PRELOAD_VOICE:
        try:
            await run_in_threadpool(_load_voice, DEFAULT_VOICE)
        except Exception:
            # Keep serving; /health reports the problem.
            log.exception("tts.preload_failed", voice=DEFAULT_VOICE)
    yield


app = FastAPI(title="agent-framework tts", version="0.1.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice: Optional[str] = Field(default=None, pattern=VOICE_NAME_PATTERN)


@app.get("/health")
def health():
    present = _voice_installed(DEFAULT_VOICE)
    return {
        "status": "ok" if present else "degraded",
        "voice": DEFAULT_VOICE,
        "voice_present": present,
        "loaded": DEFAULT_VOICE in _voice_cache,
    }


@app.get("/voices")
def voices():
    installed = sorted(
        p.name.removesuffix(".onnx")
        for p in VOICES_DIR.glob("*.onnx")
        if _voice_installed(p.name.removesuffix(".onnx"))
    )
    return {"default": DEFAULT_VOICE, "voices": installed}


# Plain `def` so FastAPI runs the CPU-bound synthesis in a worker thread
# instead of blocking the event loop.
@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    voice_name = req.voice or DEFAULT_VOICE
    try:
        voice = _load_voice(voice_name)
    except VoiceNotFound:
        raise HTTPException(status_code=404, detail=f"voice '{voice_name}' is not installed")
    except Exception:
        log.exception("tts.voice_load_failed", voice=voice_name)
        raise HTTPException(status_code=500, detail="failed to load voice")
    t0 = time.monotonic()
    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wav:
            voice.synthesize(req.text, wav)
    except Exception:
        log.exception("tts.synth_failed", text_len=len(req.text), voice=voice_name)
        raise HTTPException(status_code=500, detail="speech synthesis failed")
    audio = buf.getvalue()
    log.info(
        "tts.synth_ok", text_len=len(req.text), audio_bytes=len(audio),
        elapsed_sec=round(time.monotonic() - t0, 2), voice=voice_name,
    )
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
