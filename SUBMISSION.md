# Submission

- Name:
- Submission date (YYYY-MM-DD):
- Hours actually spent:
- Repository / how to run it: `python3 run.py --reset` (see README.md)

## 1. Understanding the request

The client described manual invoice data entry causing month-end overtime and duplicate-payment risk from typos. They asked for AI to read heterogeneous invoices (PDF, scans, handwriting) and enter them into the existing accounting system via API.

I set out to build an **end-to-end intake pipeline** that:
1. Extracts structured invoice data from 12 sample documents using vision LLM
2. **Verifies** amounts locally before trusting the AI output
3. Matches suppliers to the partner master
4. Registers valid invoices via the accounting API

The core problem is not just OCR — it is **trustworthy automation**: reducing manual entry while preventing incorrect registrations.

## 2. What you would have asked the client

| What you wanted to ask | The assumption you made | Why |
|---|---|---|
| What happens when extraction confidence is low? | Skip registration and log for human review | Safer than posting wrong data; API has strict validation |
| Should we register invoices with mixed tax rates on one document? | Support per-line tax codes (T10/T08) | Japanese invoices commonly have 10% and 8% lines |
| Is a review UI required for v1? | CLI + JSON output is enough for demo | 8-hour scope; review UI is optional differentiator |
| How are invoices received (email, folder, ERP)? | Batch folder of files | Matches sample data layout |
| What if supplier is not in partner master? | Skip and report PARTNER_NOT_FOUND | API rejects unknown partners anyway |
| Should we trust invoice-printed totals or recalculate? | Recalculate from lines using API tax rules | API returns AMOUNT_MISMATCH if totals differ |

## 3. Scoping decisions

**What you built**

- LLM vision extraction for PDF and scanned JPG invoices
- Amount verification (line math + API-compatible tax calculation)
- Partner matching by registration number and name/aliases
- Auto-start accounting API + single-command pipeline (`python3 run.py`)
- Per-invoice JSON output and summary for audit

**What you left out, and why**

- Human review web UI — time; JSON output + skip-on-failure is sufficient for demo
- Low-confidence scoring thresholds — used binary pass/fail verification instead
- Email ingestion / queue — out of scope for sample folder
- Detailed cost dashboard — documented in section 7 instead

## 4. Design and technology choices

**Flow:** `invoices/` → render PDF pages as images → GPT-4o structured JSON extraction → local verification → partner match → POST `/invoices`.

**Chose:**
- **Python** — fast integration, good PDF/image libraries
- **OpenAI GPT-4o** — strong Japanese document understanding, native vision + JSON schema
- **PyMuPDF** — render scanned PDFs to images without extra dependencies
- **httpx** — simple API client

**Decided against:**
- Traditional OCR-only (Tesseract) — weak on layout variation and handwriting
- Sending extracted totals directly to API — API recalculates and rejects mismatches
- Changing the mock API — assignment forbids it

## 5. How you used AI, and how you checked it

**What you delegated to AI**

- Reading invoice images/PDFs and extracting fields (supplier, dates, line items, amounts)
- Handling varied layouts and Japanese labels

**How you verified the output**

Nothing the model returns is trusted on its own. Each check below is recomputed
locally from the extraction, and any failure holds the invoice out of the API.

*Amounts* — recomputed from the line items, never taken from the printed totals:
1. **Line math:** `quantity × unit_price == amount` when both present (warning)
2. **Subtotal:** sum of line amounts
3. **Tax:** per tax code, `floor(subtotal × rate)` — the same arithmetic as the API
4. **Total:** subtotal + tax
5. **Internal consistency:** the three figures read off the page agree with each other

*Dates* — a wrong date passes every arithmetic check, so it needs its own checks:
6. Both dates parse to a real `YYYY-MM-DD` (令和/平成/昭和 and `R8.2.5` included)
7. `due_date` is not before `issue_date`
8. `issue_date` falls in a plausibility window (730 days back / 30 forward)
9. Cross-check against any date embedded in the invoice number (warning)

*Payee and payment integrity:*
10. Match by 登録番号 → exact name → substring of ≥4 chars; ties and shorter
    overlaps are refused rather than guessed
11. Near-miss names become a reviewer *suggestion*, never an automatic match
12. Hand-altered bank details hold the invoice regardless of the amounts
13. Duplicates are detected locally, per partner, before POSTing

An invoice reaches the accounting API only if all of the above pass; everything
else goes to the review queue with its reasons. Checks are unit-tested against
the failure modes that previously crashed the run (0% tax lines, unparseable
dates, empty line lists).

**A case where the AI got it wrong**

Three, and they are instructive because they fail in different ways.

1. **`invoice_11.jpg` — era-year misread, invisible to arithmetic.** The paper reads
   `令和8年2月5日` (2026-02-05). Across runs GPT-4o read the era year as 5 and as 2,
   registering the invoice as **2023-02-05** and **2020-02-05**. Every amount check
   passed cleanly, because a wrong date has no effect on arithmetic. This is the
   most dangerous class of error: confidently wrong, silently accepted. Fixed by
   checking the issue date against a plausibility window and cross-checking it
   against the date embedded in the invoice number (`SATO-260205` → 2026-02).

