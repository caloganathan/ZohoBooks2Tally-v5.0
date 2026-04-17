from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


def setup_test_client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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


def test_full_job_lifecycle():
    client = setup_test_client()
    headers = {"x-api-key": "dev-cloud-api-key"}

    tenant = client.post("/tenants", json={"name": "Acme", "zoho_org_id": "org1"}, headers=headers)
    assert tenant.status_code == 200
    tenant_id = tenant.json()["id"]

    connector = client.post(f"/tenants/{tenant_id}/connector/enroll", headers=headers)
    assert connector.status_code == 200
    connector_data = connector.json()

    hb = client.post(
        "/agent/heartbeat",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"]},
        headers=headers,
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
        headers=headers,
    )
    assert job.status_code == 200
    job_id = job.json()["id"]

    pull = client.post(
        "/agent/jobs/pull",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"], "limit": 5},
        headers=headers,
    )
    assert pull.status_code == 200
    assert len(pull.json()) == 1

    ack = client.post(
        f"/agent/jobs/{job_id}/ack",
        json={"connector_id": connector_data["connector_id"], "secret": connector_data["secret"]},
        headers=headers,
    )
    assert ack.status_code == 200

    jobs = client.get("/sync/jobs", headers=headers)
    assert jobs.status_code == 200
    assert jobs.json()[0]["status"] == "DONE"


def test_requires_api_key():
    client = setup_test_client()
    resp = client.post("/tenants", json={"name": "NoAuth"})
    assert resp.status_code == 401
