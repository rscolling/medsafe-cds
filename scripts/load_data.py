#!/usr/bin/env python3
"""Load synthetic patients into a HAPI FHIR server (transaction bundles with client-assigned ids).

  python scripts/load_data.py --fhir-base http://localhost:8090/fhir            # hand-authored + generated cohort
  python scripts/load_data.py ... --synthea-dir path/to/synthea/output/fhir     # optional real Synthea bundles
  python scripts/load_data.py ... --wait 180                                    # wait for HAPI to come up

Resources are PUT (idempotent), so re-running is safe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.cohort import generate_cohort  # noqa: E402

ORDER = ["Patient", "Condition", "MedicationRequest", "Observation"]


def wait_for(client: httpx.Client, base: str, seconds: int) -> None:
    deadline = time.time() + seconds
    while True:
        try:
            if client.get(f"{base}/metadata", params={"_summary": "true"}).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            raise SystemExit(f"HAPI FHIR not reachable at {base} after {seconds}s")
        time.sleep(3)


def put_bundle(client: httpx.Client, base: str, resources: list[dict]) -> None:  # type: ignore[type-arg]
    entries = [
        {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
        for rt in ORDER
        for r in resources
        if r["resourceType"] == rt
    ]
    r = client.post(base, json={"resourceType": "Bundle", "type": "transaction", "entry": entries})
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fhir-base", default="http://localhost:8090/fhir")
    ap.add_argument("--cohort-size", type=int, default=300)
    ap.add_argument("--synthea-dir", type=Path)
    ap.add_argument("--wait", type=int, default=0)
    args = ap.parse_args()
    base = args.fhir_base.rstrip("/")
    client = httpx.Client(timeout=60)
    if args.wait:
        wait_for(client, base, args.wait)
    docs = [json.loads(p.read_text()) for p in sorted((ROOT / "data/patients/hand-authored").glob("*.json"))]
    docs += generate_cohort(args.cohort_size)
    for i in range(0, len(docs), 25):
        put_bundle(client, base, [r for d in docs[i : i + 25] for r in d["resources"]])
    print(f"loaded {len(docs)} synthetic patients into {base}")
    if args.synthea_dir:
        n = 0
        for f in sorted(args.synthea_dir.glob("*.json")):
            b = json.loads(f.read_text())
            if b.get("resourceType") == "Bundle" and b.get("type") == "transaction":
                client.post(base, json=b).raise_for_status()
                n += 1
        print(f"loaded {n} Synthea bundles (note: Synthea RxNorm/SNOMED coverage in our small tables is partial)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
