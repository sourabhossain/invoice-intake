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

`run.py` will:
1. Start `accounting_api.py` if it is not already running
2. Extract data from all files in `invoices/`
3. Verify line math and totals (matching the API's tax rules)
4. Match suppliers to partner master
5. POST each valid invoice to `http://localhost:8080/invoices`
6. Save per-invoice JSON and a summary under `output/`

## Options

```bash
python3 run.py --reset          # clear API records, then process all invoices
python3 run.py --dry-run        # extract + verify only, no API registration
python3 run.py --no-api-start   # fail if accounting API is not already running
```

## Architecture

```
invoices/  →  GPT-4o Vision  →  verify amounts  →  match partner  →  POST /invoices
                  (extract)        (local)           (master)          (accounting API)
```

**Verification checks (before registration):**
- Each line: `quantity × unit_price == amount` when both are present
- Subtotal equals sum of line amounts
- Tax computed per rate (T10/T08), floored, matching API rules
- Total equals subtotal + tax

If verification fails, the invoice is skipped and logged — it is not sent to the API.

## Requirements

- Python 3.9+
- OpenAI API key (GPT-4o vision; ~12 invoices, low cost)
- Mock accounting API from `TAKE_HOME.md` (included as `accounting_api.py`)

## Output

| Path | Contents |
|---|---|
| `output/invoice_XX.json` | Raw extraction, partner match, verification result |
| `output/summary.json` | Registration status for all invoices |

## Manual API start (optional)

```bash
python3 accounting_api.py
curl http://localhost:8080/health
```
