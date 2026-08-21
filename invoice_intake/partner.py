from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# A partial (substring) name match on fewer characters than this is not trusted.
# Short overlaps like "IT" or "東京" can bind an invoice to the wrong payee, which
# in a payment pipeline means money sent to the wrong company.
MIN_PARTIAL_MATCH_CHARS = 4

# Near-miss threshold for suggesting a partner to a human reviewer. A suggestion
# never registers on its own — a single misread character (東京フーズ ->
# 東京アーズ) should reach a reviewer with a candidate, not as a dead end.
FUZZY_SUGGEST_RATIO = 0.8

# Japanese era -> (western year of era year 1) - 1, so western = base + era_year.
ERA_BASE = {
    "令和": 2018,
    "R": 2018,
    "平成": 1988,
    "H": 1988,
    "昭和": 1925,
    "S": 1925,
}

_LEGAL_FORMS = ("株式会社", "有限会社", "合同会社", "（株）", "(株)", "（有）", "(有)")


def _normalize(text: str) -> str:
    # NFKC folds full-width latin/digits so "ＩＴ" and "IT" compare equal.
    text = unicodedata.normalize("NFKC", text).strip()
    for ch in (" ", "　", *_LEGAL_FORMS):
        text = text.replace(ch, "")
    return text.lower()


def match_partner(
    partners: list[dict[str, Any]],
    supplier_name: str,
    registration_no: str | None,
) -> tuple[str | None, str, bool]:
    """Return (partner_code, match_reason, needs_review).

    needs_review is True when the match rests on a partial name overlap rather
    than a registration number or an exact name, so a human confirms the payee
    before the invoice is registered.
    """

    if registration_no:
        normalized_reg = _normalize(registration_no)
        for partner in partners:
            if _normalize(str(partner.get("registration_no", ""))) == normalized_reg:
                return partner["partner_code"], f"registration_no={registration_no}", False

    normalized_supplier = _normalize(supplier_name)
    candidates: list[tuple[int, str, str]] = []

    for partner in partners:
        names = [partner["name"], *partner.get("aliases", [])]
        for name in names:
            normalized_name = _normalize(name)
            if not normalized_name:
                continue
            if normalized_name == normalized_supplier:
                return partner["partner_code"], f"exact_name={name}", False
            if normalized_name in normalized_supplier or normalized_supplier in normalized_name:
                overlap = min(len(normalized_name), len(normalized_supplier))
                if overlap < MIN_PARTIAL_MATCH_CHARS:
                    continue
                candidates.append((overlap, partner["partner_code"], name))

    if candidates:
        candidates.sort(key=lambda c: (-c[0], c[1]))
        overlap, code, matched_name = candidates[0]
        # Two different partners overlapping equally well is ambiguous — refuse
        # rather than pick by alphabetical accident.
        tied = {c[1] for c in candidates if c[0] == overlap}
        if len(tied) > 1:
            return None, f"ambiguous_partial_name={sorted(tied)}", True
        return code, f"partial_name={matched_name}", True

    suggestion = suggest_partner(partners, supplier_name)
    if suggestion:
        code, name, ratio = suggestion
        return code, f"fuzzy_suggestion={name} (similarity {ratio:.2f})", True

    return None, "no_match", True


def suggest_partner(
    partners: list[dict[str, Any]], supplier_name: str
) -> tuple[str, str, float] | None:
    """Closest partner name above the near-miss threshold, for review only."""
    normalized_supplier = _normalize(supplier_name)
    if not normalized_supplier:
        return None

    best: tuple[str, str, float] | None = None
    for partner in partners:
        for name in [partner["name"], *partner.get("aliases", [])]:
            ratio = difflib.SequenceMatcher(None, _normalize(name), normalized_supplier).ratio()
            if ratio >= FUZZY_SUGGEST_RATIO and (best is None or ratio > best[2]):
                best = (partner["partner_code"], name, ratio)
    return best


def tax_rate_to_code(rate_percent: int) -> str | None:
    """Map a percentage to the accounting API's tax code, or None if unsupported.

    Returns None instead of raising so an odd rate (0%, 非課税, an OCR slip)
    becomes a reported verification error rather than a crashed invoice.
    """
    if rate_percent == 8:
        return "T08"
    if rate_percent == 10:
        return "T10"
    return None


def _to_iso(year: int, month: str, day: str) -> str:
    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def normalize_date(value: str) -> str:
    """Convert common Japanese date formats to YYYY-MM-DD.

    Returns the input unchanged when the format is not recognised; validation
    then reports DATE_UNPARSEABLE instead of the whole invoice crashing.
    """
    text = unicodedata.normalize("NFKC", value).strip()
    # 元年 is era year 1.
    text = text.replace("元年", "1年")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text

    m = re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return _to_iso(int(y), mo, d)

    m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?", text)
    if m:
        y, mo, d = m.groups()
        return _to_iso(int(y), mo, d)

    # 令和8年2月5日 / 平成31年4月30日
    m = re.fullmatch(r"(令和|平成|昭和)\s*(\d{1,2})年\s*(\d{1,2})月\s*(\d{1,2})日?", text)
    if m:
        era, era_year, mo, d = m.groups()
        return _to_iso(ERA_BASE[era] + int(era_year), mo, d)

    # R8.2.5 / H31/4/30
    m = re.fullmatch(r"([RHS])\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{1,2})", text.upper())
    if m:
        era, era_year, mo, d = m.groups()
        return _to_iso(ERA_BASE[era] + int(era_year), mo, d)

    return text


def dates_in_invoice_number(invoice_number: str) -> list[tuple[int, int]]:
    """Year/month pairs implied by date-like runs in an invoice number.

    Many suppliers embed the issue date in the number (SATO-260205 -> 2026-02).
    Used only as a cross-check signal against the extracted issue date, because
    an era-year misread (令和8 -> 令和5) is invisible to arithmetic checks.
    """
    found: list[tuple[int, int]] = []
    text = unicodedata.normalize("NFKC", invoice_number)

    for match in re.finditer(r"(?<!\d)(\d{4})[-/]?(\d{2})(?!\d)", text):
        year, month = int(match.group(1)), int(match.group(2))
        if 2000 <= year <= 2100 and 1 <= month <= 12:
            found.append((year, month))

    for match in re.finditer(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", text):
        yy, month, day = (int(g) for g in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.append((2000 + yy, month))

    return found
