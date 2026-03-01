"""機器マスター定義

現場で使用する機器の種別と、各機器に対して必要な撮影項目を定義する。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PhotoSlotDef:
    """撮影スロットの定義"""
    slot_id: str       # 例: "pos_front"
    label: str         # 例: "正面"


@dataclass(frozen=True)
class EquipmentDef:
    """機器種別の定義"""
    equipment_id: str       # 例: "pos_register"
    name: str               # 例: "POSレジ本体"
    photo_slots: tuple[PhotoSlotDef, ...] = field(default_factory=tuple)


# --- マスターデータ ---

EQUIPMENT_MASTER: tuple[EquipmentDef, ...] = (
    EquipmentDef(
        equipment_id="pos_register",
        name="POSレジ本体",
        photo_slots=(
            PhotoSlotDef(slot_id="pos_front", label="正面"),
            PhotoSlotDef(slot_id="pos_back", label="背面"),
            PhotoSlotDef(slot_id="pos_serial", label="シリアル番号"),
        ),
    ),
    EquipmentDef(
        equipment_id="cash_drawer",
        name="キャッシュドロア",
        photo_slots=(
            PhotoSlotDef(slot_id="drawer_full", label="全体"),
            PhotoSlotDef(slot_id="drawer_conn", label="接続部"),
        ),
    ),
    EquipmentDef(
        equipment_id="receipt_printer",
        name="レシートプリンタ",
        photo_slots=(
            PhotoSlotDef(slot_id="printer_front", label="正面"),
            PhotoSlotDef(slot_id="printer_serial", label="シリアル番号"),
        ),
    ),
    EquipmentDef(
        equipment_id="router",
        name="ルーター",
        photo_slots=(
            PhotoSlotDef(slot_id="router_front", label="正面"),
            PhotoSlotDef(slot_id="router_wiring", label="接続・配線"),
        ),
    ),
    EquipmentDef(
        equipment_id="lan_cabling",
        name="LAN配線",
        photo_slots=(
            PhotoSlotDef(slot_id="lan_overview", label="全体俯瞰"),
            PhotoSlotDef(slot_id="lan_point", label="接続ポイント"),
        ),
    ),
)


def get_equipment_by_id(equipment_id: str) -> EquipmentDef | None:
    """機器IDから機器定義を取得する"""
    for eq in EQUIPMENT_MASTER:
        if eq.equipment_id == equipment_id:
            return eq
    return None


def get_all_equipment() -> list[dict]:
    """全機器定義を辞書形式で返す（API応答用）"""
    return [
        {
            "equipment_id": eq.equipment_id,
            "name": eq.name,
            "photo_slots": [
                {"slot_id": s.slot_id, "label": s.label}
                for s in eq.photo_slots
            ],
        }
        for eq in EQUIPMENT_MASTER
    ]
