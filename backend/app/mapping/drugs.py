"""Free-text drug name -> RxNorm ingredient mapping.

VistA medication names are free text (``"METFORMIN HCL 500MG TAB"``), so we match *synonym phrases*
at token boundaries (never raw substrings: ``ASPIRIN`` must not match inside ``ASPIRIN-FREE``),
prefer the longest phrase, and extract a strength when present. Names that cannot be mapped are
returned as unmapped (never guessed) so callers can count and report coverage honestly.

Data: ``data/mapping/rxnorm_ingredients.csv`` (RxCUIs checked against the public RxNav API).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import REPO_ROOT

RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
# Tokens are runs of letters or numbers, so hyphens, periods and glued strengths never hide an ingredient:
# "GLYBURIDE-METFORMIN", "METFORMIN-ER", "METFORMIN500MG" and "METFORMIN." all tokenise cleanly. Phrases in the
# synonym table are indexed with the same tokenizer ("K-DUR" -> K DUR).
_TOKEN = re.compile(r"[A-Z]+|\d+(?:\.\d+)?")
_NUM = r"(?<![\d.])(\d*\.?\d+)"
_STRENGTH = re.compile(_NUM + r"\s*(MG|MCG|UG|GM|G)\b")
_CONCENTRATION_TAIL = re.compile(r"\s*/\s*\d*\.?\d*\s*(ML|L)\b")  # 500MG/5ML, 5 MG/ML: a concentration
_COMBO_NUMBERS = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?(?:\s*[/-]\s*\d+(?:\.\d+)?)+)(?!\s*(?:ML|L)\b)")
_SPLIT = re.compile(r"[/,&+]|\bAND\b|\bW/\b")
_COMBO_SEPARATORS = re.compile(r"[/,&+-]|\bAND\b|\bWITH\b")

# Products that are not systemic exposure to the ingredient (or are not the drug at all). They must not feed
# the systemic-drug rules (NSAID + lithium, triple whammy, ...): topical diclofenac gel is not an oral NSAID.
NON_SYSTEMIC_TOKENS = frozenset(
    {
        "GEL", "CREAM", "OINT", "OINTMENT", "LOTION", "TOPICAL", "OPHTH", "OPHTHALMIC", "EYE", "OTIC", "NASAL",
        "SHAMPOO", "FOAM", "FLUSH", "FAB", "IMMUNE",
    }
)  # fmt: skip
# Words that describe form, salt or sig, never a second ingredient (used only to decide "partial match").
NOISE_TOKENS = frozenset(
    {
        "TAB", "TABS", "TABLET", "TABLETS", "CAP", "CAPS", "CAPSULE", "CAPSULES", "MG", "MCG", "UG", "G", "GM", "ML",
        "MEQ", "UNIT", "UNITS", "ER", "XR", "SR", "CR", "EC", "SA", "XL", "LA", "DR", "HCL", "HYDROCHLORIDE", "SODIUM",
        "SULFATE", "SO4", "TARTRATE", "SUCCINATE", "MALEATE", "MESYLATE", "ACETATE", "BESYLATE", "BROMIDE", "ORAL",
        "PO", "INJ", "INJECTION", "IV", "SOLN", "SOLUTION", "SUSP", "SYRUP", "ELIXIR", "LIQUID", "DOSE", "DAILY", "BID",
        "TID", "QD", "PRN", "OF", "THE", "W", "AND", "WITH", "FOR", "EXTENDED", "RELEASE", "DELAYED", "CHEWABLE",
        "DISINTEGRATING", "SLOW", "ODT", "CONC", "HFA", "INH", "INHALER", "NON", "VA", "COMB", "PACK", "PAK",
    }
)  # fmt: skip
_BARE_POTASSIUM_EXCLUDE = frozenset({"CITRATE", "BICARBONATE", "GLUCONATE", "PHOSPHATE", "IODIDE", "PERMANGANATE"})


@dataclass(frozen=True)
class Ingredient:
    rxcui: str
    name: str
    classes: frozenset[str]


@dataclass(frozen=True)
class DrugMatch:
    """Result of mapping one free-text drug string."""

    raw: str
    ingredients: tuple[Ingredient, ...] = ()
    strength_mg: float | None = None
    matched_phrases: tuple[str, ...] = field(default_factory=tuple)
    # Strength of each ingredient (same order as ``ingredients``); None when it cannot be assigned safely.
    strengths_mg: tuple[float | None, ...] = ()
    # True when the text looks like a combination product but some segment was not recognised
    # ("SACUBITRIL/VALSARTAN" maps valsartan only): callers must not treat it as fully checked.
    partial: bool = False
    # Non-empty when the product is not systemic exposure to the ingredient (gel, cream, ophthalmic, flush, ...).
    excluded: str = ""

    @property
    def mapped(self) -> bool:
        return bool(self.ingredients)

    @property
    def classes(self) -> frozenset[str]:
        out: set[str] = set()
        for i in self.ingredients:
            out |= i.classes
        return frozenset(out)


class DrugMapper:
    """Longest-phrase synonym matcher over the ingredient table."""

    def __init__(self, csv_path: Path | None = None) -> None:
        path = csv_path or (REPO_ROOT / "data" / "mapping" / "rxnorm_ingredients.csv")
        self._by_rxcui: dict[str, Ingredient] = {}
        self._phrases: dict[tuple[str, ...], str] = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ing = Ingredient(
                    rxcui=row["rxcui"],
                    name=row["ingredient"],
                    # every ingredient is also its own class so rules can target one drug ("apixaban")
                    classes=frozenset(c for c in row["classes"].split(";") if c)
                    | {row["ingredient"].replace(" ", "_")},
                )
                self._by_rxcui[ing.rxcui] = ing
                phrases = {row["ingredient"].upper(), *filter(None, row["synonyms"].split("|"))}
                for ph in phrases:
                    self._phrases[tuple(_TOKEN.findall(ph.upper()))] = ing.rxcui
        self._max_len = max(len(k) for k in self._phrases)
        self._kcl = next((i for i in self._by_rxcui.values() if i.name == "potassium chloride"), None)

    def ingredient(self, rxcui: str) -> Ingredient | None:
        return self._by_rxcui.get(rxcui)

    def all_ingredients(self) -> list[Ingredient]:
        return sorted(self._by_rxcui.values(), key=lambda i: i.name)

    def map_text(self, text: str) -> DrugMatch:
        """Map a free-text drug name to ingredients (several for combination products)."""
        upper = text.upper().strip()
        tokens = _TOKEN.findall(upper)
        excluded = sorted(NON_SYSTEMIC_TOKENS.intersection(tokens))
        if excluded:
            return DrugMatch(raw=text, excluded=f"non-systemic or non-drug product ({', '.join(excluded)})")
        found: dict[str, str] = {}
        consumed: set[int] = set()
        i = 0
        while i < len(tokens):
            for n in range(min(self._max_len, len(tokens) - i), 0, -1):
                key = tuple(tokens[i : i + n])
                rx = self._phrases.get(key)
                if rx:
                    found.setdefault(rx, " ".join(key))
                    consumed.update(range(i, i + n))
                    i += n
                    break
            else:
                i += 1
        # Bare "POTASSIUM 20MEQ" is potassium chloride unless it is part of another product or salt
        # ("LOSARTAN POTASSIUM", "POTASSIUM CITRATE").
        if not found and self._kcl and "POTASSIUM" in tokens and not _BARE_POTASSIUM_EXCLUDE.intersection(tokens):
            found[self._kcl.rxcui] = "POTASSIUM"
            consumed.add(tokens.index("POTASSIUM"))
        ingredients = tuple(self._by_rxcui[rx] for rx in found)
        partial = False
        if ingredients and _COMBO_SEPARATORS.search(upper):
            leftovers = [
                t
                for idx, t in enumerate(tokens)
                if idx not in consumed and t.isalpha() and len(t) >= 4 and t not in NOISE_TOKENS
            ]
            partial = bool(leftovers)
        strengths = _assign_strengths(upper, len(ingredients)) if ingredients else ()
        return DrugMatch(
            raw=text,
            ingredients=ingredients,
            strength_mg=strengths[0] if strengths else None,
            matched_phrases=tuple(found.values()),
            strengths_mg=strengths,
            partial=partial,
        )


def strengths_mg_in(text: str) -> list[float]:
    """All dose strengths in the text as mg, in order of appearance (concentrations like 500MG/5ML are skipped)."""
    out: list[float] = []
    upper = text.upper()
    for m in _STRENGTH.finditer(upper):
        if _CONCENTRATION_TAIL.match(upper, m.end()):
            continue
        value, unit = float(m.group(1)), m.group(2)
        out.append(
            {"MG": value, "MCG": value / 1000.0, "UG": value / 1000.0, "GM": value * 1000.0, "G": value * 1000.0}[unit]
        )
    return out


def _assign_strengths(upper: str, n_ingredients: int) -> tuple[float | None, ...]:
    """Strength per ingredient, only when it can be assigned safely (never copy one number to every ingredient)."""
    found = strengths_mg_in(upper)
    combo = _COMBO_NUMBERS.search(upper)
    if combo and n_ingredients > 1:
        nums = [float(x) for x in re.split(r"\s*[/-]\s*", combo.group(1))]
        if len(nums) == n_ingredients:  # "2.5/500", "20-12.5", "20/12.5MG": one number per ingredient, mg
            return tuple(nums)
    if n_ingredients == 1:
        return (found[0] if found else None,)
    if found and len(found) == n_ingredients:
        return tuple(found)
    return (None,) * n_ingredients


def parse_strength_mg(text: str) -> float | None:
    """First dose strength in the text, normalised to mg (``25MCG`` -> 0.025, ``.25MG`` -> 0.25, ``1 GM`` -> 1000)."""
    found = strengths_mg_in(text)
    return found[0] if found else None


@lru_cache(maxsize=1)
def default_mapper() -> DrugMapper:
    return DrugMapper()


@lru_cache(maxsize=1)
def clinical_drugs() -> dict[str, tuple[str, float | None, str]]:
    """SCD RxCUI -> (ingredient rxcui, strength mg, description) for coded (FHIR) orders."""
    path = REPO_ROOT / "data" / "mapping" / "rxnorm_clinical_drugs.csv"
    out: dict[str, tuple[str, float | None, str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            strength = float(row["strength_mg"]) if row["strength_mg"] else None
            out[row["scd_rxcui"]] = (row["ingredient_rxcui"], strength, row["description"])
    return out
