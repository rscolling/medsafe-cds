#!/usr/bin/env python3
"""Generate the hand-authored labeled test patients (FHIR JSON) and their VistA-format twins.

Everything here is synthetic. Outputs (committed, so nothing needs regenerating to run tests):
  data/patients/hand-authored/<id>.json    FHIR resources + labeled scenarios
  data/vista/overlay_patients.json         the same people expressed as raw VistA RPC replies

Each scenario is labeled with the expected alerts in each mode AND `clinically_warranted`, the
label used for the illustrative precision numbers (author judgement, not a clinical gold standard).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.clinical.egfr import ckd_epi_2021  # noqa: E402

RX = "http://www.nlm.nih.gov/research/umls/rxnorm"
SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
REC = date(2026, 9, 1)  # "record date" = date of the newest observation (as_of is anchored to it)

# rxcui, display, VistA-style free-text name
DRUGS = {
    "metformin": ("861007", "metformin hydrochloride 500 MG Oral Tablet", "METFORMIN HCL 500MG TAB", 500),
    "ibuprofen": ("197807", "ibuprofen 800 MG Oral Tablet", "IBUPROFEN 800MG TAB", 800),
    "naproxen": ("198014", "naproxen 500 MG Oral Tablet", "NAPROXEN 500MG TAB", 500),
    "lisinopril": ("314076", "lisinopril 10 MG Oral Tablet", "LISINOPRIL 10MG TAB", 10),
    "losartan": ("979492", "losartan potassium 50 MG Oral Tablet", "LOSARTAN 50MG TAB", 50),
    "furosemide": ("310429", "furosemide 20 MG Oral Tablet", "FUROSEMIDE 20MG TAB", 20),
    "hctz": ("310798", "hydrochlorothiazide 25 MG Oral Tablet", "HYDROCHLOROTHIAZIDE 25MG TAB", 25),
    "apixaban5": ("1364445", "apixaban 5 MG Oral Tablet", "APIXABAN 5MG TAB", 5),
    "apixaban2.5": ("1364435", "apixaban 2.5 MG Oral Tablet", "APIXABAN 2.5MG TAB", 2.5),
    "rivaroxaban20": ("1232086", "rivaroxaban 20 MG Oral Tablet", "RIVAROXABAN 20MG TAB", 20),
    "kcl": ("1801294", "potassium chloride 20 MEQ Extended Release Oral Tablet", "POTASSIUM CHLORIDE 20MEQ SA TAB", None),
    "spironolactone": ("9997", "spironolactone", "SPIRONOLACTONE 25MG TAB", 25),
    "lithium": ("42351", "lithium carbonate", "LITHIUM CARBONATE 300MG CAP", 300),
}
CONDS = {
    "t2dm": ("44054006", "Type 2 diabetes mellitus", "250.00"),
    "ckd": ("709044004", "Chronic kidney disease", "585.9"),
    "afib": ("49436004", "Atrial fibrillation", "427.31"),
    "htn": ("38341003", "Hypertensive disorder", "401.9"),
    "hf": ("84114007", "Heart failure", "428.0"),
    "bipolar": ("13746004", "Bipolar disorder", "296.80"),
}

# id, label, sex, age, conditions, active meds, labs {creatinine, potassium, weight, days_ago}, scenarios
PATIENTS = [
    dict(id="hand-01", label="A. Metformin, eGFR 30-45", sex="female", age=74, conds=["t2dm", "ckd"], meds=[],
         cr=1.5, k=None, wt=68,
         scenarios=[("metformin", ["metformin-low-egfr"], ["metformin-low-egfr"], True,
                     "eGFR in the 30-45 band: initiation not recommended")]),
    dict(id="hand-02", label="B. Metformin, normal eGFR", sex="male", age=58, conds=["t2dm"], meds=[],
         cr=0.9, k=None, wt=88,
         scenarios=[("metformin", ["metformin-low-egfr"], [], False, "eGFR ~99: no renal concern")]),
    dict(id="hand-03", label="C. Metformin, no renal labs (data gap)", sex="male", age=60, conds=["t2dm"], meds=[],
         cr=None, k=None, wt=None,
         scenarios=[("metformin", ["metformin-low-egfr"], ["metformin-low-egfr"], True,
                     "no renal function on file: an info-level data-gap card is appropriate")]),
    dict(id="hand-04", label="D. NSAID + ACEI + loop diuretic, eGFR 40", sex="male", age=70, conds=["ckd", "htn", "t2dm"],
         meds=["lisinopril", "furosemide"], cr=1.8, k=None, wt=82,
         scenarios=[("ibuprofen", ["nsaid-raas-diuretic-aki"], ["nsaid-raas-diuretic-aki"], True,
                     "triple whammy with reduced eGFR"),
                    ("metformin", ["metformin-low-egfr"], ["metformin-low-egfr"], True, "eGFR 40 caution band")]),
    dict(id="hand-05", label="E. NSAID + ARB + thiazide, preserved eGFR", sex="male", age=66, conds=["htn"],
         meds=["losartan", "hctz"], cr=1.1, k=None, wt=90,
         scenarios=[("naproxen", ["nsaid-raas-diuretic-aki"], [], False, "eGFR ~74: triple-whammy risk not elevated by renal function")]),
    dict(id="hand-06", label="F. Frail 84 y/o with AF", sex="female", age=84, conds=["afib", "htn"], meds=[],
         cr=1.6, k=None, wt=55,
         scenarios=[("apixaban5", ["apixaban-dose-reduction"], ["apixaban-dose-reduction"], True, "age>=80, weight<=60, Cr>=1.5: all 3 criteria"),
                    ("apixaban2.5", ["apixaban-dose-reduction"], [], False, "already reduced dose (baseline fires on drug name only)"),
                    ("rivaroxaban20", ["rivaroxaban-renal-dose"], ["rivaroxaban-renal-dose"], True, "CrCl ~26 mL/min")]),
    dict(id="hand-07", label="G. Robust 63 y/o with AF", sex="male", age=63, conds=["afib"], meds=[],
         cr=0.9, k=None, wt=90,
         scenarios=[("apixaban5", ["apixaban-dose-reduction"], [], False, "no criteria met"),
                    ("rivaroxaban20", ["rivaroxaban-renal-dose"], [], False, "CrCl > 100 mL/min")]),
    dict(id="hand-08", label="H. ACEI + KCl, potassium/eGFR reassuring (suppression)", sex="male", age=55, conds=["htn", "hf"],
         meds=["lisinopril"], cr=0.9, k=4.3, wt=85,
         scenarios=[("kcl", ["raas-potassium-hyperkalemia"], [], False, "K 4.3 within 30 d and eGFR >= 60 within 90 d: alert suppressed")]),
    dict(id="hand-09", label="I. ACEI + spironolactone, potassium 5.4", sex="female", age=68, conds=["htn", "hf"],
         meds=["lisinopril"], cr=0.9, k=5.4, wt=72,
         scenarios=[("spironolactone", ["raas-potassium-hyperkalemia"], ["raas-potassium-hyperkalemia"], True, "K 5.4: suppression criteria not met")]),
    dict(id="hand-10", label="J. Lithium + NSAID (control rule)", sex="male", age=45, conds=["bipolar"],
         meds=["lithium"], cr=0.9, k=None, wt=80,
         scenarios=[("ibuprofen", ["lithium-interacting-drugs"], ["lithium-interacting-drugs"], True, "control rule: context mode does not change")]),
]


def coding(system: str, code: str, display: str) -> dict:
    return {"system": system, "code": code, "display": display}


def med_request(pid: str, i: int, key: str, status: str = "active") -> dict:
    rx, disp, _, mg = DRUGS[key]
    res = {
        "resourceType": "MedicationRequest", "id": f"{pid}-med-{i}", "status": status, "intent": "order",
        "subject": {"reference": f"Patient/{pid}"},
        "medicationCodeableConcept": {"coding": [coding(RX, rx, disp)], "text": disp},
    }
    if mg is not None:
        res["dosageInstruction"] = [{"doseAndRate": [{"doseQuantity": {"value": mg, "unit": "mg"}}]}]
    return res


def observation(pid: str, name: str, loinc: str, disp: str, value: float, unit: str, when: date) -> dict:
    return {
        "resourceType": "Observation", "id": f"{pid}-{name}", "status": "final",
        "code": {"coding": [coding(LOINC, loinc, disp)], "text": disp},
        "subject": {"reference": f"Patient/{pid}"}, "effectiveDateTime": when.isoformat(),
        "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org"},
    }


def fileman(d: date, hhmm: str = "0800") -> str:
    return f"{d.year - 1700:03d}{d.month:02d}{d.day:02d}.{hhmm}"


def build() -> None:
    out_dir = ROOT / "data" / "patients" / "hand-authored"
    out_dir.mkdir(parents=True, exist_ok=True)
    overlay: list[dict] = []
    for n, p in enumerate(PATIENTS, start=1):
        pid = p["id"]
        birth = date(REC.year - p["age"], 3, 15)
        patient = {
            "resourceType": "Patient", "id": pid, "gender": p["sex"], "birthDate": birth.isoformat(),
            "name": [{"text": f"Synthetic, {p['label'].split('.')[0]}"}],
            "meta": {"tag": [{"system": "urn:medsafe:data", "code": "hand-authored-synthetic"}]},
        }
        conds = [
            {"resourceType": "Condition", "id": f"{pid}-cond-{i}", "subject": {"reference": f"Patient/{pid}"},
             "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
             "code": {"coding": [coding(SCT, CONDS[c][0], CONDS[c][1])], "text": CONDS[c][1]}}
            for i, c in enumerate(p["conds"], 1)
        ]
        meds = [med_request(pid, i, m) for i, m in enumerate(p["meds"], 1)]
        obs: list[dict] = []
        if p["cr"] is not None:
            when = REC - timedelta(days=12)
            obs.append(observation(pid, "creatinine", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", p["cr"], "mg/dL", when))
            egfr = ckd_epi_2021(p["cr"], (when - birth).days / 365.2425, p["sex"])
            obs.append(observation(pid, "egfr", "98979-8", "eGFR CKD-EPI 2021", egfr, "mL/min/1.73m2", when))
        if p["k"] is not None:
            obs.append(observation(pid, "potassium", "2823-3", "Potassium [Moles/volume] in Serum or Plasma", p["k"], "mmol/L", REC - timedelta(days=5)))
        if p["wt"] is not None:
            obs.append(observation(pid, "weight", "29463-7", "Body weight", p["wt"], "kg", REC - timedelta(days=20)))
        scenarios = [
            {"order": med_request(pid, 90 + i, key, "draft"), "order_key": key,
             "expect_baseline": base, "expect_context": ctx, "clinically_warranted": warranted, "rationale": why}
            for i, (key, base, ctx, warranted, why) in enumerate(p["scenarios"], 1)
        ]
        doc = {"id": pid, "label": p["label"], "synthetic": True, "record_date": REC.isoformat(),
               "resources": [patient, *conds, *meds, *obs], "scenarios": scenarios}
        (out_dir / f"{pid}.json").write_text(json.dumps(doc, indent=2) + "\n")

        # ---- VistA twin (raw RPC reply text, same format as live VEHU)
        dfn = str(9000000 + n)
        problems = []
        for i, c in enumerate(p["conds"], 1):
            sct, disp, icd = CONDS[c]
            problems.append(f"{800 + i}^{disp} (SCT {sct})^A^{icd}^^{fileman(REC - timedelta(days=900), '0000')}^^^^^^ORQQPL DETAIL^^{sct}^0^ICD^")
        meds_txt = ""
        for i, m in enumerate(p["meds"], 1):
            name = DRUGS[m][2]
            meds_txt += f"~OP^{700 + i}P;O^{name}^^^^^^{39000 + i}^PENDING^^^30^^0\r\n {name}  Qty: 30\r\n\\ Sig: TAKE ONE TABLET BY MOUTH DAILY\r\n"
        labs = []
        vitals = ""
        if p["cr"] is not None:
            lab_when = REC - timedelta(days=12)
            block = [f"8^CH^{fileman(lab_when, '1237')}^72^SERUM^CH 0001 1^PROVIDER,SYNTHETIC^^^{fileman(lab_when, '1237')}",
                     f"173^CREATININE^{p['cr']:>8}^^mg/dL^.9 - 1.4"]
            if p["k"] is not None:
                block.append(f"177^POTASSIUM^{p['k']:>8}^^meq/L^3.8 - 5.3")
            labs.append("\r\n".join([*block, "Report Released Date/Time: synthetic", ""]))
        if p["k"] is not None and p["cr"] is None:
            k_when = REC - timedelta(days=5)
            labs.append("\r\n".join([f"8^CH^{fileman(k_when, '0900')}^72^SERUM^CH 0002 1^PROVIDER,SYNTHETIC^^^{fileman(k_when, '0900')}",
                                     f"177^POTASSIUM^{p['k']:>8}^^meq/L^3.8 - 5.3", ""]))
        if p["cr"] is not None and p["k"] is not None:
            # potassium drawn 5 days ago (separate, newer collection)
            k_when = REC - timedelta(days=5)
            labs.append("\r\n".join([f"8^CH^{fileman(k_when, '0900')}^72^SERUM^CH 0002 1^PROVIDER,SYNTHETIC^^^{fileman(k_when, '0900')}",
                                     f"177^POTASSIUM^{p['k']:>8}^^meq/L^3.8 - 5.3", ""]))
            labs[0] = "\r\n".join(labs[0].replace(f"177^POTASSIUM^{p['k']:>8}^^meq/L^3.8 - 5.3\r\n", "").split("\r\n"))
        if p["wt"] is not None:
            lb = round(p["wt"] / 0.45359237)
            wd = fileman(REC - timedelta(days=20), "1534")
            vitals = f"29392^WT^{lb}^{wd}^{lb} lb^({p['wt']:.2f} kg)^\r\n"
        newest = max((s.split("^")[2] for s in labs), default="")
        oldest = min((s.split("^")[2] for s in labs), default="")
        name = f"SYNTHETICPATIENT,{p['label'].split('.')[0].strip().upper()}TWIN"
        overlay.append({
            "dfn": dfn, "label": f"{p['label']} [VistA twin of {pid}]",
            "note": "Synthetic patient expressed as raw VistA RPC replies (NOT in VEHU); twin of " + pid,
            "twin_of": pid,
            "replies": {
                "select": f"{name}^{'F' if p['sex'] == 'female' else 'M'}^{fileman(birth, '0000').split('.')[0]}^000000000^^^^^0^^0^0^^",
                "meds": meds_txt, "problems": "\r\n".join(problems) + ("\r\n" if problems else ""),
                "labs": labs, "vitals": vitals, "newold": f"{newest}^{oldest}" if labs else "^",
            },
        })
    (ROOT / "data" / "vista" / "overlay_patients.json").write_text(
        json.dumps({"_comment": "Synthetic patients in VistA wire format. NOT VEHU data.", "patients": overlay}, indent=2) + "\n")
    print(f"wrote {len(PATIENTS)} hand-authored patients and {len(overlay)} VistA overlay twins")


if __name__ == "__main__":
    build()
