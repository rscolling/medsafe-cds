"""FileMan date helpers. ``3150810.1237`` = 2015-08-10 12:37 (year - 1700, MMDD, .HHMMSS)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def fileman_to_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    whole, _, frac = value.partition(".")
    if len(whole) != 7 or not whole.isdigit():
        return None
    year, month, day = int(whole[:3]) + 1700, int(whole[3:5]), int(whole[5:7])
    if frac and not frac.isdigit():
        return None  # e.g. "3150810.ab": not a FileMan time
    frac = (frac + "000000")[:6]
    hh, mm, ss = int(frac[:2]), int(frac[2:4]), int(frac[4:6])
    try:
        # FileMan allows month/day 00 for imprecise dates; use 1 so we still get a date.
        return datetime(year, month or 1, day or 1, min(hh, 23), min(mm, 59), min(ss, 59))
    except ValueError:
        return None


def fileman_is_precise(value: str) -> bool:
    """False when the month or day is 00 (FileMan imprecise date, e.g. a year-only DOB ``3150000``)."""
    whole = value.strip().partition(".")[0]
    return len(whole) == 7 and whole.isdigit() and whole[3:5] != "00" and whole[5:7] != "00"


def fileman_key(value: str) -> Decimal | None:
    """Numeric sort key for a FileMan date/time (compare these, never the strings)."""
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None


def fileman_to_date(value: str) -> date | None:
    dt = fileman_to_datetime(value)
    return dt.date() if dt else None


def date_to_fileman(d: date | datetime) -> str:
    base = f"{d.year - 1700:03d}{d.month:02d}{d.day:02d}"
    if isinstance(d, datetime) and (d.hour or d.minute):
        return f"{base}.{d.hour:02d}{d.minute:02d}"
    return base
