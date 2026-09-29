from __future__ import annotations

import pytest

from app.clinical.egfr import ckd_epi_2021, cockcroft_gault


@pytest.mark.parametrize(
    ("cr", "age", "sex", "expected"),
    [
        (1.0, 60, "male", 86.2),  # hand-computed from the published CKD-EPI 2021 equation
        (0.8, 55, "female", 87.0),
        (1.5, 74, "female", 36.3),
        (2.1, 69.3, "female", 25.0),  # VEHU DFN 100881 shape (female, creatinine 2.1)
        (1.0, 50, "female", 68.7),  # widely quoted CKD-EPI 2021 example (~69)
        (0.5, 30, "male", 140.7),  # below kappa: alpha branch
    ],
)
def test_ckd_epi_2021(cr: float, age: float, sex: str, expected: float) -> None:
    assert ckd_epi_2021(cr, age, sex) == pytest.approx(expected, abs=0.15)


def test_reference_formula_independent_check() -> None:
    cr, age = 1.8, 70
    manual = 142 * (cr / 0.9) ** -1.2 * 0.9938**age
    assert ckd_epi_2021(cr, age, "male") == pytest.approx(manual, abs=0.06)


def test_female_higher_than_male_at_same_high_creatinine_age() -> None:
    assert ckd_epi_2021(2.0, 60, "female") < ckd_epi_2021(2.0, 60, "male") * 1.02


@pytest.mark.parametrize(("cr", "age"), [(0, 50), (-1, 50), (1, -1)])
def test_ckd_epi_rejects_bad_input(cr: float, age: float) -> None:
    with pytest.raises(ValueError, match="must"):
        ckd_epi_2021(cr, age, "male")


def test_cockcroft_gault() -> None:
    assert cockcroft_gault(1.0, 60, 72, "male") == pytest.approx(80.0)
    assert cockcroft_gault(1.0, 60, 72, "female") == pytest.approx(68.0)
    with pytest.raises(ValueError, match="positive"):
        cockcroft_gault(0, 60, 72, "male")
