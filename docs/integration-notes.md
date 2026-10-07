# Integration notes

> Prototype, not clinical advice. Synthetic data only. Facts below were observed against the public VEHU
> image (2026-09-29) unless marked otherwise.

## VistA (RPC Broker)

**Sequence** (all through the vendored `vista_clients.rpc`): TCP connect handshake, `XUS SIGNON SETUP`,
`XUS AV CODE` (access;verify), `XWB CREATE CONTEXT` = `OR CPRS GUI CHART`. Credentials come from
`VISTA_ACCESS_CODE` / `VISTA_VERIFY_CODE`.

| RPC | params | notes |
|---|---|---|
| `ORWPT LIST ALL` | from, direction | `DFN^NAME`, paged; ~860 patients in VEHU |
| `ORWPT SELECT` | DFN | `NAME^SEX^DOB(FileMan)^...`; the SSN piece is replaced by the placeholder `000000000` in recorded fixtures |
| `ORWPS ACTIVE` | DFN, "0", "1", "0" | header `~TYPE^ORDERID^NAME^...^STATUS`, then name and `\ Sig:` lines. Types OP, NV, UD, IV, CP |
| `ORQQPL LIST` | DFN, "A" | `IEN^Problem text (SCT code)^A^ICD^onset...`; `^No problems found.` when empty |
| `ORWLRR NEWOLD` | DFN | `newestFM^oldestFM`, empty = no labs |
| `ORWLRR INTERIMG` | DFN, FMdate, "1", "1" | returns **one collection per call**; page backwards using the previous header's date |
| `ORQQVI VITALS` | DFN | weight for the apixaban/rivaroxaban rules |

### Quirks that shaped the design

- **VistA errors arrive as data (fixed in the fork).** `ORWPT SELECT "1;2"` returns `\x18M  ERROR=SELECT+14^ORWPT...`
  and a missing RPC returns `=Remote Procedure '...' doesn't exist on the server.`: a one-byte length prefix (a control
  character or `=`/`>`) in front of the message. The fork strips that byte before the error match so both raise `RPCError`
  and cannot look like an empty medication or lab list. Tests replay the real bytes; live tests re-check them on VEHU.
- **Lab specimen.** `ORWLRR INTERIMG` returns urine and serum creatinine under the same test name. VEHU has urine
  creatinine results (for example DFN 100881, 2015-07-06) that used to give an eGFR of 0.1. Only serum/plasma results with
  the creatinine test IEN (173) are used, values outside 0.1-25 mg/dL are dropped, and unknown or missing units are
  dropped rather than assumed. Verified live: DFN 100881 as of 2015-07-06 now has creatinine 2.6 mg/dL, eGFR 18.4.
- **Paging.** Real patients have up to ~150 lab collections (DFN 100000: 147), one INTERIMG call each. The cap is 200;
  paging compares FileMan values numerically and stops with a `labs-truncated` tag if a page fails to advance or the cap
  is reached. `ORWLRR NEWOLD` disagrees with the INTERIMG chain head for a few patients (2 of 110 recorded, e.g. DFN 737);
  the chain is authoritative, the disagreement is logged at INFO and counted (`lab_head_disagreements` in `/ready`), and a NEWOLD reply that is neither `^` nor a FileMan
  pair is an error, not "no labs".
- **`parse_response` bug (fixed in the fork).** Upstream treats any reply whose first byte is below 0x20 as
  an error length prefix, so a legitimate data reply such as `"\r\nNo Data Found"` raised a truncated
  `RPCError`. The fork records whether the `\x00\x00` success prefix was present and, when it was, always
  treats the payload as data. A regression test replays the real bytes; an opt-in live test asks VEHU for the
  reply that used to break. No private `_transport` workaround is used.
- **Statuses.** Every VEHU outpatient order is `PENDING` (never released); only non-VA (NV) entries are
  `ACTIVE`. "Current medication" therefore means ACTIVE, PENDING or HOLD.
- **No eGFR anywhere** in VEHU; only creatinine. eGFR is computed with CKD-EPI 2021 (age and sex from
  `ORWPT SELECT`) and labelled `source="computed"`. A reported eGFR always wins.
- **Dates are 2009-2016.** Rule windows (30/90 days) are relative to `as_of`, which defaults to the newest
  observation in the patient's own record, never to today.
- **Metformin patients (DFN 100151, 100157) have no labs**, so on native VEHU data the rule yields a data-gap
  card. The best lab patient (DFN 100881, creatinine 2.1, computed eGFR about 24) has no medications. The
  end-to-end metformin + low-eGFR demo on the VistA side therefore uses the labeled synthetic twin patients
  (DFN 9000001-9000010, in `data/vista/overlay_patients.json`, not in VEHU). The twin overlay is stored as raw
  RPC reply text and goes through exactly the same parser as real VEHU output. A test asserts the same rule
  fires for VEHU 100881 (with a draft metformin order) and for twin 9000001.
