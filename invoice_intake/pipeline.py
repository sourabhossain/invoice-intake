from __future__ import annotations

import json
from pathlib import Path

from invoice_intake.api_client import AccountingClient
from invoice_intake.config import Settings
from invoice_intake.extract import InvoiceExtractor
from invoice_intake.models import ExtractedInvoice, RegistrationResult, VerificationResult
from invoice_intake.partner import match_partner
from invoice_intake.validate import to_api_payload, verify_extraction

SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png"}
STATUS_ORDER = ("registered", "duplicate", "needs_review", "skipped", "failed")


def _write_invoice_json(
    path: Path,
    extracted: ExtractedInvoice,
    verification: VerificationResult,
    result: RegistrationResult,
) -> None:
    path.write_text(
        json.dumps(
            {
                "source_file": result.source_file,
                "status": result.status,
                "review_reasons": result.review_reasons,
                "partner_code": result.partner_code,
                "match_reason": result.match_reason,
                "accounting_id": result.accounting_id,
                "normalized": {
                    "invoice_number": extracted.invoice_number,
                    "issue_date": extracted.issue_date,
                    "due_date": extracted.due_date,
                },
                "extracted": extracted.raw,
                "verification": {
                    "passed": verification.passed,
                    "issues": [
                        {"severity": i.severity, "code": i.code, "message": i.message}
                        for i in verification.issues
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


def _print_warnings(verification: VerificationResult) -> None:
    for issue in verification.issues:
        if issue.severity != "error":
            print(f"    [{issue.severity}] {issue.code}: {issue.message}")


def _process_one(
    path: Path,
    extractor: InvoiceExtractor,
    client: AccountingClient,
    partners: list[dict],
    registered_keys: set[tuple[str, str]],
    output_dir: Path,
    dry_run: bool,
) -> RegistrationResult:
    extracted = extractor.extract(path)
    verification = verify_extraction(extracted)
    partner_code, match_reason, match_needs_review = match_partner(
        partners, extracted.supplier_name, extracted.registration_no
    )

    review_reasons = [
        f"{issue.code}: {issue.message}"
        for issue in verification.issues
        if issue.severity == "error"
    ]

    result = RegistrationResult(
        source_file=path.name,
        invoice_number=extracted.invoice_number,
        partner_code=partner_code,
        status="needs_review",
        verification=verification,
        match_reason=match_reason,
    )

    if not partner_code:
        review_reasons.insert(
            0, f"PARTNER_NOT_FOUND: '{extracted.supplier_name}' ({match_reason})"
        )
        result.error = "PARTNER_NOT_FOUND"
        print(f"  REVIEW: no partner match for '{extracted.supplier_name}'")
        for reason in review_reasons[1:]:
            print(f"    [error] {reason}")
        _print_warnings(verification)
    else:
        if match_needs_review:
            review_reasons.insert(
                0,
                f"PARTNER_MATCH_UNCERTAIN: '{extracted.supplier_name}' -> "
                f"{partner_code} via {match_reason}",
            )

        key = (partner_code, extracted.invoice_number)
        # A duplicate verdict is only meaningful once the payee is confirmed.
        if not match_needs_review and key in registered_keys:
            result.status = "duplicate"
            result.error = "DUPLICATE_INVOICE"
            print(
                f"  DUPLICATE: {extracted.invoice_number} is already registered "
                f"for {partner_code} — not posted"
            )
        elif review_reasons:
            result.error = review_reasons[0].split(":", 1)[0]
            print("  REVIEW: held for human check")
            for reason in review_reasons:
                print(f"    [error] {reason}")
            _print_warnings(verification)
        else:
            _print_warnings(verification)
            print(
                f"  OK extract: {extracted.invoice_number} ({partner_code}, {match_reason}) "
                f"{extracted.issue_date} total={verification.computed_total:,} JPY"
            )
            if dry_run:
                result.status = "skipped"
                result.error = "DRY_RUN"
                registered_keys.add(key)
            else:
                _register(client, extracted, partner_code, verification, key, registered_keys, result)

    result.review_reasons = review_reasons
    _write_invoice_json(output_dir / f"{path.stem}.json", extracted, verification, result)
    return result


def _register(
    client: AccountingClient,
    extracted: ExtractedInvoice,
    partner_code: str,
    verification: VerificationResult,
    key: tuple[str, str],
    registered_keys: set[tuple[str, str]],
    result: RegistrationResult,
) -> None:
    response = client.register_invoice(to_api_payload(extracted, partner_code, verification))

    if response.get("success"):
        result.status = "registered"
        result.accounting_id = response["data"]["accounting_id"]
        registered_keys.add(key)
        print(f"  REGISTERED: {result.accounting_id}")
        return

    error = response.get("error") or {}
    code = error.get("code", "UNKNOWN")
    message = error.get("message", "Unknown error")
    if code == "DUPLICATE_INVOICE":
        result.status = "duplicate"
        result.error = code
        registered_keys.add(key)
        print(f"  DUPLICATE: {message}")
    else:
        result.status = "failed"
        result.error = f"{code}: {message}"
        print(f"  FAILED: {code} — {message} {error.get('details') or ''}")


def process_invoices(
    settings: Settings,
    *,
    reset: bool = False,
    dry_run: bool = False,
    only: str | None = None,
) -> list[RegistrationResult]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    client = AccountingClient(settings.api_url, settings.api_key)
    client.health()

    if reset and not dry_run:
        print(f"Cleared {client.clear_invoices()} previously registered invoice(s).")

    partners = client.get_partners()

    # A resend is a business outcome to report, not a rejected POST to explain.
    registered_keys = {
        (record["partner_code"], record["invoice_number"]) for record in client.list_invoices()
    }

    extractor = InvoiceExtractor(settings.openai_api_key, settings.openai_model)

    invoice_paths = sorted(
        path
        for path in settings.invoices_dir.glob("invoice_*.*")
        if path.suffix.lower() in SUPPORTED_SUFFIXES and (only is None or only in path.name)
    )
    if not invoice_paths:
        raise RuntimeError(f"No invoices to process in {settings.invoices_dir}")

    results: list[RegistrationResult] = []
    for path in invoice_paths:
        print(f"\n--- {path.name} ---")
        try:
            results.append(
                _process_one(
                    path,
                    extractor,
                    client,
                    partners,
                    registered_keys,
                    settings.output_dir,
                    dry_run,
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

    _write_summary(settings.output_dir, results)
    _print_report(results, settings.output_dir)
    return results


def _write_summary(output_dir: Path, results: list[RegistrationResult]) -> None:
    (output_dir / "summary.json").write_text(
        json.dumps(
            [
                {
                    "source_file": r.source_file,
                    "invoice_number": r.invoice_number,
                    "partner_code": r.partner_code,
                    "match_reason": r.match_reason,
                    "status": r.status,
                    "accounting_id": r.accounting_id,
                    "error": r.error,
                    "review_reasons": r.review_reasons,
                }
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _print_report(results: list[RegistrationResult], output_dir: Path) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    print("\n=== Result ===")
    for status in STATUS_ORDER:
        if counts.get(status):
            print(f"  {status:13} {counts[status]}")

    held = [r for r in results if r.status in {"needs_review", "duplicate", "failed"}]
    if held:
        print("\n=== Not registered ===")
        for result in held:
            print(f"  {result.source_file} [{result.status}] {result.error}")
            for reason in result.review_reasons:
                print(f"      - {reason}")

    print(f"\nDetails saved to {output_dir}/")
