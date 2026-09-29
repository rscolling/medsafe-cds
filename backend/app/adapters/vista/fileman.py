"""FileMan date helpers. ``3150810.1237`` = 2015-08-10 12:37 (year - 1700, MMDD, .HHMMSS)."""

from __future__ import annotations

from datetime import date, datetime


def fileman_to_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    whole, _, frac = value.partition(".")
    if len(whole) != 7 or not whole.isdigit():
        return None
    year, month, day = int(whole[:3]) + 1700, int(whole[3:5]), int(whole[5:7])
    frac = (frac + "000000")[:6]
    hh, mm, ss = int(frac[:2]), int(frac[2:4]), int(frac[4:6])
    try:
        # FileMan allows month/day 00 for imprecise dates; use 1 so we still get a date.
        return datetime(year, month or 1, day or 1, min(hh, 23), min(mm, 59), min(ss, 59))
    except ValueError:
        return None


def fileman_to_date(value: str) -> date | None:
    dt = fileman_to_datetime(value)
    return dt.date() if dt else None


def date_to_fileman(d: date | datetime) -> str:
    base = f"{d.year - 1700:03d}{d.month:02d}{d.day:02d}"
    if isinstance(d, datetime) and (d.hour or d.minute):
        return f"{base}.{d.hour:02d}{d.minute:02d}"
    return base