- **Junk patient DFN 100897** holds 1,430 orders (a drug-list test record) and is excluded.
- **Free-text drug names.** `ORWPS ACTIVE` names are free text (`METFORMIN HCL 500MG TAB`); the mapper
  matches ingredient names and known salts. Unmapped names are counted, not guessed.
- **Not thread-safe.** One broker connection = one socket = one conversation. One lock per client, timeouts on every
  socket operation, circuit breaker (see architecture). `ORWPT SELECT` drift comparison uses only name/sex/DOB.

### Recorded fixtures

`backend/tests/fixtures/vehu_real_replies.json` and `data/vista/recorded/` hold **12,837 real replies for 110
VEHU patients** (re-captured after the lab paging cap was raised; the original 3,275 are a subset with identical bytes) captured with `scripts/capture_vista_fixtures.py` so CI does not need the 6.7 GB container.
`test_vista_live.py` (opt-in) diffs live output against **all** of them (about 45 s; `MEDSAFE_DRIFT_SAMPLE=n` for a quick subset).

### FHIR-on-VistA

Skipped deliberately. The `worldvista/fhir-on-vista` layer needs the 2019 `vehu:201911-syn-fhir` image and
the current `vehu:latest` exposes nothing on port 9080. A second 6+ GB pull on this box's slow storage was
not justified; the RPC adapter covers the same ground.

## FHIR

- HAPI FHIR `hapiproject/hapi:v7.4.0`, R4, in-memory H2. `scripts/load_data.py` PUTs transaction bundles
  (client-assigned ids, idempotent): 10 hand-authored patients and the seeded cohort. Real Synthea output can
  be loaded with `--synthea-dir`; **Synthea is optional** (`data/synthea/`), and the repo ships no Synthea output.
- Mapping (`data/mapping/*.csv`): RxNorm ingredient and clinical-drug codes to ingredient class; LOINC
  (creatinine 2160-0, eGFR 62238-1 / 98979-8 / 33914-3, potassium 2823-3, weight 29463-7); SNOMED + ICD-9
  prefixes for conditions (atrial fibrillation, CKD, diabetes ...); VistA lab test IENs (creatinine 173).
- The FHIR adapter reads `MedicationRequest` (active, on-hold), `Condition`, `Observation`; the VistA adapter
  emits the same shapes, which is what makes both sources testable with one set of rule scenarios.

## Citations

Each rule carries its public source: DailyMed labels (metformin, XARELTO, lisinopril, lithium by set id), the
FDA ELIQUIS label PDF, BMJ 2013;346:e8525 (NSAID + RAAS + diuretic), and NEJM 2021 CKD-EPI (PubMed 34554658).
`fda.gov` pages were avoided because they refuse automated fetches; the text used was checked in the DailyMed
SPL or label PDF. Thresholds are author interpretations.

## Verification log

Fresh `git clone` into /tmp, `scripts/setup.sh`, on 2026-09-29 (Python 3.12.14, Node 22):

- `make test`: 184 passed, 18 integration tests deselected, coverage 96.94% (gate 85%).
- `make lint`: ruff check + `ruff format --check` clean, `mypy app` (strict) clean, eslint (0 warnings) and tsc clean.
- `npm run build` OK. Playwright e2e: 7 passed (system Chrome).
- Live VEHU integration tests (broker up, HAPI down): 5 passed, 13 HAPI tests skipped. In the original checkout with
  HAPI v7.4.0 and VEHU both up: 18 passed.
- Security scans: `pip-audit` no known vulnerabilities (found pytest 8.4.2 advisory, fixed by requiring pytest>=9.0.3);
  `bandit -r app` no issues after one dynamic-SQL column allow-list and three justified `nosec`;
  `npm audit --audit-level=high` 0 vulnerabilities.
- Not verified: GitHub Actions workflow (not executed), full `docker compose up` stack (the sandbox bridge network
  dropped container-to-container traffic, so the loader could not reach HAPI; images build and the backend image
  serves /health and /cds-services when run with host networking).

### After the code review (`REVIEW.md`, HEAD 2d93dca), fresh clone, 2026-09-29

- `make test`: 394 passed, 24 integration tests deselected, coverage 97.06% (gate 85%).
- `make lint` clean (ruff, format check, `mypy app` strict, eslint, tsc). `npm run build` OK.
- Playwright e2e: 7 passed (system Chrome). `make security`: pip-audit no known vulnerabilities, bandit clean, npm audit 0.
- `make compare`: identical to `docs/results/baseline_comparison.txt` (cohort 63.1% suppression; hand-authored precision
  0.571 to 1.0; recorded VEHU 86.8%, data-gap 35). VEHU moved from 86.5% because urine creatinine no longer yields eGFR 0.1.
- Live VEHU integration (`MEDSAFE_VISTA_MODE=live`): 11 passed including the full 12,837-call drift diff; 13 HAPI tests
  skipped (HAPI not up).
- Still not verified: `docker compose up` end to end, rebuilt Docker images, and the GitHub Actions workflow.
