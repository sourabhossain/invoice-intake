# Invoice Intake

One command reads a folder of Japanese supplier invoices, recomputes every number
locally, and registers into the accounting API only the invoices it can prove.
Everything else lands in a review queue with a reason attached.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then set OPENAI_API_KEY

python3 run.py --reset        # starts the API, processes all 12 invoices
```

Needs Python 3.9+ (tested on 3.12) and an OpenAI API key. Nothing else: the mock
accounting API from `TAKE_HOME.md` ships with the repo and starts on its own.

`source .venv/bin/activate` is per shell, so a new terminal needs it again. Skip it
and `run.py` prints the exact commands to run instead of an import traceback.

## Flow

Every invoice takes the same path, and three gates can divert it before it reaches
the accounting API.

```mermaid
flowchart LR
    IN["invoices/<br/>PDF and scans"] --> EX["Extract<br/>GPT-4o vision"]
    EX --> VF["Recompute<br/>amounts and dates"]
    VF --> PY{"Payee resolved<br/>with certainty?"}
    PY -->|no| RV(["needs_review"])
    PY -->|yes| DUP{"Already registered<br/>for that partner?"}
    DUP -->|yes| DU(["duplicate"])
    DUP -->|no| CK{"Any blocking<br/>check failed?"}
    CK -->|yes| RV
    CK -->|no| OK(["registered"])
```

Extraction is the only step that leaves the machine. Everything after it is local
arithmetic and lookups, so a wrong reading is caught without a second API call.

## Verification

Ten checks block registration. Three record a warning without blocking. An invoice
is posted only when all ten pass.

| Area | Blocks when |
|---|---|
| Lines | no line items were found; a line has an empty description or unit; a line's tax rate is neither 10% nor 8% |
| Amounts | subtotal is not the sum of the lines; tax is not `floor(subtotal * rate)` per tax code; total is not subtotal plus tax |
| Dates | a date does not parse; the due date precedes the issue date; the issue date falls outside the plausibility window |
| Payee | the supplier does not resolve to exactly one partner in the master |
| Payment | the bank transfer details look hand-altered |

Warnings are recorded in the JSON but do not hold the invoice: a line where
`quantity * unit_price` does not equal `amount`, a printed subtotal plus tax that
does not equal the printed total, and an issue date that disagrees with the date
embedded in the invoice number.

Three checks carry most of the weight.

**Tax is recomputed from the line items, never read off the page.** The arithmetic
mirrors the accounting API exactly, including the per-code floor. A misread 消費税
looks perfectly plausible on its own and only contradicts the lines it is supposed
to summarise. This is the check that catches what the model cannot self-report.

**The issue date is checked for plausibility.** A wrong date passes every arithmetic
check untouched. 令和8年 misread as 令和5年 is three years off and nothing else
notices. The window defaults to 730 days back and 30 days forward, tunable with
`INVOICE_MAX_AGE_DAYS` and `INVOICE_MAX_FUTURE_DAYS`.

**Hand-altered bank details hold the invoice even when every amount reconciles.**
Redirecting payment to a handwritten account number is the standard invoice fraud
pattern, and no arithmetic check can see it. The extraction schema carries a typed
`payment_details_altered` boolean, with a keyword fallback over the model's notes.

### Payee matching

Tried in order: 登録番号, then an exact name match, then a substring of at least 4
characters. Shorter overlaps and ties between two partners are refused rather than
guessed, because a wrong match here pays the wrong company. A near miss (0.80
similarity or better) becomes a suggestion for the reviewer, never an automatic
match.

### Duplicates

Checked locally against what is already registered for that partner, before the
POST. Re-running the pipeline registers nothing twice.

## Options

| Flag | Effect |
|---|---|
| `--reset` | Clear API records, then process all invoices |
| `--dry-run` | Extract and verify only, no registration |
| `--no-api-start` | Fail if the accounting API is not already running |
| `--only SUBSTRING` | Process only invoices whose filename matches |

## Statuses

| Status | Meaning |
|---|---|
| `registered` | Posted to the accounting API |
| `duplicate` | Already registered for this partner, deliberately not posted |
| `needs_review` | Held for a human, with reasons, not posted |
| `skipped` | `--dry-run` only |
| `failed` | The pipeline itself broke (LLM, API or IO error) |

Exit code is non-zero only when something went wrong: a missing key, an unreachable
API, or an invoice that errored. Duplicates and review holds exit `0`, because they
are the pipeline working as designed.

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
| `demo/run-output.txt` | Console output of that run |
| `demo/summary.json` | Machine-readable result of that run |

## Tests

27 stdlib `unittest` cases over the verification rules, partner matching and date
normalization. No API key and no network needed.

```bash
python3 -m unittest discover -s tests -t .
```

## Starting the accounting API by hand

`run.py` does this for you. To run it separately:

```bash
python3 accounting_api.py
curl http://localhost:8080/health
```
