from fastapi.testclient import TestClient
from main import app
from auth import get_current_user

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
print("STATUS:", res.status_code)
print("BODY:", res.json())
