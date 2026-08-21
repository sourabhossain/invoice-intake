# Invoice Intake Automation

Reads Japanese supplier invoices — PDFs, scans, handwriting — with a vision LLM,
recomputes every number locally, and registers into the accounting API only what it
can prove. Anything it cannot verify goes to a review queue with a reason attached.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then set OPENAI_API_KEY

python3 run.py --reset        # one command: starts the API, processes all 12 invoices
```

Needs Python 3.9+ (developed and tested on 3.12), an OpenAI API key, and nothing
else — `accounting_api.py` from `TAKE_HOME.md` is included and started for you.

`source .venv/bin/activate` is per-shell, so a new terminal needs it again. Without
it `run.py` prints the exact commands to run rather than an import traceback.

## What that one command does

```
invoices/  →  GPT-4o Vision  →  verify  →  match partner  →  dedupe  →  POST /invoices
                 (extract)      (local)      (master)       (local)    (accounting API)
                                   ↓             ↓             ↓
                          review queue — held, never auto-registered
```

1. Starts `accounting_api.py` if it is not already listening
2. Extracts structured data from every invoice in `invoices/`
3. Verifies amounts, dates, payee and payment integrity
4. Matches the supplier to the partner master and skips duplicates
5. POSTs what passed to `http://localhost:8080/invoices`
6. Writes per-invoice JSON and a run summary to `output/`

## What gates registration

An invoice reaches the API only when every gate below passes. Nothing questionable
is registered, and nothing is registered on the strength of the model's own word.

| Gate | What is checked | Why it earns its place |
|---|---|---|
| **Amounts** | At least one line item was found; every line's rate maps to an API tax code (10% or 8%) and carries a non-empty description and unit, which the API requires; subtotal equals the sum of line amounts; tax is `floor(subtotal × rate)` per tax code — the same arithmetic the API applies; total equals subtotal + tax | Recomputing tax from the lines catches a misread the model cannot self-report: the printed 消費税 looks perfectly plausible on its own and only contradicts the lines it is supposed to summarise |
| **Dates** | Both dates parse to a real `YYYY-MM-DD` (incl. 令和/平成/昭和 and `R8.2.5` forms); `due_date` is not before `issue_date`; `issue_date` falls inside a plausibility window; it is cross-checked against a date embedded in the invoice number | A wrong date passes every arithmetic check untouched. 令和8年 misread as 令和5年 is three years off and no amount check notices |
| **Payee** | 登録番号 first, then exact name, then a substring of at least 4 characters. Shorter overlaps and ties between partners are refused rather than guessed; a near miss (≥ 0.80 similarity) becomes a *suggestion* for the reviewer, never an automatic match | A wrong match here means paying the wrong company |
| **Payment integrity** | Handwritten, stamped or differently-coloured alterations to the bank transfer details (振込先 / 口座) hold the invoice unconditionally, even when every amount reconciles | That is the invoice-redirection fraud pattern, and no arithmetic check can see it. The extraction schema carries a typed `payment_details_altered` boolean, with a keyword fallback over the model's notes |
| **Duplicates** | Checked locally against what is already registered for that partner, before POSTing | A resend is a business outcome to report, not a rejected POST to explain. Re-running the pipeline registers nothing twice |

Ten checks block registration. Three are advisory instead, because they flag a
discrepancy without establishing which side is wrong — they print as warnings and
land in the JSON, but do not hold the invoice on their own:

- a line whose `quantity × unit_price` does not equal its `amount`
- the printed subtotal + tax not equalling the printed total, which cannot happen
  without a blocking mismatch also firing, so it serves to tell the reviewer which
  figure to doubt
- an issue date that disagrees with the date embedded in the invoice number

The date window defaults to 730 days back and 30 days forward, tunable with
`INVOICE_MAX_AGE_DAYS` and `INVOICE_MAX_FUTURE_DAYS`. Widen it to backfill an
older archive.

## Options

```bash
python3 run.py --reset             # clear API records, then process all invoices
python3 run.py --dry-run           # extract and verify only, no registration
python3 run.py --no-api-start      # fail if the accounting API is not already up
python3 run.py --only invoice_09   # process only invoices matching a substring
```

## Result statuses

| Status | Meaning |
|---|---|
| `registered` | Posted to the accounting API |
| `duplicate` | Already registered for this partner; deliberately not posted |
| `needs_review` | Held for a human, with reasons; not posted |
| `skipped` | `--dry-run` only |
| `failed` | The pipeline itself broke (LLM, API or IO error) |

Exit code is non-zero only when something actually went wrong — a missing key, an
unreachable API, or an invoice that errored. Duplicates and review holds exit `0`:
they are the pipeline working as designed.

## Output

| Path | Contents |
|---|---|
| `output/invoice_XX.json` | Raw extraction, normalized fields, partner match, verification issues, status |
| `output/summary.json` | Status and review reasons for every invoice in the run |

`output/` is gitignored. Extractions can carry supplier bank details lifted from the
document, including handwritten annotations, so treat the directory as payment data
rather than as scratch output.

## Demo

| Path | Contents |
|---|---|
| `demo/demo-run.mov` | Screen recording of a full run (41s) |
| `demo/run-output.txt` | Console output of one `python3 run.py --reset` |
| `demo/summary.json` | Machine-readable result of that same run |

## Tests

27 stdlib `unittest` cases over the verification rules, partner matching and date
normalization. No API key and no network needed:

```bash
python3 -m unittest discover -s tests -t .
```

## Starting the accounting API by hand

`run.py` does this for you. To run it separately:

```bash
python3 accounting_api.py
curl http://localhost:8080/health
```
