from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedLine:
    description: str
    quantity: int | None
    unit: str
    unit_price: int | None
    amount: int
    tax_rate_percent: int  # 10 or 8


@dataclass
class ExtractedInvoice:
    source_file: str
    supplier_name: str
    registration_no: str | None
    invoice_number: str
    issue_date: str
    due_date: str
    lines: list[ExtractedLine]
    subtotal: int
    tax_amount: int
    total_amount: int
    confidence_notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str


@dataclass
class VerificationResult:
    passed: bool
    issues: list[VerificationIssue]
    computed_subtotal: int
    computed_tax: int
    computed_total: int


@dataclass
class RegistrationResult:
    source_file: str
    invoice_number: str
    partner_code: str | None
    status: str  # "registered" | "skipped" | "failed"
    accounting_id: str | None = None
    error: str | None = None
    verification: VerificationResult | None = None
