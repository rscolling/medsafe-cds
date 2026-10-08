# Demo script (about 5 minutes)

> Prototype, not clinical advice. Synthetic data only. Say so at the start.

Start: `make demo` (or `make demo-local`), open the UI.

Without the UI (or as a rehearsal check), `./scripts/demo-calls.sh` sends the raw CDS Hooks requests for the key
cases on both sources (`fhir` and `vista`) to the running API (default `http://localhost:8080`, override with
`MEDSAFE_API`): metformin with eGFR < 30 (critical card: seeded synthetic patient syn-0001 / public VEHU test patient
DFN 100881), metformin with normal eGFR (baseline fires, context suppresses: hand-02 / twin 9000002), metformin with
no renal labs (data-gap info card: hand-03 / twin 9000003), and ibuprofen on top of the LISINOPRIL-HCTZ combination
tablet (triple whammy: hand-11 / twin 9000011). With `jq` installed it prints one line per card and exits 1 if any
case does not return the expected card; `-v` prints each request and response.

## Finding each scenario (the picker shows patients, not scenarios)

The patient dropdown reads like an EHR patient list: **name · sex, age · DOB · ID** (FHIR id, or **DFN** on VistA).
It never shows drug names, labs or scenario labels. Choosing a patient pops up a card with that patient's **current
active medications** from the selected source (Esc or *Close* dismisses it; *View active meds* reopens it). Use this
table to find each demo case. All patients are synthetic; ages in the picker are computed from DOB on the current
date, so they move with the calendar.

| scenario | what it shows | order to place | FHIR patient (pick by name or ID) | VistA patient (pick by name or DFN) |
|---|---|---|---|---|
| A | metformin, eGFR 30-45: fires | metformin 500 | Synthetic, A · F · DOB 1952-03-15 · ID hand-01 | SYNTHETICPATIENT,ATWIN · DFN 9000001 |
| B | metformin, normal eGFR: baseline fires, context suppresses | metformin 500 | Synthetic, B · M · DOB 1968-03-15 · ID hand-02 | SYNTHETICPATIENT,BTWIN · DFN 9000002 |
| C | metformin, no renal labs: data-gap info card | metformin 500 | Synthetic, C · M · DOB 1966-03-15 · ID hand-03 | SYNTHETICPATIENT,CTWIN · DFN 9000003 |
| D | NSAID + ACE inhibitor + loop diuretic, eGFR 40 | ibuprofen 800 | Synthetic, D · M · DOB 1956-03-15 · ID hand-04 | SYNTHETICPATIENT,DTWIN · DFN 9000004 |
| E | NSAID + ARB + thiazide, preserved eGFR | naproxen 500 | Synthetic, E · M · DOB 1960-03-15 · ID hand-05 | SYNTHETICPATIENT,ETWIN · DFN 9000005 |
| F | frail 84-year-old with AF: apixaban dose reduction | apixaban 5 | Synthetic, F · F · DOB 1942-03-15 · ID hand-06 | SYNTHETICPATIENT,FTWIN · DFN 9000006 |
| G | robust 63-year-old with AF: no reduction | apixaban 5 | Synthetic, G · M · DOB 1963-03-15 · ID hand-07 | SYNTHETICPATIENT,GTWIN · DFN 9000007 |
| H | ACE inhibitor + KCl order, potassium/eGFR reassuring: suppressed | potassium chloride 20 MEQ | Synthetic, H · M · DOB 1971-03-15 · ID hand-08 | SYNTHETICPATIENT,HTWIN · DFN 9000008 |
| I | ACE inhibitor + spironolactone order, potassium 5.4: fires | spironolactone | Synthetic, I · F · DOB 1958-03-15 · ID hand-09 | SYNTHETICPATIENT,ITWIN · DFN 9000009 |
| J | lithium + NSAID (control rule): fires in both modes | ibuprofen 800 | Synthetic, J · M · DOB 1981-03-15 · ID hand-10 | SYNTHETICPATIENT,JTWIN · DFN 9000010 |
| K | LISINOPRIL-HCTZ combination tablet + NSAID, eGFR ~40 | ibuprofen 800 | Synthetic, K · F · DOB 1955-03-15 · ID hand-11 | SYNTHETICPATIENT,KTWIN · DFN 9000011 |
| VEHU | public VEHU test patient: DM/HTN/CKD problems, rising creatinine, no current meds (eGFR computed) | metformin 500 | n/a | HYPERTENSION,PATIENT FEMALE · DFN 100881 |
| cohort | seeded Synthea-style patient used by `demo-calls.sh` (eGFR < 30) | metformin 500 | Synthetic-0001 · F · DOB 1974-08-05 · ID syn-0001 | n/a |

