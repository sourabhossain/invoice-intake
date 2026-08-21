from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

from invoice_intake.models import ExtractedInvoice, ExtractedLine, VerificationIssue, VerificationResult
from invoice_intake.partner import dates_in_invoice_number, tax_rate_to_code


TAX_RATES = {"T10": 0.10, "T08": 0.08}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# An issue date outside this window around today is treated as a misread rather
# than a real date. Catches era-year slips (令和8年 -> 令和5年 is three years off)
# that every arithmetic check passes cleanly.
MAX_INVOICE_AGE_DAYS = 730
MAX_FUTURE_DAYS = 30

# Handwritten alterations to where the money goes are the standard invoice
# redirection fraud pattern, so they hold the invoice for human review
# regardless of whether the amounts add up.
PAYMENT_ALTERATION_TERMS = (
    "振込先",
    "口座",
    "銀行",
    "支店",
    "bank",
    "account",
    "transfer",
)
HANDWRITING_TERMS = (
    "手書",
    "handwrit",
    "hand-writ",
    "handwritten",
    "変更",
    "書き換え",
    "訂正",
    "赤字",
    "赤ペン",
    "annotation",
    "annotated",
    "altered",
    "crossed out",
)


def compute_amounts(lines: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Recompute subtotal/tax/total exactly the way the accounting API does."""
    subtotal = sum(item["amount"] for item in lines)
    subtotal_by_code: dict[str, int] = {}
    for item in lines:
        subtotal_by_code[item["tax_code"]] = (
            subtotal_by_code.get(item["tax_code"], 0) + item["amount"]
        )
    tax_by_code = {
        code: math.floor(amount * TAX_RATES[code])
        for code, amount in subtotal_by_code.items()
    }
    tax_amount = sum(tax_by_code.values())
    return subtotal, tax_amount, subtotal + tax_amount


def build_api_lines(lines: list[ExtractedLine]) -> list[dict[str, Any]]:
    """Shape lines for the API. tax_code is None when the rate is unsupported."""
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


def _parse_iso(value: str) -> date | None:
    if not ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_dates(invoice: ExtractedInvoice, today: date) -> list[VerificationIssue]:
    """Sanity-check dates. Amount checks cannot see a wrong date at all."""
    issues: list[VerificationIssue] = []

    parsed: dict[str, date | None] = {}
    for field in ("issue_date", "due_date"):
        raw = getattr(invoice, field)
        parsed[field] = _parse_iso(raw)
        if parsed[field] is None:
            issues.append(
                VerificationIssue(
                    severity="error",
                    code="DATE_UNPARSEABLE",
                    message=f"{field} {raw!r} is not a usable YYYY-MM-DD date",
                )
            )

    issue_date, due_date = parsed["issue_date"], parsed["due_date"]

    if issue_date and due_date and due_date < issue_date:
        issues.append(
            VerificationIssue(
                severity="error",
                code="DUE_DATE_BEFORE_ISSUE_DATE",
                message=f"due_date {due_date} is earlier than issue_date {issue_date}",
            )
        )

    if issue_date:
        if issue_date > today + timedelta(days=MAX_FUTURE_DAYS):
            issues.append(
                VerificationIssue(
                    severity="error",
                    code="ISSUE_DATE_IMPLAUSIBLE",
                    message=f"issue_date {issue_date} is more than {MAX_FUTURE_DAYS} days in the future",
                )
            )
        elif issue_date < today - timedelta(days=MAX_INVOICE_AGE_DAYS):
            issues.append(
                VerificationIssue(
                    severity="error",
                    code="ISSUE_DATE_IMPLAUSIBLE",
                    message=(
                        f"issue_date {issue_date} is more than {MAX_INVOICE_AGE_DAYS} days old — "
                        "likely an era-year or digit misread"
                    ),
                )
            )

        # Suppliers commonly embed the issue date in the invoice number. A
        # disagreement is a second, arithmetic-independent signal of a misread.
        embedded = dates_in_invoice_number(invoice.invoice_number)
        if embedded and (issue_date.year, issue_date.month) not in embedded:
            issues.append(
                VerificationIssue(
                    severity="warning",
                    code="DATE_NUMBER_DISAGREEMENT",
                    message=(
                        f"issue_date {issue_date} disagrees with the date implied by "
                        f"invoice_number {invoice.invoice_number!r} ({embedded})"
                    ),
                )
            )

    return issues


def _check_payment_alterations(invoice: ExtractedInvoice) -> list[VerificationIssue]:
    notes = invoice.confidence_notes.lower()
    if not notes:
        return []
    if any(term.lower() in notes for term in PAYMENT_ALTERATION_TERMS) and any(
        term.lower() in notes for term in HANDWRITING_TERMS
    ):
        return [
            VerificationIssue(
                severity="error",
                code="PAYMENT_DETAILS_ALTERED",
                message=(
                    "Bank/transfer details appear hand-altered — hold for human "
                    f"confirmation with the supplier. Note: {invoice.confidence_notes}"
                ),
            )
        ]
    return []


def verify_extraction(
    invoice: ExtractedInvoice, *, today: date | None = None
) -> VerificationResult:
    today = today or date.today()
    issues: list[VerificationIssue] = []

    if not invoice.lines:
        issues.append(
            VerificationIssue(
                severity="error",
                code="NO_LINE_ITEMS",
                message="No billable line items were extracted",
            )
        )

    api_lines = build_api_lines(invoice.lines)

    unsupported = sorted(
        {
            line.tax_rate_percent
            for line, api_line in zip(invoice.lines, api_lines)
            if api_line["tax_code"] is None
        }
    )
    if unsupported:
        issues.append(
            VerificationIssue(
                severity="error",
                code="UNSUPPORTED_TAX_RATE",
                message=(
                    f"Tax rate(s) {unsupported} have no accounting API tax code "
                    "(only 10% and 8% are registrable)"
                ),
            )
        )

    for index, line in enumerate(invoice.lines):
        if line.quantity is not None and line.unit_price is not None:
            expected = line.quantity * line.unit_price
            if expected != line.amount:
                issues.append(
                    VerificationIssue(
                        severity="warning",
                        code="LINE_MATH",
                        message=(
                            f"Line {index + 1}: quantity*unit_price={expected} "
                            f"but amount={line.amount}"
                        ),
                    )
                )

    issues.extend(_check_dates(invoice, today))
    issues.extend(_check_payment_alterations(invoice))

    if unsupported or not invoice.lines:
        # Recomputation is meaningless without a tax code for every line.
        computed_subtotal = sum(line.amount for line in invoice.lines)
        return VerificationResult(
            passed=False,
            issues=issues,
            computed_subtotal=computed_subtotal,
            computed_tax=0,
            computed_total=computed_subtotal,
        )

    computed_subtotal, computed_tax, computed_total = compute_amounts(api_lines)

    subtotal_ok = invoice.subtotal == computed_subtotal
    if not subtotal_ok:
        issues.append(
            VerificationIssue(
                severity="error",
                code="SUBTOTAL_MISMATCH",
                message=(
                    f"Extracted subtotal {invoice.subtotal} != sum of line amounts "
                    f"{computed_subtotal} — the line items were probably misread"
                ),
            )
        )

    # We cannot tell from the numbers alone whether a disagreeing figure was
    # misread or was already wrong on the invoice, so the codes say what was
    # observed and leave the diagnosis to the reviewer. The "lines sum
    # correctly" hint tells them where to look first.
    lines_hint = (
        " (the line amounts do sum to the extracted subtotal, so check this figure "
        "against the paper rather than the line items)"
        if subtotal_ok
        else ""
    )

    if invoice.tax_amount != computed_tax:
        issues.append(
            VerificationIssue(
                severity="error",
                code="TAX_MISMATCH",
                message=(
                    f"Extracted tax {invoice.tax_amount} != {computed_tax} recomputed "
                    f"from the lines under API tax rules{lines_hint}"
                ),
            )
        )

    if invoice.total_amount != computed_total:
        issues.append(
            VerificationIssue(
                severity="error",
                code="TOTAL_MISMATCH",
                message=(
                    f"Extracted total {invoice.total_amount} != {computed_total} recomputed "
                    f"from the lines under API tax rules{lines_hint}"
                ),
            )
        )

    # The three figures read off the page do not even agree with each other.
    # Independent of the recomputation, that means either the invoice's own
    # arithmetic is wrong or one of the three was misread.
    if invoice.subtotal + invoice.tax_amount != invoice.total_amount:
        issues.append(
            VerificationIssue(
                severity="warning",
                code="EXTRACTED_TOTALS_INCONSISTENT",
                message=(
                    f"Figures read from the invoice do not add up: "
                    f"{invoice.subtotal} + {invoice.tax_amount} != {invoice.total_amount}"
                ),
            )
        )

    has_errors = any(issue.severity == "error" for issue in issues)
    return VerificationResult(
        passed=not has_errors,
        issues=issues,
        computed_subtotal=computed_subtotal,
        computed_tax=computed_tax,
        computed_total=computed_total,
    )


def to_api_payload(
    invoice: ExtractedInvoice,
    partner_code: str,
    verification: VerificationResult,
) -> dict[str, Any]:
    api_lines = build_api_lines(invoice.lines)
    return {
        "partner_code": partner_code,
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "currency": "JPY",
        "lines": api_lines,
        "subtotal": verification.computed_subtotal,
        "tax_amount": verification.computed_tax,
        "total_amount": verification.computed_total,
    }
