"""機器マスターのテスト"""

from equipment_master import EQUIPMENT_MASTER, get_all_equipment, get_equipment_by_id


def test_master_has_5_equipment():
    assert len(EQUIPMENT_MASTER) == 5


def test_pos_register_has_3_slots():
    eq = get_equipment_by_id("pos_register")
    assert eq is not None
    assert eq.name == "POSレジ本体"
    assert len(eq.photo_slots) == 3


def test_router_has_2_slots():
    eq = get_equipment_by_id("router")
    assert eq is not None
    assert eq.name == "ルーター"
    assert len(eq.photo_slots) == 2


def test_unknown_equipment_returns_none():
    assert get_equipment_by_id("nonexistent") is None


def test_get_all_equipment_returns_dicts():
    result = get_all_equipment()
    assert isinstance(result, list)
    assert len(result) == 5
    for item in result:
        assert "equipment_id" in item
        assert "name" in item
        assert "photo_slots" in item
        assert isinstance(item["photo_slots"], list)


def test_all_slot_ids_unique():
    """全機器の全スロットIDがグローバルに一意であること"""
    all_ids = []
    for eq in EQUIPMENT_MASTER:
        for slot in eq.photo_slots:
            all_ids.append(slot.slot_id)
    assert len(all_ids) == len(set(all_ids))
