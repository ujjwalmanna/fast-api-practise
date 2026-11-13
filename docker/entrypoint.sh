#!/bin/sh
# Container entrypoint: migrate, then serve.
#
# `set -e` matters here. Without it a failed migration is ignored and the API
# starts against a schema it does not match - which fails later, at request
# time, with a confusing error instead of a clear startup failure.
set -eu

echo "[entrypoint] Applying database migrations..."
alembic upgrade head

echo "[entrypoint] Starting: $*"
# `exec` replaces the shell so the server becomes PID 1 and receives SIGTERM
# directly. Without it, the shell swallows the signal and the container is
# SIGKILLed after the grace period, dropping in-flight requests.
exec "$@"
