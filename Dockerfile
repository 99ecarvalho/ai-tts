# Voice TTS container — Piper (https://github.com/rhasspy/piper)
# CPU-only, lightweight. The Brazilian Portuguese model is downloaded during the build.
#
# Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>
# SPDX-License-Identifier: LGPL-3.0-or-later
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPER_VOICES_DIR=/voices

# curl downloads the voice at build time and runs the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Default voice: Brazilian Portuguese female (faber-medium, ~63 MB). Set
# DEFAULT_VOICE to select another voice already available in the voices directory.
# Downloads are pinned to a piper-voices commit and verified by SHA-256.
ARG PIPER_VOICES_REVISION=c10ece1aade47bb51c153c893d14e5bf8e5b7117
ARG VOICE_URL=https://huggingface.co/rhasspy/piper-voices/resolve/${PIPER_VOICES_REVISION}/pt/pt_BR/faber/medium
RUN mkdir -p ${PIPER_VOICES_DIR} \
    && cd ${PIPER_VOICES_DIR} \
    && curl -fsSL -o pt_BR-faber-medium.onnx ${VOICE_URL}/pt_BR-faber-medium.onnx \
    && curl -fsSL -o pt_BR-faber-medium.onnx.json ${VOICE_URL}/pt_BR-faber-medium.onnx.json \
    && printf '%s\n' \
        "858555e3a064209c57088fe6bd70c4c3dc54d03eaa00c45d5ecaf43a33f95aa7  pt_BR-faber-medium.onnx" \
        "7e694de195ae3fc36dd732c445eb04fb49b649854893cb5506b978f0d50a1d6f  pt_BR-faber-medium.onnx.json" \
        | sha256sum -c -

COPY server.py .

RUN useradd --system --uid 10001 --create-home app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health | grep -q '"status":"ok"' || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
