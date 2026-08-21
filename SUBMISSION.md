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

1. **Line math:** `quantity × unit_price == amount` when both present
2. **Subtotal check:** sum of line amounts
3. **Tax check:** per tax code, `floor(subtotal × rate)` — same as API
4. **Total check:** subtotal + tax
5. Invoices failing verification are **not registered**

**A case where the AI got it wrong**

_(Fill in after running: note any invoice where extraction needed correction or was skipped.)_

## 6. Integrating with the accounting system

| Invoice | Result | How you handled it |
|---|---|---|
| invoice_01.pdf | | |
| invoice_02.pdf | | |
| invoice_03.pdf | | |
| invoice_04.jpg | | |
| invoice_05.jpg | | |
| invoice_06.jpg | | |
| invoice_07.jpg | | |
| invoice_08.jpg | | |
| invoice_09.pdf | | |
| invoice_10.jpg | | |
| invoice_11.jpg | | |
| invoice_12.jpg | | |

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
