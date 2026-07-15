# Sandbox Smoke Test — Pre-Client Gate

Run this **once against a Zoho Books _sandbox_ organization** before any client
enters live data. It exercises the full Tally → Zoho Books path end to end and
confirms the API field names land correctly (the one thing unit tests cannot
verify without a live Zoho org).

> ⚠️ Use a **sandbox / trial** Zoho org, not production. This test creates real
> records (a contact, an item, an invoice, a journal) in whatever org the
> tokens belong to.

## 0. Prerequisites

- A Zoho Books **India** sandbox org and its **Organization ID**.
- A Zoho API **client_id / client_secret** (Self Client or Server-based app),
  with the redirect URI registered as `http://localhost:8000/oauth/zoho/callback`.
- A custom field with API name **`cf_external_id`** created in the sandbox org on
  Contacts, Items, Invoices, Bills and Journals (this is how the integration
  stores the Tally cross-reference). Zoho → Settings → Preferences → (module) →
  Field Customization.

## 1. Configure and start

```bash
cp .env.example .env
# Edit .env and set at minimum:
#   APP_ENCRYPTION_KEY=<output of: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
#   ZOHO_CLIENT_ID=...
#   ZOHO_CLIENT_SECRET=...
#   ZOHO_BOOKS_ORG_ID=<sandbox org id>
docker compose up --build -d

export API_KEY=dev-cloud-api-key          # match CLOUD_API_KEY in .env
export CLOUD=http://localhost:8000
export AGENT=http://localhost:8010
```

Health checks (both should return `{"status":"ok"}` / reachable):

```bash
curl -s $CLOUD/health
curl -s $AGENT/agent/health
```

## 2. Create a tenant

```bash
curl -s -X POST $CLOUD/tenants \
  -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"Sandbox Co","zoho_org_id":"<SANDBOX_ORG_ID>"}'
# -> copy the "id" as TENANT_ID
export TENANT_ID=<id-from-response>
```

## 3. Connect Zoho (OAuth) — stores encrypted tokens

```bash
curl -s -X POST $CLOUD/tenants/$TENANT_ID/zoho/connect -H "x-api-key: $API_KEY"
# -> open the returned auth_url in a browser, approve, and Zoho redirects to the
#    callback. Expect: {"status":"connected","tenant_linked":true}
```

Confirm the callback did **not** leak tokens (body should be the status object
above, with no access_token/refresh_token fields).

## 4. Seed masters + a transaction (canonical payloads)

For a pure smoke test you can create jobs directly rather than wiring a live
Tally. Order matters: **masters before the invoice**.

```bash
# 4a. Customer (Tally ledger -> Zoho contact)
curl -s -X POST $CLOUD/sync/jobs -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' -d '{
  "tenant_id":"'$TENANT_ID'","direction":"TALLY_TO_ZOHO","object_type":"CONTACT",
  "source_id":"guid-cust-1",
  "payload":{"name":"Smoke Customer","tally_guid":"guid-cust-1","is_customer":true,
             "gstin":"27AAAAA0000A1Z5","city":"Mumbai","state":"Maharashtra","pincode":"400001"}}'

# 4b. Item (Tally stock item -> Zoho item)
curl -s -X POST $CLOUD/sync/jobs -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' -d '{
  "tenant_id":"'$TENANT_ID'","direction":"TALLY_TO_ZOHO","object_type":"ITEM",
  "source_id":"guid-item-1",
  "payload":{"name":"Smoke Widget","tally_guid":"guid-item-1","rate":100,"unit":"Nos",
             "hsn_code":"8481","is_inventory":true}}'

# 4c. Sales invoice referencing the customer + item by Tally name
curl -s -X POST $CLOUD/sync/jobs -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' -d '{
  "tenant_id":"'$TENANT_ID'","direction":"TALLY_TO_ZOHO","object_type":"INVOICE",
  "source_id":"guid-inv-1",
  "payload":{"tally_guid":"guid-inv-1","voucher_number":"INV-SMOKE-1","date":"20260714",
             "party_ledger_name":"Smoke Customer",
             "inventory_entries":[{"stock_item_name":"Smoke Widget","quantity":2,"rate":100,"unit":"Nos"}]}}'
```

## 5. Process the queue (Tally → Zoho)

```bash
curl -s -X POST "$CLOUD/sync/jobs/batch/process?tenant_id=$TENANT_ID" -H "x-api-key: $API_KEY"
```

**Expect** each result `"status":"success"` with a `CREATED` sub-status and a
real Zoho id. The batch applies masters first, so the invoice resolves the
customer and item automatically.

## 6. Verify

```bash
# Cross-reference table: one row each for LEDGER, STOCKITEM, VOUCHER
curl -s "$CLOUD/mappings?tenant_id=$TENANT_ID" -H "x-api-key: $API_KEY"

# Audit trail: CONTACT_CREATED, ITEM_CREATED, INVOICE_CREATED
curl -s $CLOUD/audit/events -H "x-api-key: $API_KEY"
```

Then **open the Zoho Books sandbox UI** and confirm:
- The contact exists with name, GSTIN, city/state populated (not blank).
- The item exists with rate and HSN.
- The invoice exists, is linked to the contact, has the line item, and its
  `cf_external_id` equals `guid-inv-1`.

### Idempotency check (must not double-post)

```bash
# Re-create + re-process the SAME invoice source_id after the first is DONE.
curl -s -X POST $CLOUD/sync/jobs -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' -d '{
  "tenant_id":"'$TENANT_ID'","direction":"TALLY_TO_ZOHO","object_type":"INVOICE",
  "source_id":"guid-inv-1","payload":{"tally_guid":"guid-inv-1","voucher_number":"INV-SMOKE-1",
  "date":"20260714","party_ledger_name":"Smoke Customer",
  "inventory_entries":[{"stock_item_name":"Smoke Widget","quantity":2,"rate":100,"unit":"Nos"}]}}'
curl -s -X POST "$CLOUD/sync/jobs/batch/process?tenant_id=$TENANT_ID" -H "x-api-key: $API_KEY"
```

**Expect** `SKIPPED` (unchanged) — and **no second invoice** in Zoho.

## 7. (Optional) Journal + reconciliation

```bash
# Journal requires its ledgers to be mapped first (sync them as LEDGER jobs),
# otherwise it correctly fails with "Journal ledgers not mapped in Zoho: ...".

# Trial-balance reconciliation against the live Zoho org:
curl -s -X POST $CLOUD/reconcile/trial-balance -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' -d '{
  "tenant_id":"'$TENANT_ID'","period_from":"2026-04-01","period_to":"2026-07-14",
  "tally_trial_balance":[{"ledger_name":"Smoke Customer","debit":236,"credit":0}]}'
# -> status COMPLETED (matched) or MISMATCH with per-ledger details.
```

## Pass criteria (the gate)

- [ ] OAuth connects; callback returns status only (no tokens in body).
- [ ] Contact, item, invoice all created in the Zoho sandbox with **populated**
      fields and correct `cf_external_id`.
- [ ] Invoice is linked to the contact and carries the line item.
- [ ] Re-processing the same source is `SKIPPED` (no duplicate in Zoho).
- [ ] Trial-balance reconciliation returns a real comparison.

If any create returns a Zoho field error, note the field name from the error and
adjust the corresponding mapper in `cloud-app/app/tally_mapper.py` — this is the
expected place for a small field-name tweak before production.
