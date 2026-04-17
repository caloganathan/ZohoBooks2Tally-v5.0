# ZohoBooks2Tally v5.0 – Deployable MVP

This repository contains a **deployable MVP** aligned to `zoho-books-tallyprime-sync-architecture.md`.

## What is implemented

- Cloud control plane (`cloud-app`) for tenant lifecycle, connector enrollment, secure agent orchestration, sync jobs, and audit ledger.
- On-prem style connector (`agent`) for heartbeat, pull/ack/fail loop, and processing simulation.
- PostgreSQL persistence for tenants, connectors, sync jobs, and audit events.
- Docker Compose deployment for local/VM rollout.
- Basic test coverage for core API lifecycle.

## Services

- `cloud-app` (FastAPI): `http://localhost:8000`
- `agent` (FastAPI): `http://localhost:8010`
- `postgres`: `localhost:5432`

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

## Security model (MVP)

All cloud endpoints except `/health` are protected by `x-api-key`.

```bash
export API_KEY=dev-cloud-api-key
```

## Key API examples

Create tenant:

```bash
curl -X POST http://localhost:8000/tenants \
  -H "x-api-key: ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Finance","zoho_org_id":"123456789"}'
```

Enroll connector:

```bash
curl -X POST http://localhost:8000/tenants/<TENANT_ID>/connector/enroll \
  -H "x-api-key: ${API_KEY}"
```

Register connector in agent:

```bash
curl -X POST http://localhost:8010/agent/register \
  -H 'Content-Type: application/json' \
  -d '{"connector_id":"<CONNECTOR_ID>","secret":"<CONNECTOR_SECRET>"}'
```

Create sync job:

```bash
curl -X POST http://localhost:8000/sync/jobs \
  -H "x-api-key: ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"<TENANT_ID>",
    "direction":"ZOHO_TO_TALLY",
    "object_type":"INVOICE",
    "source_id":"460000000012345",
    "priority":"HIGH",
    "dependency_keys":["CONTACT:123","ITEM:456"],
    "payload":{"invoice_number":"INV-1","total":1000}
  }'
```

Run one processing cycle in agent:

```bash
curl -X POST http://localhost:8010/agent/run-once
```

List audit events:

```bash
curl http://localhost:8000/audit/events -H "x-api-key: ${API_KEY}"
```

## Tests

```bash
cd cloud-app
python -m pytest -q
```
