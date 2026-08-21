from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import pymupdf as fitz
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from invoice_intake.models import ExtractedInvoice, ExtractedLine
from invoice_intake.partner import normalize_date

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string"},
        "registration_no": {"type": ["string", "null"]},
        "invoice_number": {"type": "string"},
        "issue_date": {"type": "string"},
        "due_date": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["integer", "null"]},
                    "unit": {"type": "string"},
                    "unit_price": {"type": ["integer", "null"]},
                    "amount": {"type": "integer"},
                    "tax_rate_percent": {"type": "integer"},
                },
                "required": [
                    "description",
                    "quantity",
                    "unit",
                    "unit_price",
                    "amount",
                    "tax_rate_percent",
                ],
                "additionalProperties": False,
            },
        },
        "subtotal": {"type": "integer"},
        "tax_amount": {"type": "integer"},
        "total_amount": {"type": "integer"},
        "payment_details_altered": {"type": "boolean"},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "supplier_name",
        "registration_no",
        "invoice_number",
        "issue_date",
        "due_date",
        "lines",
        "subtotal",
        "tax_amount",
        "total_amount",
        "payment_details_altered",
        "confidence_notes",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract structured data from Japanese business invoices (請求書).

Rules:
- Return integer yen amounts only (no decimals, no currency symbols).
- Discounts shown with △ or minus are negative integers (e.g. △30,000 -> -30000).
- Dates may use YYYY/MM/DD, YYYY-MM-DD, YYYY年M月D日, or 令和N年M月D日 — return as printed.
- tax_rate_percent must be 10 or 8 for each line item (use the rate shown per line).
- If quantity or unit price is missing on the invoice, use null.
- supplier_name is the issuer/seller (請求元), not the recipient (御中).
- registration_no is the 登録番号 (Qualified Invoice System number) if visible.
- description and unit must be non-empty; use "式" when no unit is printed.
- Include every billable line item; exclude subtotal/tax/total rows from lines.
- For multi-page PDFs, combine all pages into one invoice.
- Keep era years exactly as printed (令和N年). Do not convert to a Western year
  and do not infer the era year from context.
- payment_details_altered: true if the bank transfer details (振込先/口座) carry
  any handwritten, stamped, struck-through or differently-coloured change.
- confidence_notes: name anything handwritten or unclear, and any digit you were
  unsure of.
"""

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0


def _render_pdf_pages(path: Path, dpi: int = 200) -> list[bytes]:
    doc = fitz.open(path)
    images: list[bytes] = []
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def _load_images(path: Path) -> list[tuple[str, bytes]]:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png"}:
        return [(path.name, path.read_bytes())]
    if suffix == ".pdf":
        return [(f"{path.name}#page{i+1}", data) for i, data in enumerate(_render_pdf_pages(path))]
    raise ValueError(f"Unsupported file type: {path}")


def _image_content(image_bytes: bytes, media_type: str = "image/png") -> dict[str, Any]:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
    }


def _parse_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid amount")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace("△", "-")
        cleaned = re.sub(r"[^\d-]", "", cleaned)
        if not cleaned:
            raise ValueError(f"Cannot parse integer from {value!r}")
        return int(cleaned)
    raise ValueError(f"Cannot parse integer from {value!r}")


def _parse_payload(raw: dict[str, Any], source_file: str) -> ExtractedInvoice:
    lines = [
        ExtractedLine(
            description=str(item["description"]).strip(),
            quantity=item["quantity"] if item["quantity"] is not None else None,
            unit=str(item["unit"]).strip(),
            unit_price=item["unit_price"] if item["unit_price"] is not None else None,
            amount=_parse_int(item["amount"]),
            tax_rate_percent=_parse_int(item["tax_rate_percent"]),
        )
        for item in raw["lines"]
    ]

    return ExtractedInvoice(
        source_file=source_file,
        supplier_name=str(raw["supplier_name"]).strip(),
        registration_no=raw.get("registration_no"),
        invoice_number=str(raw["invoice_number"]).strip(),
        issue_date=normalize_date(str(raw["issue_date"])),
        due_date=normalize_date(str(raw["due_date"])),
        lines=lines,
        subtotal=_parse_int(raw["subtotal"]),
        tax_amount=_parse_int(raw["tax_amount"]),
        total_amount=_parse_int(raw["total_amount"]),
        payment_details_altered=bool(raw.get("payment_details_altered", False)),
        confidence_notes=str(raw.get("confidence_notes", "")).strip(),
        raw=raw,
    )


class InvoiceExtractor:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def extract(self, path: Path) -> ExtractedInvoice:
        images = _load_images(path)
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Extract invoice data from this file: {path.name}. "
                    "If multiple pages, combine into one invoice."
                ),
            }
        ]
        for label, image_bytes in images:
            user_content.append({"type": "text", "text": f"Page: {label}"})
            media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            user_content.append(_image_content(image_bytes, media_type=media_type))

        content = self._complete_with_retry(user_content, path.name)
        raw = json.loads(content)
        return _parse_payload(raw, path.name)

    def _complete_with_retry(self, user_content: list[dict[str, Any]], label: str) -> str:
        last_error: Exception | None = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "invoice_extraction",
                            "strict": True,
                            "schema": EXTRACTION_SCHEMA,
                        },
                    },
                    temperature=0,
                )
                content = response.choices[0].message.content
                if not content:
                    raise RuntimeError(f"Empty extraction result for {label}")
                return content
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                last_error = exc
            except APIStatusError as exc:
                # Auth, quota and bad-request failures will not fix themselves.
                if exc.status_code < 500:
                    raise
                last_error = exc

            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                print(
                    f"  {type(last_error).__name__}, retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{RETRY_ATTEMPTS})"
                )
                time.sleep(delay)

        raise RuntimeError(f"Extraction failed for {label} after {RETRY_ATTEMPTS} attempts: {last_error}")
