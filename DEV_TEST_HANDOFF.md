# Developer Test Handoff — ZohoBooks2Tally v5.0

**What this is:** a one-way sync that pushes **TallyPrime** accounting data into
**Zoho Books (India edition)**. Your job is to validate it against a Zoho Books
**sandbox** org before it goes to the client for testing.

**Branch under test:** `claude/tally-zohobooks-sync-review-hr05l7` (PR #4)
**Estimated time:** ~½ day.

> ⚠️ Use a **Zoho Books sandbox / trial org**, never production. The sync
> creates real records (contacts, items, invoices, journals) in whatever org
> the OAuth tokens belong to.

---

## 1. What you need before starting

- Docker + Docker Compose, and Python 3.12 (to run the automated tests).
- A **Zoho Books India sandbox org** and its **Organization ID**.
- A Zoho API **client_id / client_secret** with redirect URI
  `http://localhost:8000/oauth/zoho/callback`.
- In the sandbox org, create a custom field with API name **`cf_external_id`**
  on Contacts, Items, Invoices, Bills and Journals (Settings → Preferences →
  <module> → Field Customization). This stores the Tally cross-reference and is
  how re-syncs stay idempotent.
- A **strong `APP_ENCRYPTION_KEY`** (encrypts Zoho tokens + connector secrets at
  rest). Generate one with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
  Set it once and keep it stable — changing it later makes stored secrets
  undecryptable (you'd have to re-auth Zoho and re-enroll connectors).

---

## 2. Get the code

```bash
git clone <repo-url>
cd ZohoBooks2Tally-v5.0
git checkout claude/tally-zohobooks-sync-review-hr05l7
```

## 3. Run the automated tests (fast sanity gate)

```bash
python -m venv .venv && source .venv/bin/activate

# Cloud control plane
pip install -r cloud-app/requirements.txt
cd cloud-app && python -m pytest -q && cd ..
# Expect: 25 passed

# On-prem agent
pip install -r agent/requirements.txt
cd agent && python -m pytest -q && cd ..
# Expect: 3 passed
```

If either suite fails, stop and report it — the build is not ready for manual
testing.

## 4. Configure and start the stack

```bash
cp .env.example .env
# Edit .env and set:
#   APP_ENCRYPTION_KEY=<generated key>
#   ZOHO_CLIENT_ID=...
#   ZOHO_CLIENT_SECRET=...
#   ZOHO_BOOKS_ORG_ID=<sandbox org id>
#   (leave CLOUD_API_KEY as-is for local testing, or set your own)

docker compose up --build -d
curl -s http://localhost:8000/health        # {"status":"ok"}
curl -s http://localhost:8010/agent/health  # reachable
```

## 5. Run the end-to-end smoke test (the real gate)

Follow **`SANDBOX_SMOKE_TEST.md`** in the repo root step by step. It walks
through: OAuth connect → seed a customer + item + invoice → batch-process →
verify in Zoho → idempotency re-run → trial-balance reconciliation.

**It passes only if all of these are true:**

- [ ] OAuth connects; the callback returns a **status only** (no `access_token`
      / `refresh_token` in the response body).
- [ ] Contact, item and invoice appear in the Zoho sandbox with **populated**
      fields (name, GSTIN, city, rate, HSN — not blanks).
- [ ] The invoice is **linked to the contact** and carries the line item, and
      its `cf_external_id` equals the Tally GUID you sent.
- [ ] Re-processing the **same** `source_id` returns `SKIPPED` and produces
      **no duplicate** document in Zoho.
- [ ] Trial-balance reconciliation returns a real comparison
      (`COMPLETED` when matched, or `MISMATCH` with per-ledger detail).

## 6. Extend coverage to the other document types

Repeat the pattern from the smoke test for each object type your client uses.
Create the master(s) first, then the transaction:

| object_type | Tally source | Becomes in Zoho | Notes |
|---|---|---|---|
| `CONTACT` | Sundry Debtor/Creditor ledger | Customer / Vendor | `is_customer: true/false` |
| `ITEM` | Stock item / service | Item | `is_inventory` sets goods vs service |
| `INVOICE` | Sales voucher | Invoice | needs its customer synced first |
| `BILL` | Purchase voucher | Bill | needs its vendor synced first |
| `RECEIPT` | Receipt voucher | Customer Payment | needs the customer synced |
| `PAYMENT` | Payment voucher | Vendor Payment | needs the vendor synced |
| `JOURNAL` | Journal voucher | Journal | **all** line ledgers must be synced first |
| `CREDIT_NOTE` | Credit note | Credit Note | needs the customer synced |
| `DEBIT_NOTE` | Debit note | Vendor Credit | needs the vendor synced |

Expected correct behaviours to confirm:
- A transaction whose party/ledger was **never synced** fails loudly with a
  clear error (e.g. `Customer not mapped in Zoho…`) and posts **nothing** — it
  must never create an empty/partial document.
- `GET /mappings?tenant_id=<id>` shows a cross-reference row per synced object.
- `GET /audit/events` shows a `*_CREATED` entry per successful sync.
- A failed job retries and lands in `DEAD_LETTER` after 5 attempts; `POST
  /sync/jobs/{id}/retry` requeues it.

## 7. Known caveats to validate specifically

- **Zoho field names** for `JOURNAL`, `RECEIPT`/`PAYMENT` are the most likely to
  need a small adjustment against the live API. If Zoho returns a field error on
  create, note the exact field name from the error — the fix is a one-line
  change in `cloud-app/app/tally_mapper.py` (the mapper for that object type).
- **GST treatment**: the mapper defaults to `business_gst` when a GSTIN is
  present, else `consumer`. Confirm this matches the client's contact mix; some
  orgs need `overseas` / `sez` / `business_none`.
- **Fresh database assumed.** The token/secret columns are `Text`; there's no
  in-place migration from an older schema — test on a clean DB.

## 8. How to report findings

For each issue, include:
- The **object_type** and the exact **request payload** you sent.
- The **response** (status + body) from `/sync/jobs/batch/process`, and the
  `error_message` on the job (`GET /sync/jobs/{id}`).
- If it's a Zoho field error, the **exact Zoho error text** (that names the
  field to fix).
- Whether the automated test suites still pass.

File issues against the PR branch above so fixes can be verified before merge.

---

### Quick reference — service URLs (local)

| Service | URL |
|---|---|
| Cloud control plane | http://localhost:8000 |
| On-prem agent | http://localhost:8010 |
| API docs (interactive) | http://localhost:8000/docs |
| Postgres | localhost:5432 |

All cloud endpoints except `/health` require the `x-api-key: <CLOUD_API_KEY>` header.
