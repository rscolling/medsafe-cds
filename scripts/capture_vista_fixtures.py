#!/usr/bin/env python3
"""Capture REAL replies from a live VEHU container into data/vista/recorded (offline fixtures).

Requires: `docker run -d -p 9430:9430 worldvista/vehu` (public image, synthetic data) and
VISTA_ACCESS_CODE / VISTA_VERIFY_CODE set (public demo codes from the image's Docker Hub page).
Only the RPC replies the adapter needs are stored. The (fake) SSN piece of ORWPT SELECT is blanked.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "third_party"))

from app.adapters.vista.adapter import VistaAdapter  # noqa: E402
from app.adapters.vista.rpc import BrokerRpcClient, RecordingRpcClient  # noqa: E402


def main() -> None:
    out = ROOT / "data" / "vista" / "recorded"
    out.mkdir(parents=True, exist_ok=True)
    demo = json.loads((ROOT / "data" / "vista" / "demo_patients.json").read_text())
    client = RecordingRpcClient(
        BrokerRpcClient(
            os.environ.get("VISTA_HOST", "localhost"),
            int(os.environ.get("VISTA_PORT", "9430")),
            os.environ["VISTA_ACCESS_CODE"],
            os.environ["VISTA_VERIFY_CODE"],
            "OR CPRS GUI CHART",
            30.0,
        )
    )
    adapter = VistaAdapter(client, mode="live", demo_patients=demo, cache_ttl_s=0)
    ok = 0
    for p in demo:
        try:
            adapter.get_patient(p["dfn"])
            adapter.get_active_meds(p["dfn"])
            adapter.get_lab_series(p["dfn"])
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("skip", p["dfn"], exc)
    for entry in client.recorded.values():
        if entry["rpc"] == "ORWPT SELECT":
            parts = str(entry["reply"]).split("^")
            if len(parts) > 3:
                parts[3] = "000000000"  # blank SSN (fake in VEHU, but never republish)
            entry["reply"] = "^".join(parts)
    (out / "index.json").write_text(json.dumps(dict(sorted(client.recorded.items())), indent=0, sort_keys=True) + "\n")
    print(f"captured {len(client.recorded)} RPC replies for {ok}/{len(demo)} patients -> {out}")
    client.close()


if __name__ == "__main__":
    main()