2. **`invoice_03.pdf` — mixed tax rates, partially read.** The invoice has both 8%
   and 10% lines with two separate 消費税 rows (3,950 + 6,067 = 10,017). The model
   captured only the 8% row as the tax total, and separately misread the supplier
   as 東京**ア**ーズ株式会社 (東京**フ**ーズ) while dropping the 登録番号. Caught by
   recomputing tax from the line-level rates; the misread name now produces a
   fuzzy suggestion (P-1003, 0.80) for the reviewer rather than a dead end.

3. **`invoice_09.pdf` — the AI was right and the invoice was wrong.** Extraction is
   exact: 小計 134,088, 消費税 13,408, 合計 147,497. But 134,088 + 13,408 = 147,**496**.
   The supplier's own total is off by ¥1. Worth separating from the cases above,
   because "re-scan the document" is the wrong remedy — someone has to ask the
   supplier which figure is correct.

The general lesson: verifying only the numbers the AI is *asked* to add up leaves
whole categories of error unguarded. Dates, payee identity and payment details
needed their own checks.

**A case that is not an AI error but must still be blocked**

`invoice_08.jpg` carries a handwritten red-ink change to the bank account (振込先
`1234567` → `...5`) and a 至急 (urgent) mark. The model read it correctly and every
amount reconciles, so an amounts-only pipeline registers it without comment. That
is precisely the invoice-redirection fraud pattern, so any hand alteration to
transfer details now holds the invoice unconditionally.

## 6. Integrating with the accounting system

6 of 12 registered automatically, 1 duplicate blocked, 5 held for human review.
No invoice was registered with data I could not verify against the document.

| Invoice | Result | How you handled it |
|---|---|---|
| invoice_01.pdf | registered `ACC-0001` | Matched P-1001 by 登録番号; amounts reconciled |
| invoice_02.pdf | registered `ACC-0002` | 26 line items, all 10%; amounts reconciled |
| invoice_03.pdf | **needs review** | Supplier misread (東京アーズ) → fuzzy suggestion P-1003; only one of two 消費税 rows captured, so tax 6,067 ≠ recomputed 10,017 |
| invoice_04.jpg | registered `ACC-0003` | Exact name match on 有限会社佐藤商店 |
| invoice_05.jpg | registered `ACC-0004` | Exact name match on P-1005 |
| invoice_06.jpg | registered `ACC-0005` | Supplier printed as ヤマダ製作所; matched P-1001 by 登録番号 |
| invoice_07.jpg | **duplicate blocked** | Scanned resend of invoice_01 (same YM-2026-0107); caught locally before POST, not sent |
| invoice_08.jpg | **needs review** | Handwritten bank-account change; amounts fine, payee integrity is not — held |
| invoice_09.pdf | **needs review** | Extraction correct; the invoice's own total is ¥1 above subtotal + tax |
| invoice_10.jpg | **needs review** | 新星ロジスティクス株式会社 is not in the partner master and is not a near miss of any entry |
| invoice_11.jpg | **needs review** | 令和8年 read as 令和5年/令和2年 → date 3–6 years off; caught by the date window and the invoice-number cross-check |
| invoice_12.jpg | registered `ACC-0006` | Exact name match on P-1005 |

**On the API's constraints.** The API recalculates every amount from the lines and
rejects mismatches, so the pipeline sends its own recomputation rather than the
printed totals, and applies the same `floor(subtotal × rate)` per tax code. The two
error codes I chose to pre-empt locally rather than discover by POSTing are
`DUPLICATE_INVOICE` and `DUE_DATE_BEFORE_ISSUE_DATE`: a rejected POST tells you
something is wrong but leaves the operator to work out what, and a duplicate resend
is a business outcome to report, not an integration error to retry.

Re-running the pipeline against a live API registers nothing twice — verified by
running the same invoice through two consecutive runs without `--reset`.

## 7. Cost, limits, and risk in production

- **Cost per invoice:** ~$0.02–0.05 (1–2 GPT-4o vision calls depending on PDF pages)
- **Monthly cost at 1,000 invoices/month:** ~$20–50 LLM + negligible compute
- **Processing time per invoice:** ~5–15 seconds (LLM latency dominates)
- **Where this breaks first:** handwritten annotations, unknown suppliers, multi-page complex tables, duplicate submissions
- **How you would find out if something was registered incorrectly:** reconciliation report comparing extracted JSON vs API records; spot-check invoices flagged with verification warnings

## 8. What you would do with another 8 hours

1. **Human review UI** — let accounting staff correct extractions before POST
2. **Confidence scoring** — route low-confidence extractions to review queue
3. **Idempotency + monitoring** — detect duplicates, alert on registration failures, daily reconciliation dashboard
