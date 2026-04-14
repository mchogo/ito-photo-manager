import logging

from fastapi.testclient import TestClient
from main import app
from auth import get_current_user

logging.basicConfig(level=logging.INFO)

def mock_user():
    return {"sub": "test", "role": "admin"}
app.dependency_overrides[get_current_user] = mock_user

client = TestClient(app)
res = client.post("/api/projects", json={
    "site_id": "SITE-001",
    "work_date": "2025-06-01",
    "worker_name": "テスト太郎",
    "equipment_ids": ["pos_register", "router"],
})
logger = logging.getLogger(__name__)

logger.info("STATUS: %s", res.status_code)
logger.info("BODY: %s", res.json())
