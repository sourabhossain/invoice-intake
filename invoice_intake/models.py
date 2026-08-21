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
    # registered    — posted to the accounting API
    # duplicate     — already registered for this partner; deliberately not posted
    # needs_review  — extraction is questionable; held for a human, not posted
    # skipped       — dry run
    # failed        — the pipeline itself broke (LLM/API/IO error)
    status: str
    accounting_id: str | None = None
    error: str | None = None
    verification: VerificationResult | None = None
    match_reason: str | None = None
    review_reasons: list[str] = field(default_factory=list)
