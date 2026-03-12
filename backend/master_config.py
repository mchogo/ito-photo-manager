"""マスター設定管理

ステータス一覧・書類種別一覧をJSONファイルに保存・読み込みする。
初回起動時はハードコードされたデフォルト値でファイルを作成する。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "master_config.json"

_DEFAULT_CONFIG: dict = {
    "statuses": [
        {"value": "対応前", "color": "gray"},
        {"value": "客連絡待ち", "color": "yellow"},
        {"value": "N連絡待ち", "color": "orange"},
        {"value": "調整完了", "color": "green"},
        {"value": "Pコメ待ち", "color": "blue"},
        {"value": "再架電", "color": "orange"},
        {"value": "荷電待機中", "color": "violet"},
        {"value": "仮押さえ", "color": "cyan"},
        {"value": "ファーストコール済み", "color": "teal"},
        {"value": "日程確定済み", "color": "green"},
        {"value": "対応中", "color": "indigo"},
        {"value": "案件終了", "color": "gray"},
        {"value": "対応不可", "color": "red"},
        {"value": "未発注", "color": "amber"},
        {"value": "キャンセル", "color": "red"},
        {"value": "杉本調整中", "color": "violet"},
        {"value": "成果物提出待ち", "color": "blue"},
        {"value": "図書提出待ち", "color": "blue"},
        {"value": "図書修正待ち", "color": "rose"},
        {"value": "統制移行", "color": "gray"},
    ],
    "document_types": [
        {"value": "依頼シート", "category": "管理共有"},
        {"value": "ID通知書", "category": "管理共有"},
        {"value": "コンフィグ", "category": "管理共有"},
        {"value": "チェックリスト", "category": "管理共有"},
        {"value": "現地調査報告", "category": "現地調査"},
        {"value": "完成図書_調査", "category": "現地調査"},
        {"value": "完成図書_設置", "category": "設置"},
        {"value": "その他", "category": "管理共有"},
    ],
}


def load_config() -> dict:
    """設定を読み込む。ファイルが存在しない場合はデフォルト値で作成する"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(_DEFAULT_CONFIG)
        return _DEFAULT_CONFIG.copy()
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("master_config.json の読み込みに失敗。デフォルト値を使用")
        return _DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """設定を保存する"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("master_config.json を保存しました")


def get_statuses() -> list[dict]:
    return load_config().get("statuses", _DEFAULT_CONFIG["statuses"])


def get_document_types() -> list[dict]:
    return load_config().get("document_types", _DEFAULT_CONFIG["document_types"])
