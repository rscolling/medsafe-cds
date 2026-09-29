# Disclaimer

**medsafe-cds is a prototype and is not clinical advice.** It is a portfolio demonstration of context-aware
medication-safety alerting. It is not a medical device, has not been validated, reviewed by clinicians, or
cleared by any regulator, and must not be used to make or influence decisions about real patients.

- **Synthetic data only.** Every patient in this repository and in its demos is synthetic: hand-authored
  scenarios, a seeded Synthea-style generator, the public WorldVistA VEHU training database, and ten labeled
  "VistA twin" patients written by the author. No real patient data (PHI) is used or should ever be loaded.
- **Rule content is not authoritative.** Rule thresholds are the author's reading of public drug labels and
  published literature (each card cites its source). They are examples for demonstrating an engine, not
  clinical guidance, and are not affiliated with or derived from any commercial drug-knowledge vendor's content.
- **No real VA/VistA connections.** Connecting to any real VA or other production VistA is out of scope. The
  optional live adapter talks only to a local VEHU container using the public demo credentials published on
  its Docker Hub page. Do not point it at a real system.
- **Comparison numbers are illustrative.** They come from synthetic cohorts whose prevalence was chosen by the
  author and from ten author-labeled scenarios. They do not measure real-world accuracy or alert burden.
- No warranty; see the Apache-2.0 `LICENSE`.
