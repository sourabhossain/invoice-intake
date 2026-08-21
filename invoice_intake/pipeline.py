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


def _verification_json(verification: VerificationResult) -> dict:
    return {
        "passed": verification.passed,
        "issues": [
            {"severity": issue.severity, "code": issue.code, "message": issue.message}
            for issue in verification.issues
        ],
        "computed": {
            "subtotal": verification.computed_subtotal,
            "tax_amount": verification.computed_tax,
            "total_amount": verification.computed_total,
        },
    }


def _write_invoice_json(
    output_dir: Path,
    stem: str,
    extracted: ExtractedInvoice,
    partner_code: str | None,
    match_reason: str,
    verification: VerificationResult,
    status: str,
    review_reasons: list[str],
) -> None:
    (output_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "source_file": extracted.source_file,
                "status": status,
                "review_reasons": review_reasons,
                "partner_code": partner_code,
                "match_reason": match_reason,
                "normalized": {
                    "invoice_number": extracted.invoice_number,
                    "issue_date": extracted.issue_date,
                    "due_date": extracted.due_date,
                },
                "extracted": extracted.raw,
                "verification": _verification_json(verification),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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
        removed = client.clear_invoices()
        print(f"Cleared {removed} previously registered invoice(s).")

    partners = client.get_partners()

    # Detect duplicates before POSTing rather than relying on the API to reject
    # them: a duplicate resend is the failure mode the client actually asked
    # about, and it should be reported as caught, not as an integration error.
    already_registered = {
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
            extracted = extractor.extract(path)
            verification = verify_extraction(extracted)
            partner_code, match_reason, match_needs_review = match_partner(
                partners,
                extracted.supplier_name,
                extracted.registration_no,
            )

            review_reasons = [
                f"{issue.code}: {issue.message}"
                for issue in verification.issues
                if issue.severity == "error"
            ]

            def record(status: str, error: str | None = None, accounting_id: str | None = None):
                _write_invoice_json(
                    settings.output_dir,
                    path.stem,
                    extracted,
                    partner_code,
                    match_reason,
                    verification,
                    status,
                    review_reasons,
                )
                results.append(
                    RegistrationResult(
                        source_file=path.name,
                        invoice_number=extracted.invoice_number,
                        partner_code=partner_code,
                        status=status,
                        accounting_id=accounting_id,
                        error=error,
                        verification=verification,
                        match_reason=match_reason,
                        review_reasons=list(review_reasons),
                    )
                )

            if not partner_code:
                review_reasons.insert(
                    0, f"PARTNER_NOT_FOUND: '{extracted.supplier_name}' ({match_reason})"
                )
                print(f"  REVIEW: no partner match for '{extracted.supplier_name}'")
                record("needs_review", error="PARTNER_NOT_FOUND")
                continue

            if match_needs_review:
                review_reasons.insert(
                    0,
                    f"PARTNER_MATCH_UNCERTAIN: '{extracted.supplier_name}' -> "
                    f"{partner_code} via {match_reason}",
                )

            key = (partner_code, extracted.invoice_number)
            # Only trust a duplicate verdict when we are sure of the payee; an
            # unconfirmed partner guess would make the key meaningless.
            if not match_needs_review and key in already_registered:
                print(
                    f"  DUPLICATE: {extracted.invoice_number} is already registered "
                    f"for {partner_code} — not posted"
                )
                record("duplicate", error="DUPLICATE_INVOICE")
                continue

            if review_reasons:
                print("  REVIEW: held for human check")
                for issue in verification.issues:
                    print(f"    [{issue.severity}] {issue.code}: {issue.message}")
                record("needs_review", error=review_reasons[0].split(":", 1)[0])
                continue

            for issue in verification.issues:
                print(f"    [{issue.severity}] {issue.code}: {issue.message}")

            payload = to_api_payload(extracted, partner_code, verification)
            print(
                f"  OK extract: {extracted.invoice_number} "
                f"({partner_code}, {match_reason}) "
                f"{extracted.issue_date} total={verification.computed_total:,} JPY"
            )

            if dry_run:
                record("skipped", error="DRY_RUN")
                continue

            response = client.register_invoice(payload)
            if response.get("success"):
                accounting_id = response["data"]["accounting_id"]
                already_registered.add(key)
                print(f"  REGISTERED: {accounting_id}")
                record("registered", accounting_id=accounting_id)
            else:
                error = response.get("error", {}) or {}
                code = error.get("code", "UNKNOWN")
                message = error.get("message", "Unknown error")
                if code == "DUPLICATE_INVOICE":
                    print(f"  DUPLICATE: {message}")
                    record("duplicate", error=code)
                else:
                    print(f"  FAILED: {code} — {message} {error.get('details') or ''}")
                    record("failed", error=f"{code}: {message}")

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
    for status in ("registered", "duplicate", "needs_review", "skipped", "failed"):
        if counts.get(status):
            print(f"  {status:13} {counts[status]}")

    review = [r for r in results if r.status in {"needs_review", "duplicate"}]
    if review:
        print("\n=== Human review queue ===")
        for result in review:
            print(f"  {result.source_file} [{result.status}] {result.error}")
            for reason in result.review_reasons:
                print(f"      - {reason}")

    print(f"\nDetails saved to {output_dir}/")
