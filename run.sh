#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python || ! -f web/dist/index.html ]]; then
  echo 'Install first: uv sync --frozen; npm --prefix web ci; npm --prefix web run build' >&2
  exit 1
fi
exec .venv/bin/python -m app start "$@"
