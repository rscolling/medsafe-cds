# medsafe-cds - PROTOTYPE, NOT CLINICAL ADVICE. Synthetic data only.
PY ?= backend/.venv/bin/python
VENV = backend/.venv
.DEFAULT_GOAL := help

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

setup:           ## create venv (Python 3.12) + install backend and frontend deps
	./scripts/setup.sh

test:            ## backend unit+contract tests with the >=85% coverage gate
	cd backend && .venv/bin/python -m pytest -m "not integration" --cov=app --cov-report=term-missing

test-integration: ## integration tests (skip cleanly if HAPI / VEHU containers are not up)
	cd backend && .venv/bin/python -m pytest tests/integration -rs

lint:            ## ruff, mypy --strict, eslint, tsc
	cd backend && .venv/bin/ruff check . ../scripts && .venv/bin/ruff format --check . ../scripts && .venv/bin/mypy app
	cd frontend && npm run lint && npm run typecheck

e2e:             ## Playwright e2e (starts backend on fixtures/recorded data + built UI)
	cd frontend && npx playwright install chromium >/dev/null 2>&1 || true
	cd frontend && npx playwright test

compare:         ## baseline vs context-aware alert counts (labeled illustrative on synthetic data)
	MEDSAFE_LOG_LEVEL=ERROR $(PY) scripts/baseline_comparison.py

capture-vista:   ## re-record VEHU RPC replies from a live container (needs VISTA_* env)
	$(PY) scripts/capture_vista_fixtures.py

demo:            ## full demo: docker compose if available, else local (fixtures + recorded VEHU)
	./scripts/demo.sh

demo-local:      ## demo without Docker
	./scripts/demo.sh --local

security:        ## dependency + code scanning (pip-audit, bandit, npm audit)
	cd backend && .venv/bin/pip-audit --skip-editable || true
	cd backend && .venv/bin/bandit -q -r app
	cd frontend && npm audit --audit-level=high

.PHONY: help setup test test-integration lint e2e compare capture-vista demo demo-local security
