"""Piper text-to-speech HTTP service.

Endpoints:
  GET  /health       -> {status, voice, voice_present, loaded}
  GET  /voices       -> {default, voices}
  POST /synthesize   -> audio/wav (default) or audio/mpeg

/synthesize takes {"text", "voice"?, "format"?, "stream"?}. Without
"format" and "stream" it returns the complete WAV file, as it always has.
"format": "mp3" returns MP3; "stream": true sends audio sentence by
sentence as it is synthesized.

Loads the model lazily on the first request (about 1 second to warm up),
or at startup when PRELOAD_VOICE=1.
Set DEFAULT_VOICE to select a voice (path: /voices/<name>.onnx).

Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>
SPDX-License-Identifier: LGPL-3.0-or-later
"""
from __future__ import annotations

import io
import logging
import os
import struct
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator, Literal, Optional

import lameenc
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/voices"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "pt_BR-faber-medium")
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "5000"))
PRELOAD_VOICE = os.environ.get("PRELOAD_VOICE", "").lower() in {"1", "true", "yes"}
MP3_BITRATE = int(os.environ.get("MP3_BITRATE", "64"))  # kbit/s

# Voice names map directly to file names, so only allow characters that
# cannot escape VOICES_DIR (no "/", no "..").
VOICE_NAME_PATTERN = r"^[A-Za-z0-9_-]+$"

MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg"}

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


def _mp3_encoder(sample_rate: int) -> lameenc.Encoder:
    enc = lameenc.Encoder()
    enc.set_bit_rate(MP3_BITRATE)
    enc.set_in_sample_rate(sample_rate)
    enc.set_channels(1)
    enc.set_quality(2)
    return enc


def _streaming_wav_header(sample_rate: int) -> bytes:
    """WAV header for 16-bit mono PCM of unknown length.

    The RIFF and data sizes are set to 0xFFFFFFFF, the usual marker for a
    stream; players read until the connection closes.
    """
    byte_rate = sample_rate * 2
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if PRELOAD_VOICE:
        try:
            await run_in_threadpool(_load_voice, DEFAULT_VOICE)
        except Exception:
            # Keep serving; /health reports the problem.
            log.exception("tts.preload_failed", voice=DEFAULT_VOICE)
    yield


app = FastAPI(title="agent-framework tts", version="0.2.0", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice: Optional[str] = Field(default=None, pattern=VOICE_NAME_PATTERN)
    format: Literal["wav", "mp3"] = "wav"
    stream: bool = False


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


def _synthesize_full(voice, text: str, fmt: str) -> bytes:
    if fmt == "mp3":
        enc = _mp3_encoder(voice.config.sample_rate)
        mp3 = enc.encode(b"".join(voice.synthesize_stream_raw(text)))
        return bytes(mp3 + enc.flush())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        voice.synthesize(text, wav)
    return buf.getvalue()


def _synthesize_stream(voice, text: str, fmt: str, voice_name: str) -> Iterator[bytes]:
    # Starlette iterates sync generators in a worker thread.
    t0 = time.monotonic()
    sent = 0
    sample_rate = voice.config.sample_rate
    enc = _mp3_encoder(sample_rate) if fmt == "mp3" else None
    try:
        if enc is None:
            header = _streaming_wav_header(sample_rate)
            sent += len(header)
            yield header
        for pcm in voice.synthesize_stream_raw(text):
            chunk = bytes(enc.encode(pcm)) if enc else pcm
            if chunk:
                sent += len(chunk)
                yield chunk
        if enc:
            tail = bytes(enc.flush())
            sent += len(tail)
            yield tail
    except Exception:
        # The status line is already sent; re-raising aborts the connection
        # so the client sees a truncated response, not a complete one.
        log.exception("tts.synth_failed", text_len=len(text), voice=voice_name,
                      format=fmt, stream=True)
        raise
    log.info(
        "tts.synth_ok", text_len=len(text), audio_bytes=sent,
        elapsed_sec=round(time.monotonic() - t0, 2), voice=voice_name,
        format=fmt, stream=True,
    )


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
    media_type = MEDIA_TYPES[req.format]
    if req.stream:
        return StreamingResponse(
            _synthesize_stream(voice, req.text, req.format, voice_name),
            media_type=media_type,
            # X-Accel-Buffering stops nginx from buffering the stream.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    t0 = time.monotonic()
    try:
        audio = _synthesize_full(voice, req.text, req.format)
    except Exception:
        log.exception("tts.synth_failed", text_len=len(req.text), voice=voice_name,
                      format=req.format, stream=False)
        raise HTTPException(status_code=500, detail="speech synthesis failed")
    log.info(
        "tts.synth_ok", text_len=len(req.text), audio_bytes=len(audio),
        elapsed_sec=round(time.monotonic() - t0, 2), voice=voice_name,
        format=req.format, stream=False,
    )
    return Response(
        content=audio,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )
