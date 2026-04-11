"""Reference master/template data loader."""

from __future__ import annotations

import json
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SITE_MASTER_PATH = DATA_DIR / "site_master_ncr.json"
REQUEST_TEMPLATE_PATH = DATA_DIR / "request_sheet_template.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_site_master() -> dict:
    return _load_json(SITE_MASTER_PATH)


def load_request_sheet_template() -> dict:
    return _load_json(REQUEST_TEMPLATE_PATH)

