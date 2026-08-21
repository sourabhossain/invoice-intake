# Invoice Intake Automation

Automates reading Japanese supplier invoices with an LLM, verifying extracted amounts, and registering them in the mock accounting API.

## Quick start

```bash
# 1. Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure your LLM key
cp .env.example .env
# Edit .env and set OPENAI_API_KEY

# 3. Run everything with one command
python3 run.py --reset
```

Step 1's `source .venv/bin/activate` is per-shell — a new terminal needs it again.
Without it `run.py` exits with the exact commands to run rather than an import
traceback, so this is recoverable, not fatal.

`run.py` will:
1. Start `accounting_api.py` if it is not already running
2. Extract data from all files in `invoices/`
3. Verify amounts, dates, payee and payment integrity (see below)
4. Match suppliers to partner master, and skip duplicates
5. POST each valid invoice to `http://localhost:8080/invoices`
6. Save per-invoice JSON and a summary under `output/`

## Options

```bash
python3 run.py --reset             # clear API records, then process all invoices
python3 run.py --dry-run           # extract + verify only, no API registration
python3 run.py --no-api-start      # fail if accounting API is not already running
python3 run.py --only invoice_09   # process just the invoices matching a substring
```

## Architecture

```
invoices/  →  GPT-4o Vision  →  verify  →  match partner  →  dedupe  →  POST /invoices
                  (extract)     (local)      (master)      (local)     (accounting API)
                                    ↓             ↓            ↓
                              held for human review (never auto-registered)
```

## What gates registration

An invoice is registered only if every check below passes. Anything else lands in
the review queue with a reason — nothing questionable reaches the accounting API.

**Amounts**
- Each line: `quantity × unit_price == amount` when both are present (warning)
- Subtotal equals the sum of line amounts
- Tax per rate (T10/T08), floored — the same arithmetic the API applies
- Total equals subtotal + tax
- The subtotal/tax/total read off the page agree with each other
- Every line has a non-empty description and unit, which the API requires

**Dates** — amount checks cannot see a wrong date, so these are checked separately
- Both dates parse to a real `YYYY-MM-DD` (incl. 令和/平成/昭和 and `R8.2.5` forms)
- `due_date` is not before `issue_date`
- `issue_date` is within `INVOICE_MAX_AGE_DAYS` back / `INVOICE_MAX_FUTURE_DAYS`
  forward of today (730 / 30 by default). This is what catches an era-year
  misread: 令和8年 read as 令和5年 is three years off and every arithmetic check
  still passes. Widen the window if you backfill an older archive.
- Cross-check against a date embedded in the invoice number (warning)

**Payee**
- Matched by 登録番号, then exact name, then a substring of at least 4 characters.
  Shorter overlaps are refused and ties between two partners are refused, because
  a wrong match here means paying the wrong company.
- A near-miss name (≥0.80 similarity) becomes a *suggestion* for the reviewer,
  never an automatic match.

**Fraud / tampering**
- Handwritten or coloured alterations to bank transfer details (振込先 / 口座) hold
  the invoice unconditionally, even when the amounts are perfect. Redirecting
  payment to a hand-written account number is the standard invoice fraud pattern.
- The extraction schema carries an explicit `payment_details_altered` boolean, so
  this is a structured signal rather than a keyword search over free text. A
  keyword fallback over `confidence_notes` still applies if the flag is unset.

**Duplicates**
- Checked locally against invoices already registered for that partner, before
  POSTing. Re-running the pipeline is safe: nothing is registered twice.

## Result statuses

| Status | Meaning |
|---|---|
| `registered` | Posted to the accounting API |
| `duplicate` | Already registered for this partner; deliberately not posted |
| `needs_review` | Held for a human, with reasons; not posted |
| `skipped` | `--dry-run` only |
| `failed` | The pipeline itself broke (LLM, API or IO error) |

Exit code is non-zero only for `failed`. Duplicates and review holds are the
pipeline working as designed, not errors.

## Requirements

- Python 3.9+
- OpenAI API key (GPT-4o vision; ~12 invoices, low cost)
- Mock accounting API from `TAKE_HOME.md` (included as `accounting_api.py`)

## Output

| Path | Contents |
|---|---|
| `output/invoice_XX.json` | Raw extraction, normalized fields, partner match, verification issues, status |
| `output/summary.json` | Status and review reasons for all invoices |

`output/` is gitignored. Note that extractions can contain supplier bank details
picked up from the document (including handwritten annotations), so treat the
directory as containing payment data rather than as scratch output.

## Tests

Verification, date normalization and partner matching are covered by stdlib
`unittest` — no API key or network needed:

```bash
python3 -m unittest discover -s tests -t .
```

## Manual API start (optional)

```bash
python3 accounting_api.py
curl http://localhost:8080/health
```
