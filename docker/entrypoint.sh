#!/bin/sh
set -eu
python -m live_recorder.migrate
exec "$@"
