import json
import os
import secrets as py_secrets
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    cloud_base_url: str = "http://cloud-app:8000"
    cloud_api_key: str = "dev-cloud-api-key"
    agent_name: str = "local-agent-1"
    agent_poll_size: int = 10
    agent_local_store: str = "/data/agent.json"
    connector_id: str | None = None
    connector_secret: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
app = FastAPI(title="ZohoBooks2Tally On-Prem Agent", version="0.2.1")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if not py_secrets.compare_digest(x_api_key or "", settings.cloud_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _load_persisted_registration() -> None:
    store = Path(settings.agent_local_store)
    if settings.connector_id and settings.connector_secret:
        return
    if not store.exists():
        return
    try:
        data = json.loads(store.read_text())
        settings.connector_id = data.get("connector_id") or settings.connector_id
        settings.connector_secret = data.get("secret") or settings.connector_secret
    except (json.JSONDecodeError, OSError):
        # Corrupt store file – ignore, registration will be required again.
        return


def _persist_registration(connector_id: str, secret: str) -> None:
    store = Path(settings.agent_local_store)
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"connector_id": connector_id, "secret": secret}))
        os.chmod(store, 0o600)
    except OSError:
        # Persistence is best-effort; registration still works in-memory.
        return


_load_persisted_registration()


class RegistrationIn(BaseModel):
    connector_id: str
    secret: str


def assert_registered() -> tuple[str, str]:
    if not settings.connector_id or not settings.connector_secret:
        raise HTTPException(status_code=400, detail="Agent is not registered. Call /agent/register first.")
    return settings.connector_id, settings.connector_secret


def process_job_payload(job: dict) -> tuple[bool, str]:
    payload = job.get("payload") or {}
    if payload.get("force_fail"):
        return False, "Simulated Tally import failure for testing"
    return True, f"Processed {job.get('object_type')}:{job.get('source_id')}"


@app.get("/agent/health")
def health() -> dict:
    return {
        "status": "ok",
        "agent_name": settings.agent_name,
        "registered": bool(settings.connector_id),
        "time": utcnow_iso(),
    }


@app.post("/agent/register", dependencies=[Depends(verify_api_key)])
def register(data: RegistrationIn) -> dict:
    settings.connector_id = data.connector_id
    settings.connector_secret = data.secret
    _persist_registration(data.connector_id, data.secret)
    return {"status": "registered", "connector_id": settings.connector_id}


@app.post("/agent/run-once", dependencies=[Depends(verify_api_key)])
def run_once() -> dict:
    connector_id, secret = assert_registered()
    headers = {"x-api-key": settings.cloud_api_key}

    with httpx.Client(timeout=30.0) as client:
        hb = client.post(
            f"{settings.cloud_base_url}/agent/heartbeat",
            json={"connector_id": connector_id, "secret": secret},
            headers=headers,
        )
        hb.raise_for_status()

        pull = client.post(
            f"{settings.cloud_base_url}/agent/jobs/pull",
            json={"connector_id": connector_id, "secret": secret, "limit": settings.agent_poll_size},
            headers=headers,
        )
        pull.raise_for_status()
        jobs = pull.json()

        results = []
        for job in jobs:
            ok, message = process_job_payload(job)
            if ok:
                response = client.post(
                    f"{settings.cloud_base_url}/agent/jobs/{job['id']}/ack",
                    json={"connector_id": connector_id, "secret": secret},
                    headers=headers,
                )
            else:
                response = client.post(
                    f"{settings.cloud_base_url}/agent/jobs/{job['id']}/fail",
                    json={"connector_id": connector_id, "secret": secret, "error_message": message},
                    headers=headers,
                )
            response.raise_for_status()
            results.append({"job_id": job["id"], "result": response.json()})

    return {"pulled": len(jobs), "results": results}
