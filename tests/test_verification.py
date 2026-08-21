"""Checks on the verification layer, run with: python3 -m unittest discover tests

Covers the failure modes that must never reach the accounting API, including the
three that were silently registered or crashed the run before.
"""

from __future__ import annotations

import unittest
from datetime import date

from invoice_intake.models import ExtractedInvoice, ExtractedLine
from invoice_intake.partner import match_partner, normalize_date, tax_rate_to_code
from invoice_intake.validate import verify_extraction

TODAY = date(2026, 8, 21)

PARTNERS = [
    {
        "partner_code": "P-1001",
        "name": "株式会社山田製作所",
        "aliases": ["ヤマダ製作所", "山田製作所"],
        "registration_no": "T1010001000101",
    },
    {
        "partner_code": "P-1003",
        "name": "東京フーズ株式会社",
        "aliases": ["東京フーズ"],
        "registration_no": "T3030003000303",
    },
    {
        "partner_code": "P-1005",
        "name": "みらいITソリューションズ株式会社",
        "aliases": ["みらいIT", "みらいITソリューションズ"],
        "registration_no": "T5050005000505",
    },
]


def line(
    amount: int,
    rate: int = 10,
    qty: int | None = None,
    price: int | None = None,
    description: str = "品目",
    unit: str = "式",
):
    return ExtractedLine(description, qty, unit, price, amount, rate)


def invoice(
    lines=None,
    subtotal=100_000,
    tax=10_000,
    total=110_000,
    issue="2026-02-05",
    due="2026-03-31",
    notes="",
    number="INV-260205",
    altered=False,
):
    return ExtractedInvoice(
        source_file="t.pdf",
        supplier_name="株式会社山田製作所",
        registration_no="T1010001000101",
        invoice_number=number,
        issue_date=issue,
        due_date=due,
        lines=lines if lines is not None else [line(100_000)],
        subtotal=subtotal,
        tax_amount=tax,
        total_amount=total,
        payment_details_altered=altered,
        confidence_notes=notes,
        raw={},
    )


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


