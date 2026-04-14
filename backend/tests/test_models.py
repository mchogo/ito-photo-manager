"""Pydantic モデルテスト"""

from datetime import date, datetime

from models import ProjectResponse


def _build_project_response() -> ProjectResponse:
    return ProjectResponse(
        project_id="PRJ-001",
        site_id="SITE-001",
        work_date=date(2026, 1, 1),
        worker_name="テスト作業員",
        created_at=datetime(2026, 1, 1, 9, 0, 0),
        equipment=[],
    )


def test_project_response_documents_is_independent_between_instances():
    first = _build_project_response()
    second = _build_project_response()

    first.documents.append({
        "document_id": "DOC-001",
        "project_id": "PRJ-001",
        "document_type": "依頼シート",
        "original_filename": "request.pdf",
        "stored_filename": "stored-request.pdf",
        "size_bytes": 1024,
        "uploaded_at": datetime(2026, 1, 1, 10, 0, 0),
    })

    assert len(first.documents) == 1
    assert second.documents == []
