from __future__ import annotations

import json
from pathlib import Path

from invoice_intake.api_client import AccountingClient
from invoice_intake.config import Settings
from invoice_intake.extract import InvoiceExtractor
from invoice_intake.models import RegistrationResult
from invoice_intake.partner import match_partner
from invoice_intake.validate import to_api_payload, verify_extraction


def process_invoices(
    settings: Settings,
    *,
    reset: bool = False,
    dry_run: bool = False,
) -> list[RegistrationResult]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    client = AccountingClient(settings.api_url, settings.api_key)
    client.health()

    if reset and not dry_run:
        removed = client.clear_invoices()
        print(f"Cleared {removed} previously registered invoice(s).")

    partners = client.get_partners()
    extractor = InvoiceExtractor(settings.openai_api_key, settings.openai_model)

    invoice_paths = sorted(settings.invoices_dir.glob("invoice_*.*"))
    results: list[RegistrationResult] = []

    for path in invoice_paths:
        print(f"\n--- {path.name} ---")
        try:
            extracted = extractor.extract(path)
            verification = verify_extraction(extracted)
            partner_code, match_reason = match_partner(
                partners,
                extracted.supplier_name,
                extracted.registration_no,
            )

            output_path = settings.output_dir / f"{path.stem}.json"
            output_path.write_text(
                json.dumps(
                    {
                        "extracted": extracted.raw,
                        "source_file": extracted.source_file,
                        "partner_code": partner_code,
                        "match_reason": match_reason,
                        "verification": {
                            "passed": verification.passed,
                            "issues": [
                                {
                                    "severity": issue.severity,
                                    "code": issue.code,
                                    "message": issue.message,
                                }
                                for issue in verification.issues
                            ],
                            "computed": {
                                "subtotal": verification.computed_subtotal,
                                "tax_amount": verification.computed_tax,
                                "total_amount": verification.computed_total,
                            },
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            if not partner_code:
                print(f"  SKIP: no partner match for '{extracted.supplier_name}'")
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=None,
                        status="skipped",
                        error="PARTNER_NOT_FOUND",
                        verification=verification,
                    )
                )
                continue

            if not verification.passed:
                print("  SKIP: verification failed")
                for issue in verification.issues:
                    print(f"    [{issue.severity}] {issue.message}")
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=partner_code,
                        status="skipped",
                        error="VERIFICATION_FAILED",
                        verification=verification,
                    )
                )
                continue

            payload = to_api_payload(extracted, partner_code, verification)
            print(
                f"  OK extract: {extracted.invoice_number} "
                f"({partner_code}, {match_reason}) "
                f"total={verification.computed_total:,} JPY"
            )

            if dry_run:
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=partner_code,
                        status="skipped",
                        error="DRY_RUN",
                        verification=verification,
                    )
                )
                continue

            response = client.register_invoice(payload)
            if response.get("success"):
                accounting_id = response["data"]["accounting_id"]
                print(f"  REGISTERED: {accounting_id}")
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=partner_code,
                        status="registered",
                        accounting_id=accounting_id,
                        verification=verification,
                    )
                )
            else:
                error = response.get("error", {})
                code = error.get("code", "UNKNOWN")
                message = error.get("message", "Unknown error")
                print(f"  FAILED: {code} — {message}")
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=partner_code,
                        status="failed",
                        error=code,
                        verification=verification,
                    )
                )

        except Exception as exc:
            print(f"  ERROR: {exc}")
            results.append(
                RegistrationResult(
                    source_file=path.name,
                    invoice_number="",
                    partner_code=None,
                    status="failed",
                    error=str(exc),
                )
            )

    summary_path = settings.output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "source_file": r.source_file,
                    "invoice_number": r.invoice_number,
                    "partner_code": r.partner_code,
                    "status": r.status,
                    "accounting_id": r.accounting_id,
                    "error": r.error,
                }
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    registered = sum(1 for r in results if r.status == "registered")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    print(f"\n=== Done: {registered} registered, {skipped} skipped, {failed} failed ===")
    print(f"Details saved to {settings.output_dir}/")

    return results
