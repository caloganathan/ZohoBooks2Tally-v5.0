from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


HEADERS = {"x-api-key": "dev-cloud-api-key"}


def setup_test_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _create_tenant_and_connector(client):
    tenant = client.post("/tenants", json={"name": "Acme", "zoho_org_id": "org1"}, headers=HEADERS)
    assert tenant.status_code == 200
    tenant_id = tenant.json()["id"]
    connector = client.post(f"/tenants/{tenant_id}/connector/enroll", headers=HEADERS)
    assert connector.status_code == 200
    return tenant_id, connector.json()


def test_full_job_lifecycle():
    client = setup_test_client()
    tenant_id, connector_data = _create_tenant_and_connector(client)

    hb = client.post(
        "/agent/heartbeat",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"]},
        headers=HEADERS,
    )
    assert hb.status_code == 200

    job = client.post(
        "/sync/jobs",
        json={
            "tenant_id": tenant_id,
            "direction": "ZOHO_TO_TALLY",
            "object_type": "INVOICE",
            "source_id": "inv-1",
            "payload": {"x": 1},
        },
        headers=HEADERS,
    )
    assert job.status_code == 200
    job_id = job.json()["id"]

    pull = client.post(
        "/agent/jobs/pull",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"], "limit": 5},
        headers=HEADERS,
    )
    assert pull.status_code == 200
    assert len(pull.json()) == 1

    ack = client.post(
        f"/agent/jobs/{job_id}/ack",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"]},
        headers=HEADERS,
    )
    assert ack.status_code == 200

    jobs = client.get("/sync/jobs", headers=HEADERS)
    assert jobs.status_code == 200
    assert jobs.json()[0]["status"] == "DONE"


def test_requires_api_key():
    client = setup_test_client()
    resp = client.post("/tenants", json={"name": "NoAuth"})
    assert resp.status_code == 401


def test_rejects_invalid_connector_secret():
    client = setup_test_client()
    _, connector = _create_tenant_and_connector(client)
    resp = client.post(
        "/agent/heartbeat",
        json={"connector_id": connector["connector_id"], "secret": "bogus"},
        headers=HEADERS,
    )
    assert resp.status_code == 401


def test_rejects_invalid_enum_values():
    client = setup_test_client()
    tenant_id, _ = _create_tenant_and_connector(client)
    resp = client.post(
        "/sync/jobs",
        json={
            "tenant_id": tenant_id,
            "direction": "INVALID",
            "object_type": "INVOICE",
            "source_id": "inv-1",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 422


def test_duplicate_active_job_rejected():
    client = setup_test_client()
    tenant_id, _ = _create_tenant_and_connector(client)
    body = {
        "tenant_id": tenant_id,
        "direction": "ZOHO_TO_TALLY",
        "object_type": "INVOICE",
        "source_id": "inv-dup",
    }
    first = client.post("/sync/jobs", json=body, headers=HEADERS)
    assert first.status_code == 200
    second = client.post("/sync/jobs", json=body, headers=HEADERS)
    assert second.status_code == 409


def test_fail_then_retry_cycle_and_dead_letter():
    client = setup_test_client()
    tenant_id, connector = _create_tenant_and_connector(client)
    cid, secret = connector["connector_id"], connector["secret"]

    body = {
        "tenant_id": tenant_id,
        "direction": "ZOHO_TO_TALLY",
        "object_type": "INVOICE",
        "source_id": "inv-retry",
        "payload": {"force_fail": True},
    }
    job = client.post("/sync/jobs", json=body, headers=HEADERS).json()
    job_id = job["id"]

    # Cycle through MAX_ATTEMPTS failures until the job hits DEAD_LETTER.
    last_status = None
    for _ in range(10):
        pull = client.post(
            "/agent/jobs/pull",
            json={"connector_id": cid, "secret": secret, "limit": 1},
            headers=HEADERS,
        )
        if not pull.json():
            break
        fail = client.post(
            f"/agent/jobs/{job_id}/fail",
            json={"connector_id": cid, "secret": secret, "error_message": "boom"},
            headers=HEADERS,
        )
        assert fail.status_code == 200
        last_status = fail.json()["status"]
        if last_status == "DEAD_LETTER":
            break

    assert last_status == "DEAD_LETTER"

    # Manual retry should requeue and reset attempt counter.
    retry = client.post(f"/sync/jobs/{job_id}/retry", headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"

    jobs = client.get("/sync/jobs", headers=HEADERS).json()
    refreshed = next(j for j in jobs if j["id"] == job_id)
    assert refreshed["status"] == "QUEUED"
    assert refreshed["attempt"] == 0
    assert refreshed["error_message"] is None


def test_retry_missing_job_returns_404():
    client = setup_test_client()
    resp = client.post("/sync/jobs/does-not-exist/retry", headers=HEADERS)
    assert resp.status_code == 404


def test_ack_rejects_non_in_progress_job():
    client = setup_test_client()
    tenant_id, connector = _create_tenant_and_connector(client)
    cid, secret = connector["connector_id"], connector["secret"]

    job = client.post(
        "/sync/jobs",
        json={
            "tenant_id": tenant_id,
            "direction": "ZOHO_TO_TALLY",
            "object_type": "INVOICE",
            "source_id": "inv-ack-guard",
        },
        headers=HEADERS,
    ).json()

    resp = client.post(
        f"/agent/jobs/{job['id']}/ack",
        json={"connector_id": cid, "secret": secret},
        headers=HEADERS,
    )
    assert resp.status_code == 409
