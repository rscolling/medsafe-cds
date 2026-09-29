# Demo script (about 5 minutes)

> Prototype, not clinical advice. Synthetic data only. Say so at the start.

Start: `make demo` (or `make demo-local`), open the UI.

1. **The banner.** Point out the "Prototype, not clinical advice" banner and that all patients are synthetic.
2. **True positive, FHIR.** Source FHIR, patient "A. Metformin, eGFR 30-45", drug metformin, *Sign order*.
   Baseline panel: 1 alert. Context panel: 1 card with the actual eGFR, the "why" bullets, the DailyMed source
   link and override reasons.
3. **False alarm removed.** Patient "B. Metformin, normal eGFR". Baseline shows 1 alert; context shows 0 and a
   "Suppressed by context" note. This is the alert-fatigue point.
4. **Data gap.** Patient "C. Metformin, no renal labs". Context shows an info card: no eGFR in 90 days.
   Baseline would have fired blindly; context says what is missing.
5. **Same rule, other source.** Switch to VistA. Choose twin "A. Metformin..." (DFN 9000001, labeled synthetic
   overlay) and then real VEHU patient DFN 100881 with metformin: same card, eGFR computed by CKD-EPI 2021
   from creatinine, labelled "computed".
6. **Context suppression with a rule that combines labs.** Patient "H. ACEI + KCl": potassium and eGFR
   reassuring, so suppressed; patient "I. ACEI + spironolactone, K 5.4": fires.
7. **Control rule.** Patient "J. Lithium + NSAID": fires in both modes; context does not weaken it.
8. **Override and audit.** Override a card with a coded reason; show `/api/audit/summary`.
9. **Numbers.** `make compare`; state the limits (synthetic cohorts, n=10 labels, author-chosen prevalence).
10. **Failure mode.** Stop the VistA source (or set a bad host): the API returns zero cards with an
    `X-Medsafe-Degraded` header instead of blocking the order.

Talking points: why fail open, why data-gap cards, how the adapter interface keeps FHIR and VistA symmetric,
what the vendored-client fix was, what would be needed before any real deployment (clinical governance,
validated content, security review, real integration).
