#!/usr/bin/env bash
# Create the Python 3.12 venv and install everything. Requires uv (https://docs.astral.sh/uv/) or python3.12.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null; then
  uv venv --python 3.12 backend/.venv
  uv pip install --python backend/.venv/bin/python -e "backend[dev]"
else
  python3.12 -m venv backend/.venv
  backend/.venv/bin/pip install -e "backend[dev]"
fi
(cd frontend && npm ci --no-audit --no-fund)
[ -f .env ] || cp .env.example .env
echo "setup complete"
