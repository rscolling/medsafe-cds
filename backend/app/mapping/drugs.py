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
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9\-\.]*")
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(MG|MCG|G)\b")
# Words that are formulation/noise, never part of an ingredient phrase.
_SPLIT = re.compile(r"[/,&+]|\bAND\b|\bW/\b")


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

    def ingredient(self, rxcui: str) -> Ingredient | None:
        return self._by_rxcui.get(rxcui)

    def all_ingredients(self) -> list[Ingredient]:
        return sorted(self._by_rxcui.values(), key=lambda i: i.name)

    def map_text(self, text: str) -> DrugMatch:
        """Map a free-text drug name to ingredients (possibly several for combination products)."""
        upper = text.upper().strip()
        found: dict[str, str] = {}
        for chunk in _SPLIT.split(upper):
            tokens = _TOKEN.findall(chunk)
            i = 0
            while i < len(tokens):
                for n in range(min(self._max_len, len(tokens) - i), 0, -1):
                    key = tuple(tokens[i : i + n])
                    rx = self._phrases.get(key)
                    if rx:
                        found.setdefault(rx, " ".join(key))
                        i += n
                        break
                else:
                    i += 1
        ingredients = tuple(self._by_rxcui[rx] for rx in found)
        return DrugMatch(
            raw=text,
            ingredients=ingredients,
            strength_mg=parse_strength_mg(upper) if ingredients else None,
            matched_phrases=tuple(found.values()),
        )


def parse_strength_mg(text: str) -> float | None:
    """First numeric strength in the text, normalised to mg (``25MCG`` -> 0.025)."""
    m = _STRENGTH.search(text.upper())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    return {"MG": value, "MCG": value / 1000.0, "G": value * 1000.0}[unit]


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
