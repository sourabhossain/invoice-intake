# Submission

- Name: Sourab Hossain
- Submission date (YYYY-MM-DD): 2026-08-21
- Hours actually spent: 8
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
| A supplier hand-wrote a new bank account on the invoice — pay it or stop? | Hold for human confirmation with the supplier; never auto-register | Every amount reconciles perfectly, so no arithmetic check can catch it. This is the redirection fraud pattern, and it appears in the sample data (`invoice_08`) |
| The supplier's own total is ¥1 above subtotal + tax — pay the printed figure or our recomputation? | Hold for review rather than silently choosing either | The API would happily accept our recomputation, but paying a figure the supplier did not invoice creates a reconciliation gap nobody asked for |
| How far back can a legitimately old invoice be? | 730 days, configurable via `INVOICE_MAX_AGE_DAYS` | Needed *some* date sanity check, because a misread era year (令和8 → 令和5) passes every amount check silently |

## 3. Scoping decisions

I spent the first pass getting extraction and registration working end to end, then
spent the rest on the part that actually matters: **making the pipeline refuse to
register what it cannot verify.** That reordering came from the data — the first
working version registered two invoices with wrong or unsafe data and reported
them as successes.

**What you built**

- LLM vision extraction for PDF and scanned JPG invoices (PyMuPDF renders pages)
- Amount verification: line math, subtotal, per-code floored tax, total, and
  internal consistency of the three printed figures
- Date verification: era-year conversion (令和/平成/昭和), plausibility window, and
  a cross-check against the date embedded in the invoice number
- Payment-integrity gate: hand-altered bank details hold the invoice even when
  every amount reconciles
- Partner matching by 登録番号 → exact name → bounded substring, with near-miss
  names surfaced as reviewer *suggestions* rather than automatic matches
- Local duplicate detection per partner, before POSTing — re-runs are idempotent
- A review queue: every non-registered invoice reports why, in the console and in
  `output/summary.json`
- Auto-start accounting API + single command (`python3 run.py --reset`), with a
  dependency preflight so a missing venv gives instructions, not a traceback
- 27 unit tests over the verification rules, needing no API key or network

**What you left out, and why**

- Human review web UI — the highest-value next step, but the review *queue* with
  machine-readable reasons is the part a UI needs underneath, so I built that first
- Confidence scoring — binary pass/fail plus specific error codes gives a reviewer
  more to act on than a number would, at this scale
- Second-pass re-extraction on disputed fields — would likely have fixed the two
  extraction errors I found, but I chose to *detect* reliably before *correcting*
- Email ingestion / queue — out of scope for a sample folder
- Batch-relative date checking (flag the outlier against the rest of the batch) —
  more robust than an absolute window, but needs a two-pass restructure

## 4. Design and technology choices

**Flow:** `invoices/` → render PDF pages as images → GPT-4o structured JSON extraction → local verification → partner match → POST `/invoices`.

**Chose:**
- **Python** — fast integration, good PDF/image libraries
- **OpenAI GPT-4o** — strong Japanese document understanding, native vision + JSON
  schema. Paid API on my own key; there are only 12 invoices so cost is negligible
- **Structured output over free text** — `strict` JSON schema, including a
  `payment_details_altered` boolean, so the tampering signal is a typed field
  rather than a keyword search over prose
- **PyMuPDF** — render scanned PDFs to images without extra dependencies
- **httpx** — simple API client; retries with backoff on 429/5xx only

**Decided against:**
- Traditional OCR-only (Tesseract) — weak on layout variation and handwriting
- Sending extracted totals directly to API — API recalculates and rejects mismatches
- Trusting the model's own confidence prose as the tampering signal — too fragile;
  a typed boolean with a keyword fallback is harder to miss
- Auto-correcting a figure the pipeline believes is wrong — detection is reliable,
  correction is not, and this is money
- Changing the mock API — assignment forbids it

## 5. How you used AI, and how you checked it

**What you delegated to AI**

- Reading invoice images/PDFs and extracting fields (supplier, dates, line items, amounts)
- Handling varied layouts and Japanese labels

**How you verified the output**

Nothing the model returns is trusted on its own. Everything is recomputed locally,
and any failure holds the invoice out of the API.

The check I would defend first is **recomputing tax per code from the line items**
(`floor(subtotal × rate)`, mirroring the API) instead of reading the printed 消費税.
It is the one check that catches a misread the model has no way to self-report: the
figure looks perfectly plausible on its own and only contradicts the lines it is
supposed to summarise. It caught `invoice_03`.

