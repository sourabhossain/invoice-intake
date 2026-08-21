from __future__ import annotations

import math
import os
import re
from datetime import date, timedelta
from typing import Any

from invoice_intake.models import (
    ExtractedInvoice,
    ExtractedLine,
    VerificationIssue,
    VerificationResult,
)
from invoice_intake.partner import dates_in_invoice_number, tax_rate_to_code

TAX_RATES = {"T10": 0.10, "T08": 0.08}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_MAX_INVOICE_AGE_DAYS = 730
DEFAULT_MAX_FUTURE_DAYS = 30

_PAYMENT_TERMS = ("振込先", "口座", "銀行", "bank", "account", "transfer")
_ALTERATION_TERMS = (
    "手書",
    "変更",
    "訂正",
    "書き換え",
    "赤",
    "handwrit",
    "hand-writ",
    "hand written",
    "by hand",
    "altered",
    "amended",
    "overwritten",
    "crossed out",
    "struck",
    "red ink",
    "blue ink",
    "in pen",
    "ballpoint",
    "marker",
)


def _window() -> tuple[int, int]:
    def env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, "") or default)
        except ValueError:
            return default

    return (
        env_int("INVOICE_MAX_AGE_DAYS", DEFAULT_MAX_INVOICE_AGE_DAYS),
        env_int("INVOICE_MAX_FUTURE_DAYS", DEFAULT_MAX_FUTURE_DAYS),
    )


def compute_amounts(lines: list[dict[str, Any]]) -> tuple[int, int, int]:
    subtotal = sum(item["amount"] for item in lines)
    subtotal_by_code: dict[str, int] = {}
    for item in lines:
        subtotal_by_code[item["tax_code"]] = (
            subtotal_by_code.get(item["tax_code"], 0) + item["amount"]
        )
    tax_amount = sum(
        math.floor(amount * TAX_RATES[code]) for code, amount in subtotal_by_code.items()
    )
    return subtotal, tax_amount, subtotal + tax_amount


def build_api_lines(lines: list[ExtractedLine]) -> list[dict[str, Any]]:
    return [
        {
            "description": line.description,
            "quantity": line.quantity,
            "unit": line.unit,
            "unit_price": line.unit_price,
            "amount": line.amount,
            "tax_code": tax_rate_to_code(line.tax_rate_percent),
        }
        for line in lines
    ]


def _error(code: str, message: str) -> VerificationIssue:
    return VerificationIssue(severity="error", code=code, message=message)


def _warning(code: str, message: str) -> VerificationIssue:
    return VerificationIssue(severity="warning", code=code, message=message)


def _parse_iso(value: str) -> date | None:
    if not ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_lines(invoice: ExtractedInvoice) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []

    if not invoice.lines:
        return [_error("NO_LINE_ITEMS", "No billable line items were extracted")]

    for index, line in enumerate(invoice.lines, start=1):
        for name, value in (("description", line.description), ("unit", line.unit)):
            if not value.strip():
                issues.append(
                    _error("LINE_FIELD_MISSING", f"Line {index}: {name} is empty")
                )

        if line.quantity is not None and line.unit_price is not None:
            expected = line.quantity * line.unit_price
            if expected != line.amount:
                issues.append(
                    _warning(
                        "LINE_MATH",
                        f"Line {index}: quantity*unit_price={expected} but amount={line.amount}",
                    )
                )

    return issues


def _check_dates(invoice: ExtractedInvoice, today: date) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    max_age, max_future = _window()

    parsed: dict[str, date | None] = {}
    for field in ("issue_date", "due_date"):
        raw = getattr(invoice, field)
        parsed[field] = _parse_iso(raw)
        if parsed[field] is None:
            issues.append(
                _error("DATE_UNPARSEABLE", f"{field} {raw!r} is not a usable YYYY-MM-DD date")
            )

    issue_date, due_date = parsed["issue_date"], parsed["due_date"]

    if issue_date and due_date and due_date < issue_date:
        issues.append(
            _error(
                "DUE_DATE_BEFORE_ISSUE_DATE",
                f"due_date {due_date} is earlier than issue_date {issue_date}",
            )
        )

    if not issue_date:
        return issues

    if issue_date > today + timedelta(days=max_future):
        issues.append(
            _error(
                "ISSUE_DATE_IMPLAUSIBLE",
                f"issue_date {issue_date} is more than {max_future} days in the future",
            )
        )
    elif issue_date < today - timedelta(days=max_age):
        issues.append(
            _error(
                "ISSUE_DATE_IMPLAUSIBLE",
                f"issue_date {issue_date} is more than {max_age} days old — "
                "likely an era-year or digit misread",
            )
        )

    embedded = dates_in_invoice_number(invoice.invoice_number)
    if embedded and (issue_date.year, issue_date.month) not in embedded:
        issues.append(
            _warning(
                "DATE_NUMBER_DISAGREEMENT",
                f"issue_date {issue_date} disagrees with the date implied by "
                f"invoice_number {invoice.invoice_number!r} ({embedded})",
            )
        )

    return issues