class AmountChecks(unittest.TestCase):
    def test_consistent_invoice_passes(self):
        result = verify_extraction(invoice(), today=TODAY)
        self.assertTrue(result.passed, codes(result))
        self.assertEqual(
            (result.computed_subtotal, result.computed_tax, result.computed_total),
            (100_000, 10_000, 110_000),
        )

    def test_mixed_tax_rates_are_floored_per_code(self):
        # 103,200 @ 8% -> 8,256 and 6,800 @ 10% -> 680, as invoice_08 prints.
        result = verify_extraction(
            invoice(
                lines=[line(103_200, rate=8), line(6_800, rate=10)],
                subtotal=110_000,
                tax=8_936,
                total=118_936,
            ),
            today=TODAY,
        )
        self.assertTrue(result.passed, codes(result))
        self.assertEqual(result.computed_tax, 8_936)

    def test_misread_line_amount_is_caught(self):
        result = verify_extraction(
            invoice(lines=[line(90_000)], subtotal=100_000), today=TODAY
        )
        self.assertFalse(result.passed)
        self.assertIn("SUBTOTAL_MISMATCH", codes(result))

    def test_partial_tax_row_is_caught(self):
        """invoice_03: only one of two 消費税 rows was captured."""
        result = verify_extraction(
            invoice(
                lines=[line(75_840, rate=8), line(39_500, rate=10)],
                subtotal=115_340,
                tax=6_067,
                total=125_357,
            ),
            today=TODAY,
        )
        self.assertFalse(result.passed)
        self.assertIn("TAX_MISMATCH", codes(result))
        self.assertIn("EXTRACTED_TOTALS_INCONSISTENT", codes(result))

    def test_off_by_one_printed_total_is_caught(self):
        """invoice_09: extraction correct, the invoice's own total is ¥1 out."""
        result = verify_extraction(
            invoice(
                lines=[line(101_121), line(32_967)],
                subtotal=134_088,
                tax=13_408,
                total=147_497,
            ),
            today=TODAY,
        )
        self.assertFalse(result.passed)
        self.assertIn("TOTAL_MISMATCH", codes(result))

    def test_line_math_is_a_warning_not_a_block(self):
        result = verify_extraction(
            invoice(lines=[line(100_000, qty=3, price=30_000)]), today=TODAY
        )
        self.assertTrue(result.passed, codes(result))
        self.assertIn("LINE_MATH", codes(result))

    def test_unsupported_tax_rate_reports_instead_of_crashing(self):
        result = verify_extraction(invoice(lines=[line(100_000, rate=0)]), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("UNSUPPORTED_TAX_RATE", codes(result))

    def test_no_line_items_reports_instead_of_crashing(self):
        result = verify_extraction(invoice(lines=[]), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("NO_LINE_ITEMS", codes(result))


class DateChecks(unittest.TestCase):
    def test_era_year_misread_is_caught(self):
        """invoice_11: 令和8年 read as 令和5年 -> 2023, all amounts still reconcile."""
        result = verify_extraction(
            invoice(issue="2023-02-05", due="2023-03-31", number="SATO-260205"),
            today=TODAY,
        )
        self.assertFalse(result.passed)
        self.assertIn("ISSUE_DATE_IMPLAUSIBLE", codes(result))
        self.assertIn("DATE_NUMBER_DISAGREEMENT", codes(result))

    def test_future_issue_date_is_caught(self):
        result = verify_extraction(invoice(issue="2027-02-05", due="2027-03-31"), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("ISSUE_DATE_IMPLAUSIBLE", codes(result))

    def test_due_before_issue_is_caught_locally(self):
        result = verify_extraction(invoice(issue="2026-03-31", due="2026-02-05"), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("DUE_DATE_BEFORE_ISSUE_DATE", codes(result))

    def test_unparseable_date_reports_instead_of_crashing(self):
        result = verify_extraction(invoice(issue="令和8年"), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("DATE_UNPARSEABLE", codes(result))

    def test_normalize_date_formats(self):
        cases = {
            "2026-02-05": "2026-02-05",
            "2026/2/5": "2026-02-05",
            "2026.02.05": "2026-02-05",
            "2026年2月5日": "2026-02-05",
            "令和8年2月5日": "2026-02-05",
            "令和元年5月1日": "2019-05-01",
            "平成31年4月30日": "2019-04-30",
            "R8.2.5": "2026-02-05",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_date(raw), expected)

    def test_unrecognised_date_is_returned_unchanged(self):
        self.assertEqual(normalize_date("来月末"), "来月末")


class PaymentIntegrityChecks(unittest.TestCase):
    def test_flag_blocks_a_clean_invoice(self):
        """invoice_08: every amount reconciles; the payee account does not."""
        result = verify_extraction(invoice(altered=True), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("PAYMENT_DETAILS_ALTERED", codes(result))

    def test_notes_block_even_when_the_flag_is_unset(self):
        for note in (
            "The bank account number (振込先) has been altered by hand.",
            "振込先が手書きで変更されている",
            "Account number overwritten in red ink",
        ):
            with self.subTest(note=note):
                result = verify_extraction(invoice(notes=note), today=TODAY)
                self.assertIn("PAYMENT_DETAILS_ALTERED", codes(result))

    def test_unrelated_handwriting_does_not_block(self):
        result = verify_extraction(
            invoice(notes="Handwritten '至急' stamp near the recipient name."), today=TODAY
        )
        self.assertTrue(result.passed, codes(result))

    def test_innocuous_wording_near_payment_terms_does_not_false_positive(self):
        for note in (
            "Bank transfer line includes a handling fee.",
            "Account holder name is pending confirmation.",
            "The bank branch is linked to the head office.",
        ):
            with self.subTest(note=note):
                result = verify_extraction(invoice(notes=note), today=TODAY)
                self.assertNotIn("PAYMENT_DETAILS_ALTERED", codes(result))


class LineFieldChecks(unittest.TestCase):
    def test_empty_unit_is_caught_before_posting(self):
        result = verify_extraction(invoice(lines=[line(100_000, unit="")]), today=TODAY)
        self.assertFalse(result.passed)
        self.assertIn("LINE_FIELD_MISSING", codes(result))

    def test_empty_description_is_caught_before_posting(self):
        result = verify_extraction(
            invoice(lines=[line(100_000, description="  ")]), today=TODAY
        )
        self.assertFalse(result.passed)
        self.assertIn("LINE_FIELD_MISSING", codes(result))


class PartnerMatching(unittest.TestCase):
    def test_registration_number_wins(self):
        code, reason, review = match_partner(PARTNERS, "まったく別の名前", "T1010001000101")
        self.assertEqual(code, "P-1001")
        self.assertFalse(review)

    def test_alias_matches_without_review(self):
        code, _, review = match_partner(PARTNERS, "ヤマダ製作所", None)
        self.assertEqual(code, "P-1001")
        self.assertFalse(review)

    def test_short_overlap_is_refused(self):
        # "みらいIT" must not bind on a two-character latin overlap alone.
        code, _, review = match_partner(PARTNERS, "IT株式会社", None)
        self.assertIsNone(code)
        self.assertTrue(review)

    def test_one_character_misread_becomes_a_review_suggestion(self):
        """invoice_03: 東京フーズ read as 東京アーズ."""
        code, reason, review = match_partner(PARTNERS, "東京アーズ株式会社", None)
        self.assertEqual(code, "P-1003")
        self.assertTrue(review, "a fuzzy suggestion must never auto-register")
        self.assertIn("fuzzy_suggestion", reason)

    def test_unknown_supplier_has_no_match(self):
        code, reason, review = match_partner(PARTNERS, "新星ロジスティクス株式会社", None)
        self.assertIsNone(code)
        self.assertEqual(reason, "no_match")
        self.assertTrue(review)

    def test_full_width_names_normalise(self):
        code, _, review = match_partner(PARTNERS, "みらいＩＴソリューションズ株式会社", None)
        self.assertEqual(code, "P-1005")
        self.assertFalse(review)

    def test_tax_rate_mapping(self):
        self.assertEqual(tax_rate_to_code(10), "T10")
        self.assertEqual(tax_rate_to_code(8), "T08")
        self.assertIsNone(tax_rate_to_code(0))


if __name__ == "__main__":
    unittest.main()
