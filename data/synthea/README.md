# Synthea configuration (optional)

`synthea.properties` configures a real Synthea run (Apache-2.0, https://github.com/synthetichealth/synthea).
The repository does **not** ship Synthea output and the tests never download it. By default the app uses
`backend/app/cohort.py`, a deterministic *Synthea-style* generator (not real Synthea) so the demo and CI are
self-contained. Real Synthea patients load through `scripts/load_data.py --synthea-dir`.
Synthea's drug/lab coding differs from our small mapping tables; unmapped drugs are counted and reported.