def _check_payment_integrity(invoice: ExtractedInvoice) -> list[VerificationIssue]:
    notes = invoice.confidence_notes.lower()
    described = any(term.lower() in notes for term in _PAYMENT_TERMS) and any(
        term.lower() in notes for term in _ALTERATION_TERMS
    )
    if not invoice.payment_details_altered and not described:
        return []

    detail = f" Note: {invoice.confidence_notes}" if invoice.confidence_notes else ""
    return [
        _error(
            "PAYMENT_DETAILS_ALTERED",
            "Bank/transfer details appear hand-altered — confirm with the supplier "
            f"through a known channel before paying.{detail}",
        )
    ]


def verify_extraction(
    invoice: ExtractedInvoice, *, today: date | None = None
) -> VerificationResult:
    today = today or date.today()

    issues = _check_lines(invoice)
    api_lines = build_api_lines(invoice.lines)

    unsupported = sorted(
        {line.tax_rate_percent for line in invoice.lines if tax_rate_to_code(line.tax_rate_percent) is None}
    )
    if unsupported:
        issues.append(
            _error(
                "UNSUPPORTED_TAX_RATE",
                f"Tax rate(s) {unsupported} have no accounting API tax code "
                "(only 10% and 8% are registrable)",
            )
        )

    issues.extend(_check_dates(invoice, today))
    issues.extend(_check_payment_integrity(invoice))

    if unsupported or not invoice.lines:
        subtotal = sum(line.amount for line in invoice.lines)
        return VerificationResult(False, issues, subtotal, 0, subtotal)

    computed_subtotal, computed_tax, computed_total = compute_amounts(api_lines)
    subtotal_ok = invoice.subtotal == computed_subtotal

    if not subtotal_ok:
        issues.append(
            _error(
                "SUBTOTAL_MISMATCH",
                f"Extracted subtotal {invoice.subtotal} != sum of line amounts "
                f"{computed_subtotal} — the line items were probably misread",
            )
        )

    hint = (
        " (the line amounts do sum to the extracted subtotal, so check this figure "
        "against the paper rather than the line items)"
        if subtotal_ok
        else ""
    )

    if invoice.tax_amount != computed_tax:
        issues.append(
            _error(
                "TAX_MISMATCH",
                f"Extracted tax {invoice.tax_amount} != {computed_tax} recomputed from "
                f"the lines under API tax rules{hint}",
            )
        )

    if invoice.total_amount != computed_total:
        issues.append(
            _error(
                "TOTAL_MISMATCH",
                f"Extracted total {invoice.total_amount} != {computed_total} recomputed "
                f"from the lines under API tax rules{hint}",
            )
        )

    if invoice.subtotal + invoice.tax_amount != invoice.total_amount:
        issues.append(
            _warning(
                "EXTRACTED_TOTALS_INCONSISTENT",
                f"Figures read from the invoice do not add up: {invoice.subtotal} + "
                f"{invoice.tax_amount} != {invoice.total_amount}",
            )
        )

    passed = not any(issue.severity == "error" for issue in issues)
    return VerificationResult(passed, issues, computed_subtotal, computed_tax, computed_total)


def to_api_payload(
    invoice: ExtractedInvoice,
    partner_code: str,
    verification: VerificationResult,
) -> dict[str, Any]:
    return {
        "partner_code": partner_code,
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "currency": "JPY",
        "lines": build_api_lines(invoice.lines),
        "subtotal": verification.computed_subtotal,
        "tax_amount": verification.computed_tax,
        "total_amount": verification.computed_total,
    }
