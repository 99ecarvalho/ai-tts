#!/usr/bin/env bash
# Build, run, test and try out the Piper TTS service.
# Run ./run.sh help for the commands.
#
# Copyright (c) 2026 Eduardo Correia <ecorreia@apliant.com.br>
# SPDX-License-Identifier: LGPL-3.0-or-later
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-ai-tts}"
CONTAINER="${CONTAINER:-ai-tts}"
PORT="${PORT:-8000}"
OUT_DIR="${OUT_DIR:-out}"
VENV="${VENV:-.venv}"
URL="http://127.0.0.1:${PORT}"

usage() {
    cat <<EOF
Usage: ./run.sh <command> [arguments]

Build, run, test and try out the Piper TTS service.

Commands:
  all                  Run the tests, build the image, start the container
                       and run the smoke test
  build                Build the Docker image
  start                Start the container and wait until it is healthy
  stop                 Stop and remove the container
  restart              stop, then start
  status               Show the container state and the /health response
  logs                 Follow the container logs
  test                 Run the unit tests in a local virtualenv (${VENV})
  smoke                Call every endpoint of the running service and save
                       the audio files in ${OUT_DIR}/
  say TEXT [OPTIONS]   Synthesize TEXT into a file
      -f, --format FMT   wav (default) or mp3
      -v, --voice NAME   voice to use (default: the server's DEFAULT_VOICE)
      -s, --stream       request a streamed response
      -o, --output FILE  output file (default: ${OUT_DIR}/say.<format>)
  clean                Stop the container, remove the image and ${OUT_DIR}/
  help                 Show this help

Environment variables (current value in brackets):
  IMAGE                Docker image name [${IMAGE}]
  CONTAINER            Docker container name [${CONTAINER}]
  PORT                 Host port for the service [${PORT}]
  OUT_DIR              Where smoke and say write audio files [${OUT_DIR}]
  VENV                 Virtualenv used by the test command [${VENV}]
  VOICES_DIR           Host directory with extra voices, mounted read-only
                       over /voices. It replaces the built-in voice, so it
                       must contain DEFAULT_VOICE too.
  DEFAULT_VOICE        Passed to the container (default: pt_BR-faber-medium)
  PRELOAD_VOICE        Passed to the container; 1 loads the voice at startup
  MAX_TEXT_CHARS       Passed to the container (default: 5000)
  MP3_BITRATE          Passed to the container, in kbit/s (default: 64)

Examples:
  ./run.sh all
  ./run.sh say "Olá, mundo!"
  ./run.sh say "Olá, mundo!" -f mp3 -s -o hello.mp3
  PORT=9000 PRELOAD_VOICE=1 ./run.sh restart
EOF
}

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"
}

container_exists() {
    [ -n "$(docker ps -aq --filter "name=^${CONTAINER}\$")" ]
}

cmd_build() {
    need docker
    info "Building image ${IMAGE}"
    docker build -t "${IMAGE}" .
}

cmd_start() {
    need docker
    need curl
    if container_exists; then
        die "container ${CONTAINER} already exists; use ./run.sh restart"
    fi
    if [ -z "$(docker images -q "${IMAGE}")" ]; then
        cmd_build
    fi

    local args=(-d --name "${CONTAINER}" -p "${PORT}:8000")
    local var
    for var in DEFAULT_VOICE PRELOAD_VOICE MAX_TEXT_CHARS MP3_BITRATE; do
        if [ -n "${!var:-}" ]; then
            args+=(-e "${var}=${!var}")
        fi
    done
    if [ -n "${VOICES_DIR:-}" ]; then
        [ -d "${VOICES_DIR}" ] || die "VOICES_DIR ${VOICES_DIR} is not a directory"
        args+=(-v "$(realpath "${VOICES_DIR}"):/voices:ro")
    fi

    info "Starting container ${CONTAINER} on ${URL}"
    docker run "${args[@]}" "${IMAGE}" >/dev/null

    local _
    for _ in $(seq 60); do
        if curl -fs "${URL}/health" >/dev/null 2>&1; then
            info "Service is up: $(curl -s "${URL}/health")"
            return
        fi
        sleep 1
    done
    docker logs --tail 20 "${CONTAINER}" >&2 || true
    die "service did not answer on ${URL}/health within 60 seconds"
}

cmd_stop() {
    need docker
    if container_exists; then
        info "Removing container ${CONTAINER}"
        docker rm -f "${CONTAINER}" >/dev/null
    else
        info "Container ${CONTAINER} is not running"
    fi
}

