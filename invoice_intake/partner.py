from __future__ import annotations

import re
from typing import Any


def _normalize(text: str) -> str:
    text = text.strip()
    for ch in (" ", "　", "株式会社", "有限会社", "（株）", "(株)"):
        text = text.replace(ch, "")
    return text.lower()


def match_partner(
    partners: list[dict[str, Any]],
    supplier_name: str,
    registration_no: str | None,
) -> tuple[str | None, str]:
    """Return (partner_code, match_reason)."""

    if registration_no:
        for partner in partners:
            if partner.get("registration_no") == registration_no:
                return partner["partner_code"], f"registration_no={registration_no}"

    normalized_supplier = _normalize(supplier_name)
    candidates: list[tuple[int, str, str]] = []

    for partner in partners:
        names = [partner["name"], *partner.get("aliases", [])]
        for name in names:
            normalized_name = _normalize(name)
            if not normalized_name:
                continue
            if normalized_name == normalized_supplier:
                return partner["partner_code"], f"exact_name={name}"
            if normalized_name in normalized_supplier or normalized_supplier in normalized_name:
                score = min(len(normalized_name), len(normalized_supplier))
                candidates.append((score, partner["partner_code"], name))

    if candidates:
        candidates.sort(reverse=True)
        _, code, matched_name = candidates[0]
        return code, f"partial_name={matched_name}"

    return None, "no_match"


def tax_rate_to_code(rate_percent: int) -> str:
    if rate_percent == 8:
        return "T08"
    if rate_percent == 10:
        return "T10"
    raise ValueError(f"Unsupported tax rate: {rate_percent}%")


def normalize_date(value: str) -> str:
    """Convert common Japanese date formats to YYYY-MM-DD."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value

    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", value)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"

    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"

    m = re.fullmatch(r"令和(\d{1,2})年(\d{1,2})月(\d{1,2})日", value)
    if m:
        reiwa_year, mo, d = m.groups()
        western_year = 2018 + int(reiwa_year)
        return f"{western_year}-{int(mo):02d}-{int(d):02d}"

    raise ValueError(f"Unrecognized date format: {value}")
