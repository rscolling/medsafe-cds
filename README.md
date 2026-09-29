# medsafe-cds

> **Prototype, not clinical advice.** Synthetic data only. No real patient data, no real VA/VistA
> connection, no vendor drug-knowledge content. See [DISCLAIMER.md](DISCLAIMER.md).

A small **CDS Hooks 2.0** service that shows one idea: **one rules engine, two very different data sources
(FHIR R4 and VistA RPC), the same alerts, and fewer false alarms** because rules look at patient context
(labs, problems, age, weight) instead of only matching drug classes.

Example: a naive drug-class rule fires on *every* metformin order. The context-aware rule looks at the newest
eGFR (reported, or computed with CKD-EPI 2021 from creatinine, age and sex) and fires only when it matters,
explains why, cites the public label, and says so explicitly when the data needed to decide is missing
(a *data-gap* info card, not a silent pass).

## Quick start

```bash
make demo            # docker compose if available (HAPI FHIR + API + UI), else local mode without Docker
make demo-local      # no Docker: fixtures + recorded real VEHU replies; UI on :4173, API docs on :8080/docs
docker compose up --build          # HAPI + loader + backend + UI on http://localhost:5173
```

Local setup: `scripts/setup.sh` (needs Python 3.12 via `uv` or `python3.12`, and Node 22 as used in CI and pinned
in `.nvmrc`; Node 20.19+ also works and was used for the local runs reported below). Then:

| command | what it does |
|---|---|
| `make test` | backend unit + contract tests with the 85% coverage gate |
| `make lint` | ruff, ruff format check, `mypy app` (strict), eslint, `tsc` (does not build the UI) |
| `cd frontend && npm run build` | type-check and production build of the UI (also run by CI) |
| `make e2e` | Playwright end-to-end tests against the built UI and a real API on fixtures/recorded data. It runs `npx playwright install chromium` first and **stops if that download fails**. To use an already-installed Chrome/Chromium instead, set `PLAYWRIGHT_CHROME_PATH=/path/to/chrome` (the install step is then skipped). Run `npm run build` first if `frontend/dist` is missing |
| `make compare` | baseline vs context-aware alert counts |
| `make test-integration` | opt-in tests that need the HAPI and/or VEHU containers; skipped when they are down |
| `make security` | pip-audit, bandit, npm audit |

## Security and exposure: do not expose this service

This is a **localhost prototype**. Do not put it on a network, and never load real patient data.

- `docker-compose.yml` binds every port to `127.0.0.1`. `make demo-local` binds the API to localhost by default.
- With no configuration the service logs a loud `INSECURE DEV DEFAULT` warning at startup: `/api/audit`,
  `/api/audit/summary`, `/metrics` and `/docs` are open, and patient ids in logs/audit are pseudonymised with a
  public default salt (trivially reversible).
- Set `MEDSAFE_AUDIT_API_KEY` to protect `/api/audit*`, `/metrics` and the UI helper endpoints `/api/sources`,
  `/api/patients`, `/api/compare` (send it as `X-API-Key`) and to switch off `/docs` and `/openapi.json`. The bundled
  demo UI does not send a key, so it only works keyless on localhost; with a key set, use the CDS Hooks endpoints or
  call the API with the header. `/health` and `/ready` stay open for probes (`/ready` also reports a down data source under `degraded`). Set `MEDSAFE_PSEUDONYM_SALT` (16+
  random characters).
- Set `MEDSAFE_ENV=production` and the app **refuses to start** unless both of the above are set. This enforces
  the two settings; it does not make the prototype production-ready (no TLS, no user authentication on the hook
  endpoints, in-process rate limiting).
- Rate limiting is per client IP, in process. Behind a reverse proxy (including the bundled nginx) every request
  arrives from the proxy's address and **shares one bucket**; set `MEDSAFE_CLIENT_IP_HEADER=X-Forwarded-For` (only if
  your proxy overwrites/appends it; the last entry is used) to get per-client buckets. Use the gateway's limiter for
  anything multi-replica.
- Request bodies are capped at 1 MB (`MEDSAFE_MAX_BODY_BYTES`, 413) and at 50 draft orders per request
  (`MEDSAFE_MAX_DRAFT_ORDERS`, 422). Malformed or hostile payloads fail open (`{"cards": []}` with an
  `X-Medsafe-Degraded` header) or return a 422 that does not echo the request body.
