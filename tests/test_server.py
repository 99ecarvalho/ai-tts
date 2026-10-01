# Tests for server.py. Run with `pytest` (see requirements-dev.txt).
#
# Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>
# SPDX-License-Identifier: LGPL-3.0-or-later
import io
import struct
import sys
import types
import wave

import pytest
from fastapi.testclient import TestClient

import server


class FakeVoice:
    """Mimics piper.voice.PiperVoice: two "sentences" of 100 silent samples."""

    config = types.SimpleNamespace(sample_rate=22050)

    def synthesize_stream_raw(self, text):
        for _ in range(2):
            yield b"\x00\x00" * 100

    def synthesize(self, text, wav):
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.config.sample_rate)
        for pcm in self.synthesize_stream_raw(text):
            wav.writeframes(pcm)


def _install(voices_dir, name):
    (voices_dir / f"{name}.onnx").write_bytes(b"")
    (voices_dir / f"{name}.onnx.json").write_text("{}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(server, "DEFAULT_VOICE", "default-voice")
    monkeypatch.setattr(server, "_voice_cache", {})
    return TestClient(server.app)


@pytest.fixture
def fake_piper(monkeypatch):
    """Replace piper.voice so _load_voice works without the real package."""
    loads = []

    class PiperVoice:
        @staticmethod
        def load(model_path, config_path=None):
            loads.append(model_path)
            return FakeVoice()

    module = types.ModuleType("piper.voice")
    module.PiperVoice = PiperVoice
    monkeypatch.setitem(sys.modules, "piper", types.ModuleType("piper"))
    monkeypatch.setitem(sys.modules, "piper.voice", module)
    return loads


def test_health_degraded_when_voice_missing(client):
    body = client.get("/health").json()
    assert body == {
        "status": "degraded",
        "voice": "default-voice",
        "voice_present": False,
        "loaded": False,
    }


def test_health_degraded_when_config_missing(client, tmp_path):
    (tmp_path / "default-voice.onnx").write_bytes(b"")
    assert client.get("/health").json()["status"] == "degraded"


def test_health_ok(client, tmp_path):
    _install(tmp_path, "default-voice")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["voice_present"] is True


def test_voices_lists_only_complete_installs(client, tmp_path):
    _install(tmp_path, "b-voice")
    _install(tmp_path, "a-voice")
    (tmp_path / "half-voice.onnx").write_bytes(b"")
    _install(tmp_path, "dotted.voice")  # valid files, but not a requestable name
    assert client.get("/voices").json() == {
        "default": "default-voice",
        "voices": ["a-voice", "b-voice"],
    }


def test_synthesize_returns_wav(client, tmp_path, fake_piper):
    _install(tmp_path, "default-voice")
    resp = client.post("/synthesize", json={"text": "  hello  "})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(resp.content)) as wav:
        assert wav.getnframes() == 200


def test_voice_is_loaded_once(client, tmp_path, fake_piper):
    _install(tmp_path, "default-voice")
    for _ in range(3):
        assert client.post("/synthesize", json={"text": "hi"}).status_code == 200
    assert len(fake_piper) == 1
    assert client.get("/health").json()["loaded"] is True


@pytest.mark.parametrize("payload", [
    {},
    {"text": ""},
    {"text": "   "},
    {"text": 123},
    {"text": "x" * (server.MAX_TEXT_CHARS + 1)},
])
def test_synthesize_rejects_invalid_text(client, payload):
    assert client.post("/synthesize", json=payload).status_code == 422


@pytest.mark.parametrize("voice", ["../etc/passwd", "a/b", "..", "voice.name"])
def test_synthesize_rejects_unsafe_voice_names(client, voice):
    resp = client.post("/synthesize", json={"text": "hi", "voice": voice})
    assert resp.status_code == 422


def test_synthesize_unknown_voice_is_404(client):
    resp = client.post("/synthesize", json={"text": "hi", "voice": "missing"})
    assert resp.status_code == 404


def test_voice_load_error_does_not_leak_details(client, tmp_path, monkeypatch):
    _install(tmp_path, "default-voice")

    class PiperVoice:
        @staticmethod
        def load(model_path, config_path=None):
            raise RuntimeError(f"secret path {model_path}")

    module = types.ModuleType("piper.voice")
    module.PiperVoice = PiperVoice
    monkeypatch.setitem(sys.modules, "piper", types.ModuleType("piper"))
    monkeypatch.setitem(sys.modules, "piper.voice", module)

    resp = client.post("/synthesize", json={"text": "hi"})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "failed to load voice"}


def test_default_response_is_unchanged(client, tmp_path, fake_piper):
    """Legacy clients send only text and expect a complete WAV file."""
    _install(tmp_path, "default-voice")
    resp = client.post("/synthesize", json={"text": "hi"})
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["content-length"] == str(len(resp.content))
    assert resp.headers["cache-control"] == "no-store"
    with wave.open(io.BytesIO(resp.content)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 22050)


def _is_mp3(data):
    return data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def test_synthesize_mp3(client, tmp_path, fake_piper):
    _install(tmp_path, "default-voice")
    resp = client.post("/synthesize", json={"text": "hi", "format": "mp3"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert _is_mp3(resp.content)


def test_stream_wav(client, tmp_path, fake_piper):
    _install(tmp_path, "default-voice")
    resp = client.post("/synthesize", json={"text": "hi", "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert "content-length" not in resp.headers
    assert resp.headers["x-accel-buffering"] == "no"
    data = resp.content
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", data[22:28])
    assert (channels, rate) == (1, 22050)
    assert data[36:40] == b"data"
    assert len(data) == 44 + 2 * 200


def test_stream_mp3(client, tmp_path, fake_piper):
    _install(tmp_path, "default-voice")
    resp = client.post("/synthesize", json={"text": "hi", "format": "mp3", "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert _is_mp3(resp.content)


def test_stream_unknown_voice_is_404(client):
    resp = client.post("/synthesize", json={"text": "hi", "voice": "missing", "stream": True})
    assert resp.status_code == 404


def test_rejects_unknown_format(client):
    assert client.post("/synthesize", json={"text": "hi", "format": "ogg"}).status_code == 422