- **Amounts** — line math (warning), subtotal from lines, per-code floored tax,
  total, and whether the three printed figures agree with each other at all.
- **Dates** — both parse to a real `YYYY-MM-DD` (令和/平成/昭和, `R8.2.5`); due not
  before issue; issue date inside a plausibility window; cross-check against a date
  embedded in the invoice number. Dates need their own checks because a wrong date
  passes every arithmetic check untouched.
- **Payee** — 登録番号 → exact name → substring of ≥4 chars; ties and short overlaps
  refused rather than guessed; near-misses surfaced as suggestions, never matches.
- **Payment integrity** — hand-altered bank details hold the invoice regardless of
  the amounts, driven by a typed `payment_details_altered` field.
- **Pre-empting the API** — local duplicate detection per partner, and non-empty
  description/unit, so these surface as reasons rather than rejected POSTs.

27 unit tests cover these rules, including the three inputs that used to crash the
run outright (0% tax lines, unparseable dates, empty line lists).

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

The table below is **one representative run** (`output/summary.json` from the log in
`demo/`). Extraction is not fully deterministic even at `temperature=0`:
`invoice_03` sometimes reads cleanly and registers, and `invoice_11`'s misread era
year came out as 令和5 on one run and 令和2 on another. The *verification* outcome is
stable — what varies is which invoice needs review, not whether a bad one slips
through. I regard that instability as the main argument for the whole checking
layer, so I have left it visible rather than reporting a best-of run.

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

- **Cost per invoice:** ~$0.01–0.03. One vision call per page at 200 DPI is roughly
  1–1.5k input tokens plus ~0.5k output; multi-page PDFs scale linearly. Retries are
  rare and capped at 3. Worth re-measuring against current GPT-4o pricing before
  quoting this to the client.
- **Monthly cost at 1,000 invoices/month:** ~$10–30 in LLM calls, compute negligible.
  The real cost at that volume is **human review time**, not tokens: at the 5-in-12
  hold rate seen here, roughly 400 invoices/month reach a person. Driving that rate
  down is where the money is.
- **Processing time per invoice:** ~5–15 seconds, LLM latency dominant. Sequential
  today; the batch parallelises trivially since invoices are independent.
- **Where this breaks first:**
  1. **Single-digit misreads inside the plausibility window.** 令和8 → 令和5 is caught
     because it lands 3 years out; 令和8 → 令和7 would not be. The invoice-number
     cross-check only helps for suppliers who embed the date.
  2. **Documents with several 消費税 rows.** `invoice_03` is the one the pipeline got
     wrong in both directions — partial tax capture *and* a one-character supplier
     misread on the same document.
  3. **Extraction non-determinism.** The same file can extract differently run to
     run, so "it worked yesterday" is not evidence.
  4. **Every new supplier needs a partner-master entry first.** The pipeline cannot
     create one, so onboarding stays manual.
  5. **Tampering the model does not remark on.** A subtle alteration that it reads
     as clean print bypasses the payment-integrity gate entirely.
- **How you would find out if something was registered incorrectly:** a daily
  reconciliation job re-reading `output/*.json` against `GET /invoices`, alerting on
  any field drift; a weekly sample of registered invoices re-verified against the
  source image; and alerting on the *hold rate* itself — a sudden drop means the
  checks stopped firing, which is more dangerous than a spike.

## 8. What you would do with another 8 hours

1. **Human review UI over the existing queue.** Every held invoice already carries
   machine-readable reasons, a computed-vs-printed diff, and a suggested partner —
   the data a correction screen needs is there, only the screen is missing. This is
   first because 5 of 12 invoices need a person, and right now that person reads JSON.
2. **Targeted re-extraction on disputed fields.** When a check fails, re-ask the
   model about *only* the contested figure with a cropped region, and require two
   runs to agree. Both extraction errors I found (`invoice_03` tax rows,
   `invoice_11` era year) were single-field misreads on documents that were
   otherwise read perfectly, so a narrow second pass should convert most holds into
   automatic registrations. Second because it needs the UI to fall back to.
3. **Reconciliation and hold-rate monitoring.** The daily drift job and hold-rate
   alerting described in section 7. Third not because it matters least, but because
   with 12 invoices a month-end eyeball still works; at 1,000 it does not.
