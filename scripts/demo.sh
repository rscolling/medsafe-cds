#!/usr/bin/env bash
# One-command demo. PROTOTYPE, NOT CLINICAL ADVICE. Synthetic data only.
#   ./scripts/demo.sh           docker compose if available (HAPI + backend + UI), else local mode
#   ./scripts/demo.sh --local   no Docker: FastAPI (fixtures + recorded real VEHU replies) + built UI
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || cp .env.example .env   # public VEHU demo codes only

if [ "${1:-}" != "--local" ] && docker compose version >/dev/null 2>&1; then
  echo ">> docker compose up (HAPI FHIR + backend + UI). Add '--profile vehu' for live VistA."
  exec docker compose up --build
fi

[ "${1:-}" = "--local" ] || echo ">> docker compose not available: running local demo (FHIR fixtures + recorded VEHU replies)"
[ -x backend/.venv/bin/python ] || ./scripts/setup.sh
export MEDSAFE_FHIR_MODE="${MEDSAFE_FHIR_MODE:-fixtures}" MEDSAFE_VISTA_MODE="${MEDSAFE_VISTA_MODE:-auto}"
set -a; . ./.env; set +a
echo ">> comparison on synthetic cohorts:"
MEDSAFE_LOG_LEVEL=ERROR backend/.venv/bin/python scripts/baseline_comparison.py | sed -n '1,12p'
(cd frontend && VITE_BACKEND_URL=http://localhost:8080 npm run build >/dev/null)
backend/.venv/bin/python -m uvicorn app.api.app:app_factory --factory --app-dir backend --port 8080 &
BACK=$!
trap 'kill $BACK 2>/dev/null || true' EXIT
echo ">> UI: http://localhost:4173   API: http://localhost:8080/docs"
cd frontend && VITE_BACKEND_URL=http://localhost:8080 npm run preview -- --host 127.0.0.1
