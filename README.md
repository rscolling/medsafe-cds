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

Local setup: `scripts/setup.sh` (needs Python 3.12 via `uv` or `python3.12`, and Node 20+). Then
`make test`, `make lint`, `make compare`, `make test-integration`.

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
  synthetic training database. They are not secrets. They are kept in `.env.example` / environment variables,
  never in code; get them from <https://hub.docker.com/r/worldvista/vehu>.
- **Prototype on synthetic data. Not clinical advice.** Rule thresholds are the author's interpretation of
  public labels and literature, with no clinician review.
- **FHIR-on-VistA was evaluated and skipped**: it needs the old `worldvista/vehu:201911-syn-fhir` image
  (another 6+ GB pull, unmaintained). The FHIR side here is HAPI R4 with synthetic patients.
- **Rule matching is deliberately small**: 6 rules, a small RxNorm/LOINC/SNOMED mapping table. Drugs the
  mapper cannot identify (for example free-text VistA names) are counted and reported, never guessed.

## Verification status (what was actually run)

Run on the author's dev box (Debian, Python 3.12.14 via uv, Node 22) on 2026-09-29:

| check | result |
|---|---|
| backend unit + contract tests (`make test`) | 184 passed, 18 integration tests deselected; coverage 96.94% (gate 85%) |
| `ruff check` / `ruff format --check` / `mypy app` (strict) | clean |
| integration tests with HAPI v7.4.0 and live VEHU up | 18 passed (skip cleanly otherwise) |
| live VEHU verification | RPCs ORWPT SELECT, ORWPS ACTIVE, ORQQPL LIST, ORWLRR NEWOLD/INTERIMG, ORQQVI VITALS work through the patched client; live output matches the recorded fixtures |
| frontend eslint, tsc, build | clean |
| Playwright e2e (system Chrome) | 7 passed |
| Docker images | backend + frontend images build; backend image serves `/health` and CDS Hooks discovery |
| `docker-compose.yml` | parsed and images built with compose v2.29.7; **the full stack was not verified end to end here**: on this sandbox the bridge network could not route container-to-container traffic (the loader could not reach HAPI), so compose is unverified as a running system |
| GitHub Actions workflow | **not run** (no runner); its commands were run locally |

Precise coverage, test counts, and security-scan results are in the final section of
[docs/integration-notes.md](docs/integration-notes.md#verification-log).

## The comparison, and its limits

`make compare` prints alert counts for baseline vs context-aware mode ([docs/results/baseline_comparison.txt](docs/results/baseline_comparison.txt)).
On the seeded 300-patient cohort context mode suppresses 63% of baseline alerts, and on the ten labeled
scenarios precision goes 0.571 to 1.0 at recall 1.0. **Read those as a demonstration of the mechanism, not a
result**: the cohort's prevalence and lab distributions were chosen by the author, the labels are the
author's judgement on n=10, there is no clinician review and no outcome data. Data-gap info cards are counted
separately from actionable alerts.

## VistA live mode

```bash
# the image is ~6.7 GB; the broker needs ~60-75 s after start
docker run -d -p 9430:9430 --name vehu worldvista/vehu
cp .env.example .env            # public demo codes from the Docker Hub page; then:
set -a; . ./.env; set +a
MEDSAFE_VISTA_MODE=live make test-integration      # opt-in tests, skip when the broker is down
make capture-vista                                 # re-record replies into data/vista/recorded
```

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