- Logs and the audit table never contain raw patient ids or order text: ids are pseudonymised, error text is
  redacted, free-text order names are logged as a short hash and length only.

## What is in the box

| piece | where |
|---|---|
| Rules (6 versioned YAML rules, each with a public source citation) | `rules/`, [rules/README.md](rules/README.md) |
| Engine: baseline mode (class match only) vs context mode (named checks / suppress predicates) | `backend/app/rules/` |
| One adapter interface `get_active_meds / get_problems / get_lab_series`, FHIR-shaped returns | `backend/app/adapters/base.py` |
| FHIR R4 adapter (HAPI or bundled fixtures) | `backend/app/adapters/fhir/` |
| VistA adapter (RPC Broker, live or recorded) + vendored client | `backend/app/adapters/vista/`, `backend/third_party/vista_clients/` |
| CDS Hooks API, audit trail (SQLite), Prometheus metrics, rate limiting, structured logs | `backend/app/api/`, `audit/`, `metrics.py` |
| React 19 + TypeScript UI with baseline vs context panels, "why" and override capture | `frontend/` |
| Data: 10 hand-authored scenarios, seeded cohort, mapping tables, recorded VEHU replies | `data/` |
| Docs | [docs/architecture.md](docs/architecture.md), [docs/integration-notes.md](docs/integration-notes.md), [docs/brief.md](docs/brief.md), [docs/demo-script.md](docs/demo-script.md) |

CDS Hooks endpoints: `GET /cds-services`, `POST /cds-services/medsafe-order-sign` (context-aware),
`POST /cds-services/medsafe-order-sign-baseline` (comparison only), `POST /cds-services/{id}/feedback`.
The data source is chosen per request with `extension["org.medsafe.source"]` (`fhir` or `vista`); a full
prefetch is also accepted. Operational endpoints: `/health`, `/ready`, `/metrics`, `/api/*`.

## Scope and honesty statements

- **Real VA / production VistA connections are out of scope.** The VistA adapter is built and verified only
  against the public **WorldVistA VEHU** training image on localhost.
- **The VEHU sign-on codes are public demo credentials** published on the image's Docker Hub page for a
  synthetic training database. They are not secrets. The application and the vendored client read them only
  from environment variables (`VISTA_ACCESS_CODE` / `VISTA_VERIFY_CODE`); the vendored client's upstream built-in
  fallback was removed (see `backend/third_party/vista_clients/NOTICE.md`), so no credential is hard-coded in
  code. The public pair appears in `.env.example` (and in test/recorded-capture setup that copies it) and is
  listed at <https://hub.docker.com/r/worldvista/vehu>.
- **Prototype on synthetic data. Not clinical advice.** Rule thresholds are the author's interpretation of
  public labels and literature, with no clinician review.
- **FHIR-on-VistA was evaluated and skipped**: it needs the old `worldvista/vehu:201911-syn-fhir` image
  (another 6+ GB pull, unmaintained). The FHIR side here is HAPI R4 with synthetic patients.
- **Rule matching is deliberately small**: 6 rules, a small RxNorm/LOINC/SNOMED mapping table. Drugs the
  mapper cannot identify (for example free-text VistA names) are counted and reported, never guessed.
- **Non-systemic products are not checked.** A drug name containing any of these words is excluded from systemic
  drug mapping (it would otherwise match its active ingredient and fire, e.g. for a topical NSAID gel or a
  heparin lock flush): `GEL`, `CREAM`, `OINT`, `OINTMENT`, `LOTION`, `TOPICAL`, `OPHTH`, `OPHTHALMIC`, `EYE`,
  `OTIC`, `NASAL`, `SHAMPOO`, `FOAM`, `FLUSH`, `FAB`, `IMMUNE`. A draft order like that gets an explicit
  "check not performed" info card, and a current medication like that is listed under `excluded_meds`, never silently
  ignored. Patches are deliberately *not* excluded (nicotine and other patches are systemic). The list is a
  word-level heuristic (`backend/app/mapping/drugs.py`), not a formulary. A combination product that is only partly
  recognised also gets the info card.

## Configuration (environment variables)

