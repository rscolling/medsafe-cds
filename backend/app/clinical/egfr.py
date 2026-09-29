"""Renal-function formulas (pure functions; unit-tested against hand-computed values)."""

from __future__ import annotations

Sex = str  # "male" | "female"


def ckd_epi_2021(creatinine_mg_dl: float, age_years: float, sex: Sex) -> float:
    """CKD-EPI 2021 race-free creatinine equation, mL/min/1.73 m2.

    Inker LA et al., N Engl J Med 2021;385:1737-49 (https://pubmed.ncbi.nlm.nih.gov/34554658/).
    eGFR = 142 x min(Scr/k, 1)^a x max(Scr/k, 1)^-1.200 x 0.9938^age x 1.012 [female]
    """
    if creatinine_mg_dl <= 0:
        raise ValueError("creatinine must be positive")
    if age_years < 0:
        raise ValueError("age must be non-negative")
    female = sex.lower() in {"female", "f"}
    kappa = 0.7 if female else 0.9
    alpha = -0.241 if female else -0.302
    ratio = creatinine_mg_dl / kappa
    value = (
        142.0 * min(ratio, 1.0) ** alpha * max(ratio, 1.0) ** -1.200 * 0.9938**age_years * (1.012 if female else 1.0)
    )
    return float(round(value, 1))


def cockcroft_gault(creatinine_mg_dl: float, age_years: float, weight_kg: float, sex: Sex) -> float:
    """Cockcroft-Gault creatinine clearance in mL/min (used by DOAC labelling)."""
    if creatinine_mg_dl <= 0 or weight_kg <= 0:
        raise ValueError("creatinine and weight must be positive")
    crcl = (140.0 - age_years) * weight_kg / (72.0 * creatinine_mg_dl)
    if sex.lower() in {"female", "f"}:
        crcl *= 0.85
    return round(crcl, 1)
