# One-page brief

> Prototype, not clinical advice. Synthetic data only.

**Problem.** Drug-class alerts are noisy. Clinicians override most of them, and the ones that matter get lost.
The cause is usually missing context: the rule does not know the patient's renal function, potassium, age or
weight, and it cannot tell "no problem" from "no data".

**What this prototype shows.**
1. **Context-aware rules.** Six versioned YAML rules. Baseline mode fires on class match; context mode checks
   labs/problems/age/weight, suppresses when the record shows the risk is absent (example: ACE inhibitor + potassium is
   suppressed when K is 5.0 or less within 30 days and eGFR is 60 or more within 90 days), and raises an
   info-level data-gap card instead of silently passing when the deciding value is missing.
2. **One engine, two data sources.** An interface of three calls (`get_active_meds`, `get_problems`,
   `get_lab_series`) returns FHIR-shaped resources. FHIR R4 (HAPI) and VistA (RPC Broker against the public VEHU
   image) both plug in, and the same scenarios fire the same rule on both.
3. **Explainability and audit.** Each card says why, cites a public source, offers suggestions and coded
   override reasons; the audit trail stores pseudonymised ids and reason codes only.
4. **Operability.** CDS Hooks 2.0, fail-open on data-source trouble, timeouts, per-patient caching, metrics,
   structured logs, rate limiting, CI, containers.

**Numbers (illustrative, synthetic).** On a seeded 300-patient cohort context mode suppresses 63% of baseline
alerts (with data-gap cards counted separately); on ten author-labeled scenarios precision rises from 0.571 to
1.0 with recall unchanged at 1.0. These are mechanism demos, not evidence of clinical performance.

**Integration lessons from VistA.** Free-text medication names; every outpatient order PENDING; no eGFR (compute
it); lab RPC returns one collection per call; a client parsing bug that misread valid replies as errors
(fixed in the vendored fork); a stateful, non-thread-safe broker.

**Not done / next.** Real EHR connections (out of scope); clinician-reviewed rule content and governance;
prospective evaluation with real override/outcome data; more rules with a real mapping service; multi-instance
rate limiting and audit storage; live FHIR-on-VistA.