All settings are read once at startup (`backend/app/config.py`); every one has a default. The template is `.env.example`.

| variable | default | meaning |
|---|---|---|
| `MEDSAFE_ROOT` | repo root | directory holding `rules/` and `data/`; set it for a non-editable install (the Docker image does) |
| `MEDSAFE_ENV` | `dev` | `production` refuses to start without `MEDSAFE_AUDIT_API_KEY` and a non-default salt |
| `MEDSAFE_AUDIT_API_KEY` | unset | `X-API-Key` for `/api/audit*`, `/metrics`, `/api/sources`, `/api/patients`, `/api/compare` |
| `MEDSAFE_PSEUDONYM_SALT` | dev default | salt for pseudonymised ids in logs and audit (16+ chars) |
| `MEDSAFE_AUDIT_DB` | `audit.sqlite3` | SQLite file for the audit trail (`:memory:` for tests) |
| `MEDSAFE_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | comma-separated browser origins allowed by CORS |
| `MEDSAFE_LOG_LEVEL` | `INFO` | log level of the structured JSON logs |
| `MEDSAFE_RATE_LIMIT_PER_MINUTE` | `120` | per-client token bucket (`0` disables); see the proxy note above |
| `MEDSAFE_CLIENT_IP_HEADER` | unset | trusted proxy header carrying the client IP (e.g. `X-Forwarded-For`) |
| `MEDSAFE_MAX_BODY_BYTES` / `MEDSAFE_MAX_DRAFT_ORDERS` | `1000000` / `50` | request size and draft-order caps |
| `MEDSAFE_AS_OF_POLICY` | `auto` | `auto` \| `today` \| `anchored` (see "VistA live mode") |
| `MEDSAFE_FHIR_MODE` | `auto` | `http` (HAPI) \| `fixtures` \| `auto` |
| `MEDSAFE_FHIR_BASE_URL` | `http://localhost:8090/fhir` | HAPI FHIR base URL |
| `MEDSAFE_FHIR_TIMEOUT_S` | `3` | HTTP timeout for HAPI calls, seconds |
| `MEDSAFE_VISTA_MODE` | `auto` | `live` \| `recorded` \| `auto` |
| `VISTA_HOST` / `VISTA_PORT` | `localhost` / `9430` | RPC Broker address |
| `VISTA_ACCESS_CODE` / `VISTA_VERIFY_CODE` | unset | sign-on codes (public VEHU demo codes live in `.env.example`) |
| `VISTA_CONTEXT` | `OR CPRS GUI CHART` | broker application context |
| `MEDSAFE_VISTA_TIMEOUT_S` | `8` | socket timeout per broker operation |
| `MEDSAFE_VISTA_CACHE_TTL_S` | `60` | per-patient cache lifetime |
| `MEDSAFE_VISTA_FETCH_DEADLINE_S` | `10` | total time budget for one patient fetch |
| `MEDSAFE_VISTA_AUTH_COOLDOWN_S` | `300` | pause on all sign-ons after a rejected login (lockout protection) |
| `MEDSAFE_VISTA_BREAKER_COOLDOWN_S` | `15` | fail-fast window after a transport failure |
| `MEDSAFE_VISTA_PENDING_ACTIVE` | `true` | count VistA `PENDING` orders as current medications |

## Verification status (what was actually run)

Run on the author's dev box (Debian, Python 3.12.14 via uv, Node 20.19.2) on 2026-09-29:

| check | result |
|---|---|
| backend unit + contract tests (`make test`) | 394 passed, 24 integration tests deselected; coverage 97.06% (gate 85%). Fresh clone at the review-fix commits |
| `ruff check` / `ruff format --check` / `mypy app` (strict) | clean |
| integration tests | **11 passed** against live VEHU (incl. a diff of all 12,837 recorded RPC replies), **13 HAPI tests skipped** because HAPI was not running in this pass (the earlier run with HAPI v7.4.0 up passed 13/13, before the review fixes; not repeated) |
| live VEHU verification | RPCs ORWPT SELECT, ORWPS ACTIVE, ORQQPL LIST, ORWLRR NEWOLD/INTERIMG, ORQQVI VITALS work through the patched client; live output matches the recorded fixtures |
| frontend eslint, tsc, build | clean |
| Playwright e2e (system Chrome) | 7 passed |
| Docker images | **Not re-run after the review fixes.** The backend and frontend images built and the backend image served `/health` and CDS Hooks discovery once, before the QA hardening and review-fix commits; nothing since has been built or run |
| `docker-compose.yml` | parsed and images built with compose v2.29.7; **the full stack was not verified end to end here**: on this sandbox the bridge network could not route container-to-container traffic (the loader could not reach HAPI), so compose is unverified as a running system |
| GitHub Actions workflow | **not run** (no runner); its commands were run locally |