cmd_status() {
    need docker
    if ! container_exists; then
        echo "Container ${CONTAINER}: not created"
        return
    fi
    docker ps -a --filter "name=^${CONTAINER}\$" \
        --format 'Container {{.Names}}: {{.Status}} ({{.Ports}})'
    echo "Health: $(curl -s "${URL}/health" || echo 'no response')"
}

cmd_logs() {
    need docker
    docker logs -f "${CONTAINER}"
}

cmd_test() {
    need python3
    if [ ! -x "${VENV}/bin/python" ]; then
        info "Creating virtualenv ${VENV}"
        python3 -m venv "${VENV}"
    fi
    info "Installing test dependencies"
    "${VENV}/bin/pip" install -q -r requirements-dev.txt
    info "Running tests"
    "${VENV}/bin/pytest" -q
}

# post FILE JSON: POST JSON to /synthesize, save the body to FILE, print a summary.
post() {
    local file="$1" json="$2"
    curl -fsS -o "${file}" -H 'Content-Type: application/json' -d "${json}" \
        -w "%{http_code} %{content_type} %{size_download} bytes in %{time_total}s (first byte %{time_starttransfer}s)" \
        "${URL}/synthesize"
}

cmd_smoke() {
    need curl
    mkdir -p "${OUT_DIR}"
    local text='Olá! Este é um teste do serviço de voz. Ele transforma texto em fala.'

    info "GET /health"
    curl -fsS "${URL}/health"; echo
    info "GET /voices"
    curl -fsS "${URL}/voices"; echo

    local fmt stream
    for fmt in wav mp3; do
        for stream in false true; do
            local file="${OUT_DIR}/smoke.${fmt}"
            [ "${stream}" = true ] && file="${OUT_DIR}/smoke-stream.${fmt}"
            info "POST /synthesize format=${fmt} stream=${stream} -> ${file}"
            post "${file}" "{\"text\": \"${text}\", \"format\": \"${fmt}\", \"stream\": ${stream}}"
            echo
        done
    done

    info "POST /synthesize with an unknown voice (expect 404)"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
        -d '{"text": "hi", "voice": "does-not-exist"}' "${URL}/synthesize")
    [ "${code}" = 404 ] || die "expected 404, got ${code}"
    echo "404 as expected"

    info "Smoke test passed. Audio files are in ${OUT_DIR}/"
}

# json_string TEXT: TEXT as a JSON string literal.
json_string() {
    python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

cmd_say() {
    need curl
    need python3
    [ $# -ge 1 ] || die "usage: ./run.sh say TEXT [-f wav|mp3] [-v VOICE] [-s] [-o FILE]"
    local text="$1"; shift
    local format=wav voice="" stream=false output=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--format) format="${2:?missing value for $1}"; shift 2 ;;
            -v|--voice) voice="${2:?missing value for $1}"; shift 2 ;;
            -s|--stream) stream=true; shift ;;
            -o|--output) output="${2:?missing value for $1}"; shift 2 ;;
            *) die "unknown option for say: $1" ;;
        esac
    done
    if [ -z "${output}" ]; then
        mkdir -p "${OUT_DIR}"
        output="${OUT_DIR}/say.${format}"
    fi

    local json text_json
    text_json=$(json_string "${text}")
    json="{\"text\": ${text_json}, \"format\": \"${format}\", \"stream\": ${stream}"
    [ -n "${voice}" ] && json+=", \"voice\": $(json_string "${voice}")"
    json+="}"

    info "Writing ${output}"
    post "${output}" "${json}"
    echo
}

cmd_clean() {
    cmd_stop
    if [ -n "$(docker images -q "${IMAGE}")" ]; then
        info "Removing image ${IMAGE}"
        docker rmi "${IMAGE}" >/dev/null
    fi
    rm -rf "${OUT_DIR}"
}

cmd_all() {
    cmd_test
    cmd_build
    cmd_stop
    cmd_start
    cmd_smoke
}

main() {
    local cmd="${1:-help}"
    [ $# -gt 0 ] && shift
    case "${cmd}" in
        all) cmd_all ;;
        build) cmd_build ;;
        start) cmd_start ;;
        stop) cmd_stop ;;
        restart) cmd_stop; cmd_start ;;
        status) cmd_status ;;
        logs) cmd_logs ;;
        test) cmd_test ;;
        smoke) cmd_smoke ;;
        say) cmd_say "$@" ;;
        clean) cmd_clean ;;
        help|-h|--help) usage ;;
        *) usage >&2; echo >&2; die "unknown command: ${cmd}" ;;
    esac
}

main "$@"
