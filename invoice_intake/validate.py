from __future__ import annotations

import math
from typing import Any

from invoice_intake.models import ExtractedInvoice, ExtractedLine, VerificationIssue, VerificationResult
from invoice_intake.partner import tax_rate_to_code


TAX_RATES = {"T10": 0.10, "T08": 0.08}


def compute_amounts(lines: list[dict[str, Any]]) -> tuple[int, int, int]:
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


def verify_extraction(invoice: ExtractedInvoice) -> VerificationResult:
    issues: list[VerificationIssue] = []

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

    api_lines = build_api_lines(invoice.lines)
    computed_subtotal, computed_tax, computed_total = compute_amounts(api_lines)

    if invoice.subtotal != computed_subtotal:
        issues.append(
            VerificationIssue(
                severity="error",
                code="SUBTOTAL_MISMATCH",
                message=(
                    f"Extracted subtotal {invoice.subtotal} != "
                    f"computed {computed_subtotal}"
                ),
            )
        )

    if invoice.tax_amount != computed_tax:
        issues.append(
            VerificationIssue(
                severity="error",
                code="TAX_MISMATCH",
                message=(
                    f"Extracted tax {invoice.tax_amount} != computed {computed_tax}"
                ),
            )
        )

    if invoice.total_amount != computed_total:
        issues.append(
            VerificationIssue(
                severity="error",
                code="TOTAL_MISMATCH",
                message=(
                    f"Extracted total {invoice.total_amount} != "
                    f"computed {computed_total}"
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