Precise coverage, test counts, and security-scan results are in the final section of
[docs/integration-notes.md](docs/integration-notes.md#verification-log).

## The comparison, and its limits

`make compare` prints alert counts for baseline vs context-aware mode ([docs/results/baseline_comparison.txt](docs/results/baseline_comparison.txt)).
On the seeded 300-patient cohort context mode suppresses 63.1% of baseline alerts (86.8% on the recorded VEHU cohort, where most orders simply find no matching class), and on the ten labeled
scenarios precision goes 0.571 to 1.0 at recall 1.0. **Read those as a demonstration of the mechanism, not a
result**: the cohort's prevalence and lab distributions were chosen by the author, the labels are the
author's judgement on n=10, there is no clinician review and no outcome data. Data-gap info cards are counted
separately from actionable alerts.

## VistA live mode

```bash
# the image is ~6.7 GB; the broker needs ~60-75 s after start
docker run -d -p 127.0.0.1:9430:9430 --name vehu worldvista/vehu   # loopback only: the broker is plaintext
cp .env.example .env            # public demo codes from the Docker Hub page; then:
set -a; . ./.env; set +a
MEDSAFE_VISTA_MODE=live make test-integration      # opt-in tests, skip when the broker is down
make capture-vista                                 # re-record replies into data/vista/recorded
```

Live-mode safety: a rejected sign-on (bad codes, handshake or context error) is **never retried** and suspends all
sign-on attempts for `MEDSAFE_VISTA_AUTH_COOLDOWN_S` (300 s) so a misconfiguration cannot lock the VistA account; a
transport failure opens a circuit for `MEDSAFE_VISTA_BREAKER_COOLDOWN_S` (15 s) so calls fail fast (and fail open) instead
of each waiting for a socket timeout; one patient fetch has a total budget (`MEDSAFE_VISTA_FETCH_DEADLINE_S`, 10 s).
Lab history is paged up to 200 collections; if that cap (or a bad page) is hit the Patient is tagged `labs-truncated`,
logged, and counted in `/api/sources`.

`MEDSAFE_AS_OF_POLICY` (`auto` default | `today` | `anchored`) sets what "now" means for windows such as "eGFR within
90 days": `today` is the wall clock (a 2021 creatinine is then stale and yields a data-gap card, not a critical
alert), `anchored` is the newest observation in the record. `auto` uses `today` for caller-supplied prefetch data and
`anchored` for the bundled fixtures and VEHU (their dates are historical). The policy and date are printed on every
card and returned by `/api/compare`. `MEDSAFE_VISTA_PENDING_ACTIVE` (default true) controls whether VistA `PENDING`
(unreleased) outpatient orders count as current medications.

`MEDSAFE_VISTA_MODE`: `live` (broker only), `recorded` (real replies captured from VEHU, used in CI), or
`auto` (live if reachable and credentials set, else recorded). VEHU has no eGFR results and only two
metformin patients (neither with labs), so the metformin + low-eGFR demo on the VistA side uses ten clearly
labeled synthetic "twin" patients (DFN 9000001-9000010, `vehu-synthetic-overlay`) that exist only as raw RPC
reply text in `data/vista/overlay_patients.json`, plus the real VEHU patient DFN 100881, whose creatinine gives
a computed eGFR. One junk VEHU patient (DFN 100897, 1,430 orders) is excluded.

## Third-party code

- `backend/third_party/vista_clients`: fork of [CivicActions/vista-clients](https://github.com/CivicActions/vista-clients)
  (Apache-2.0) with a documented fix for a reply-parsing bug (see its `NOTICE.md`). `vavista-rpc` (AGPL) is not used.
- Licence: Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
