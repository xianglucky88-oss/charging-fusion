from charging_fusion.metering_application import run_check
from charging_fusion.metering_models import Attribution, CheckResult
from charging_fusion.metering_normalize import normalize_reading
from charging_fusion.metering_storage import MeteringStore

T0 = "2026-11-01T00:00:00Z"
T1 = "2026-11-01T01:00:00Z"
T2 = "2026-11-01T02:00:00Z"


def _reading(value, measured_at, *, source="gateway-a", point="G-1-M"):
    return normalize_reading({
        "source": source,
        "payload": {
            "point_id": point,
            "point_level": "gun",
            "reading_type": "cumulative",
            "measured_at": measured_at,
            "value_kwh": value,
        },
    })


def _check_row(station="ST-1-M", start=T0, end=T1):
    return CheckResult(station, start, end, "insufficient_evidence", None, None, None, 1.0, 0.0, (), ())


def test_upsert_is_idempotent_and_correction_updates_in_place(tmp_path):
    store = MeteringStore(str(tmp_path / "s.sqlite3"))
    try:
        reading = _reading(500.0, T0)
        assert store.upsert_reading(reading) is True
        assert store.upsert_reading(reading) is False
        corrected = _reading(500.5, T0)
        assert store.upsert_reading(corrected) is True
        assert store.upsert_reading(corrected) is False
        stored = store.readings_for_point("G-1-M", T0, T1)
        assert len(stored) == 1
        assert stored[0].value_kwh == 500.5
        assert stored[0].id is not None
    finally:
        store.close()


def test_rejected_readings_are_recorded(tmp_path):
    store = MeteringStore(str(tmp_path / "r.sqlite3"))
    try:
        store.reject_reading({"source": "gateway-a", "payload": {}}, "point_id is required")
        assert store.counts()["rejected"] == 1
    finally:
        store.close()


def test_stale_marking_only_touches_overlapping_checks(tmp_path):
    db = str(tmp_path / "stale.sqlite3")
    store = MeteringStore(db)
    try:
        # 归属配置把枪测点映射到站测点，过期标记沿此定位受影响的站
        store.replace_config(
            [Attribution("G-1-M", "ST-1-M", "2026-10-31T00:00:00Z", None)],
            [],
        )
        store.save_check(_check_row())
        store.save_check(_check_row(start=T1, end=T2))
        # 周期外的读数不影响任何核查
        store.mark_checks_stale_for_reading(_reading(1.0, "2026-11-01T05:00:00Z"))
        assert store.pending_rechecks() == []
        # 落在 [T0, T1] 内的读数只标记第一个核查
        store.mark_checks_stale_for_reading(_reading(500.0, T0))
        assert store.pending_rechecks() == [{"station_point_id": "ST-1-M", "period_start": T0, "period_end": T1}]
        # 区间读数按其计量周期匹配
        interval = normalize_reading({
            "source": "gateway-a",
            "payload": {
                "point_id": "G-1-M",
                "point_level": "gun",
                "reading_type": "interval",
                "period_start": T1,
                "period_end": T2,
                "measured_at": T2,
                "value_kwh": 4.0,
            },
        })
        store.mark_checks_stale_for_reading(interval)
        assert len(store.pending_rechecks()) == 2
    finally:
        store.close()


def test_config_replace_marks_all_checks_stale(tmp_path):
    db = str(tmp_path / "cfg.sqlite3")
    store = MeteringStore(db)
    try:
        store.save_check(_check_row())
        store.replace_config([], [])
        assert len(store.pending_rechecks()) == 1
    finally:
        store.close()


def test_get_check_returns_none_for_unknown_period(tmp_path):
    db = str(tmp_path / "none.sqlite3")
    store = MeteringStore(db)
    try:
        assert store.get_check("ST-1-M", T0, T1) is None
    finally:
        store.close()


def test_check_without_config_uses_zero_tolerance(tmp_path):
    # 无配置时容差为 0：任何非零差额都确认为超差（带符号）
    db = str(tmp_path / "zero.sqlite3")
    store = MeteringStore(db)
    try:
        store.upsert_reading(_reading(100.0, T0, point="ST-1-M"))
        store.upsert_reading(_reading(101.0, T1, point="ST-1-M"))
    finally:
        store.close()
    result = run_check(db, "ST-1-M", T0, T1)
    assert result["status"] == "confirmed_exceeds_tolerance"
    assert result["station_total_kwh"] == 1.0
    assert result["components_total_kwh"] == 0.0
    assert result["difference_kwh"] == 1.0