The VistA patients 9000001-9000011 are labelled synthetic "twins" of hand-01..hand-11 that exist only as raw RPC
replies (not in VEHU); the API's `/api/patients` still returns the scenario label in a `label` field for scripts.

## Steps

1. **The banner.** Point out the "Prototype, not clinical advice" banner and that all patients are synthetic.
2. **True positive, FHIR.** Source FHIR, patient **Synthetic, A (ID hand-01)**. The active-medications card pops up
   (no current meds); close it. Drug metformin, *Sign order*. Baseline panel: 1 alert. Context panel: 1 card with the
   actual eGFR, the "why" bullets, the DailyMed source link and override reasons.
3. **False alarm removed.** Patient **Synthetic, B (hand-02)**. Baseline shows 1 alert; context shows 0 and a
   "Suppressed by context" note. This is the alert-fatigue point.
4. **Data gap.** Patient **Synthetic, C (hand-03)**. Context shows an info card: no eGFR in 90 days.
   Baseline would have fired blindly; context says what is missing.
5. **The med list behind the rule.** Patient **Synthetic, D (hand-04)**: the pop-up lists lisinopril and furosemide
   with their RxNorm codes. Order ibuprofen: the triple-whammy card fires because of exactly those two meds.
6. **Same rule, other source.** Switch to VistA. Choose **SYNTHETICPATIENT,ATWIN (DFN 9000001)** (labeled synthetic
   overlay) and then the real VEHU test patient **HYPERTENSION,PATIENT FEMALE (DFN 100881)** with metformin: same
   card, eGFR computed by CKD-EPI 2021 from creatinine, labelled "computed". On VistA the pop-up shows the order status
   VistA itself reports (VEHU outpatient orders are `PENDING`) and RxNorm codes mapped from the free-text drug name.
7. **Context suppression with a rule that combines labs.** Patient **Synthetic, H (hand-08)** with potassium
   chloride: potassium and eGFR reassuring, so suppressed; patient **Synthetic, I (hand-09)** with spironolactone
   (potassium 5.4): fires.
8. **Combination tablet, both sources.** Patient **Synthetic, K (hand-11)**, drug ibuprofen: the pop-up shows one
   combination tablet that supplies the ACE inhibitor and the thiazide, so the order completes the NSAID + ACEI/ARB +
   diuretic triple and the warning fires. Switch to VistA, pick **SYNTHETICPATIENT,KTWIN (DFN 9000011)**: same card,
   eGFR computed.
9. **Control rule.** Patient **Synthetic, J (hand-10)** with ibuprofen: fires in both modes; context does not weaken it.
10. **Override and audit.** Override a card with a coded reason; show `/api/audit/summary`.
11. **Numbers.** `make compare`; state the limits (synthetic cohorts, n=14 labels, author-chosen prevalence).
12. **Failure mode.** Stop the VistA source (or set a bad host): the API returns zero cards with an
    `X-Medsafe-Degraded` header instead of blocking the order.

Talking points: why fail open, why data-gap cards, how the adapter interface keeps FHIR and VistA symmetric,
what the vendored-client fix was, what would be needed before any real deployment (clinical governance,
validated content, security review, real integration).
