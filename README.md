# ai-tts

[![License: LGPL v3+](https://img.shields.io/badge/license-LGPL--3.0--or--later-blue.svg)](COPYING.LESSER)

A small, self-hosted text-to-speech HTTP service built on
[Piper](https://github.com/rhasspy/piper). Send it text and get back speech
as WAV or MP3, either as one complete file or streamed sentence by sentence
while it is still being synthesized. It runs on the CPU in a single Docker
container and ships with a Brazilian Portuguese voice.

Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>

## Quickstart

You need Docker and curl. `./run.sh test` and `./run.sh say` also need
Python 3.11 or newer.

```bash
git clone https://github.com/99ecarvalho/ai-tts.git
cd ai-tts
./run.sh all                      # run the tests, build, start and smoke-test everything
./run.sh say "Olá, mundo!"        # writes out/say.wav
./run.sh stop                     # stop and remove the container
```

Or step by step:

```bash
./run.sh build                    # build the Docker image
./run.sh start                    # start the service on http://127.0.0.1:8000
./run.sh status                   # container state and health
./run.sh smoke                    # call every endpoint, save audio in out/
./run.sh say "Olá, mundo!" -f mp3 -s -o hello.mp3   # streamed MP3 into hello.mp3
./run.sh logs                     # follow the logs
./run.sh stop
```

Without the script:

```bash
docker build -t ai-tts .
docker run -d --name ai-tts -p 8000:8000 ai-tts
curl -o hello.wav -H 'Content-Type: application/json' \
     -d '{"text": "Olá, mundo!"}' http://127.0.0.1:8000/synthesize
```

`./run.sh help` lists every command and option.

## Contents

- [Features](#features)
- [API](#api)
- [Configuration](#configuration)
- [Using other voices](#using-other-voices)
- [Deployment notes](#deployment-notes)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Simple HTTP API:** one endpoint for synthesis, plus health and voice listing.
- **WAV or MP3:** 16-bit mono WAV by default, or MP3 at a configurable bit rate.
- **Streaming:** with `"stream": true` the audio is sent one sentence at a
  time, so playback can start after the first sentence instead of after the
  whole text.
- **Backwards compatible:** a request with only `text` gets exactly what it
  always got, a complete WAV file.
- **Multiple voices:** any installed Piper voice can be chosen per request.
- **Lightweight:** CPU-only. A voice loads in about a second and stays in memory.
- **Container-ready:** runs as a non-root user, has a Docker `HEALTHCHECK`,
  writes JSON logs, and the voice download is pinned and checksum-verified.

## API

FastAPI also serves interactive documentation at `/docs` and the OpenAPI
schema at `/openapi.json`.

### `POST /synthesize`

Request body (JSON):

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `text` | string | required | Text to speak. Leading and trailing whitespace is removed; it must not be empty and may be up to `MAX_TEXT_CHARS` (5000) characters. |
| `voice` | string | `DEFAULT_VOICE` | Voice name, such as `pt_BR-faber-medium`. Letters, digits, `_` and `-` only. |
| `format` | `"wav"` or `"mp3"` | `"wav"` | Audio format. |
| `stream` | boolean | `false` | Send the audio as it is synthesized instead of as one complete file. |

Responses:

| Status | When | Body |
| ------ | ---- | ---- |
| 200 | Success | `audio/wav` or `audio/mpeg` |
| 404 | The voice is not installed | `{"detail": "voice '<name>' is not installed"}` |
| 422 | Invalid request, for example empty or too-long text, a bad voice name, or an unknown format | FastAPI validation error |
| 500 | The voice failed to load or synthesis failed; details are in the server log | `{"detail": "..."}` |

Audio is 16-bit mono at the voice's sample rate (22,050 Hz for the built-in
voice). Every response has `Cache-Control: no-store`.

Examples:

```bash
# Complete WAV file (the default)
curl -o hello.wav -H 'Content-Type: application/json' \
     -d '{"text": "Olá, mundo!"}' http://127.0.0.1:8000/synthesize

# Complete MP3 file
curl -o hello.mp3 -H 'Content-Type: application/json' \
     -d '{"text": "Olá, mundo!", "format": "mp3"}' http://127.0.0.1:8000/synthesize

# Stream MP3 straight into a player
curl -sN -H 'Content-Type: application/json' \
     -d '{"text": "Primeira frase. Segunda frase.", "format": "mp3", "stream": true}' \
     http://127.0.0.1:8000/synthesize | ffplay -nodisp -autoexit -
```

#### Streaming

With `"stream": true` the response uses chunked transfer encoding and has no
`Content-Length`. Piper synthesizes one sentence at a time, and each one is
sent as soon as it is ready.

- **MP3** streams naturally. This is the best choice for playing audio while
  it arrives.
- **WAV** starts with a 44-byte header whose length fields are set to
  `0xFFFFFFFF`, the usual marker for a stream of unknown length, followed by
  raw PCM. Most players and libraries accept this. Some tools warn about the
  length when you open a saved file.
- The status code is sent before synthesis starts, so an error in the middle
  of a stream cannot become a 500. Instead the server closes the connection
  early and the client sees a truncated response.
- Errors that happen before any audio is produced, such as an unknown voice
  or invalid input, still return 404 or 422.

The responses include `X-Accel-Buffering: no` so that nginx passes chunks on
immediately.

### `GET /health`

```json
{"status": "ok", "voice": "pt_BR-faber-medium", "voice_present": true, "loaded": true}
```

- `status` is `"ok"` when the default voice's files are installed and
  `"degraded"` otherwise.
- `voice_present` says whether both the `.onnx` and `.onnx.json` files exist.
- `loaded` says whether the default voice is already in memory.

The Docker `HEALTHCHECK` marks the container unhealthy unless `status` is `"ok"`.

### `GET /voices`

```json
{"default": "pt_BR-faber-medium", "voices": ["pt_BR-faber-medium"]}
```

Lists every voice in the voices directory that has both its `.onnx` and
`.onnx.json` files.

## Configuration

The service reads these environment variables:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DEFAULT_VOICE` | `pt_BR-faber-medium` | Voice used when a request has no `voice` |
| `PIPER_VOICES_DIR` | `/voices` | Directory with the `<name>.onnx` and `<name>.onnx.json` files |
| `PRELOAD_VOICE` | off | `1`, `true` or `yes` loads the default voice at startup instead of on the first request |
| `MAX_TEXT_CHARS` | `5000` | Longest accepted text |
| `MP3_BITRATE` | `64` | MP3 bit rate in kbit/s |

With `run.sh`, set them in the environment of `./run.sh start`; for example,
`PRELOAD_VOICE=1 ./run.sh restart`. With Docker, pass them with `-e`.

## Using other voices

Piper has voices for many languages at
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices). Each
voice is a pair of files, `<name>.onnx` and `<name>.onnx.json`.

To use your own set, put the files in a directory and mount it over `/voices`.
The mount replaces the built-in voice, so include the default voice too, or
change `DEFAULT_VOICE`:

```bash
VOICES_DIR=./voices DEFAULT_VOICE=en_US-lessac-medium ./run.sh restart
# or
docker run -d -p 8000:8000 -v "$PWD/voices:/voices:ro" \
       -e DEFAULT_VOICE=en_US-lessac-medium ai-tts
```

Then choose a voice per request with `"voice": "<name>"`.

To bake a different default voice into the image, change the download step in
the [Dockerfile](Dockerfile), including the SHA-256 checksums.

## Deployment notes

- The service has no authentication. Run it on a private network, or put it
  behind a reverse proxy that handles authentication and TLS.
- Synthesis is CPU-bound and runs in FastAPI's worker thread pool, so
  `/health` keeps answering while long texts are being synthesized.
- Voices stay in memory once loaded, so memory use grows with each distinct
  voice requested.
- The voice download is pinned to a piper-voices commit and checked against
  SHA-256 checksums, so rebuilding the image gives the same model.

## Development

```bash
./run.sh test          # creates .venv, installs requirements-dev.txt, runs pytest
```

The tests use a fake voice, so they don't need Piper or a model download. To
run them by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

| File | Purpose |
| ---- | ------- |
| `server.py` | The FastAPI service |
| `Dockerfile` | Container image, including the pinned voice download |
| `run.sh` | Build, run, test and try out the service |
| `tests/` | pytest suite |
| `requirements.txt` | Runtime dependencies |
| `requirements-dev.txt` | Test dependencies |

## Contributing

Bug reports, documentation fixes and code are welcome. Report bugs and
suggest features in the [issue tracker](https://github.com/99ecarvalho/ai-tts/issues),
and please open an issue to discuss larger changes first. Make sure `./run.sh test` passes before
opening a pull request, and if you change the API, update this README.

## License

Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>

ai-tts is free software: you can redistribute it and/or modify it under the
terms of the **GNU Lesser General Public License, version 3 or (at your
option) any later version**. The license text is in
[COPYING.LESSER](COPYING.LESSER); it supplements the GNU General Public
License v3, included as [COPYING](COPYING).

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.

Audio you generate with the service is yours; the license covers the
software, not its output.

### Third-party components

The Docker image includes software and data with their own licenses:

| Component | License |
| --------- | ------- |
| [Piper](https://github.com/rhasspy/piper) (`piper-tts` 1.2.0) | MIT |
| [lameenc](https://github.com/chrisstaite/lameenc), which bundles the LAME MP3 encoder | LGPL-3.0-or-later |
| [FastAPI](https://github.com/fastapi/fastapi), [Pydantic](https://github.com/pydantic/pydantic), [Uvicorn](https://github.com/encode/uvicorn) | MIT / BSD-3-Clause |
| [structlog](https://github.com/hynek/structlog) | MIT or Apache-2.0 |
| `pt_BR-faber-medium` voice from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/pt/pt_BR/faber/medium) | Trained on a CC0 dataset, according to its [model card](https://huggingface.co/rhasspy/piper-voices/blob/main/pt/pt_BR/faber/medium/MODEL_CARD) |

If you use other voices, check each voice's model card for its license.
