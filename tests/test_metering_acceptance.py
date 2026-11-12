import json
import sqlite3
from pathlib import Path

import pytest

from charging_fusion import metering_cli
from charging_fusion.metering_application import (
    configure_from_fixture,
    ingest_readings_fixture,
    list_pending,
    recheck_pending,
    run_check,
    show_check,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"

STATION = "ST-1-M"
P1_START = "2026-11-01T00:00:00Z"
P1_END = "2026-11-01T01:00:00Z"
P2_START = "2026-11-01T01:00:00Z"
P2_END = "2026-11-01T02:00:00Z"
P3_START = "2026-11-01T00:30:00Z"


def _setup(db, config="metering_config.json", *reading_fixtures):
    configure_from_fixture(str(FIXTURES / config), db)
    for name in reading_fixtures:
        ingest_readings_fixture(str(FIXTURES / name), db)


def _component(result, point_id):
    return next(item for item in result["components"] if item["point_id"] == point_id)


def _assert_not_packaged_as_loss(result):
    """证据不足的不变量：差额/合计永远为空，阻止项非空，被阻分量没有数值。"""
    assert result["status"] == "insufficient_evidence"
    assert result["station_total_kwh"] is None
    assert result["components_total_kwh"] is None
    assert result["difference_kwh"] is None
    assert result["blockers"]
    for component in result["components"]:
        if component["status"] == "blocked":
            assert component["kwh"] is None


# ---- 手算样例：总表 + 两把枪 ----

def test_base_sample_matches_hand_calculation(tmp_path):
    db = str(tmp_path / "base.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json")
    result = run_check(db, STATION, P1_START, P1_END)
    # 手算：站 1010.0-1000.0=10.0；枪 6.0+3.5=9.5；差额 +0.5；容差 1.0 kWh
    assert result["status"] == "confirmed_within_tolerance"
    assert result["station_total_kwh"] == 10.0
    assert result["components_total_kwh"] == 9.5
    assert result["difference_kwh"] == 0.5
    assert result["allowed_kwh"] == 1.0
    assert result["blockers"] == []
    assert _component(result, "G-1-M")["kwh"] == 6.0
    assert _component(result, "G-2-M")["kwh"] == 3.5


def test_difference_traces_back_to_participating_readings(tmp_path):
    db = str(tmp_path / "trace.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json")
    run_check(db, STATION, P1_START, P1_END)
    shown = show_check(db, STATION, P1_START, P1_END)
    assert shown["difference_kwh"] == 0.5
    evidence = {row["id"]: row for row in shown["evidence"]}
    assert len(evidence) == 6
    components = {item["point_id"]: item for item in shown["components"]}
    station_values = sorted(evidence[rid]["value_kwh"] for rid in components[STATION]["evidence_reading_ids"])
    g1_values = sorted(evidence[rid]["value_kwh"] for rid in components["G-1-M"]["evidence_reading_ids"])
    g2_values = sorted(evidence[rid]["value_kwh"] for rid in components["G-2-M"]["evidence_reading_ids"])
    assert station_values == [1000.0, 1010.0]
    assert g1_values == [500.0, 506.0]
    assert g2_values == [200.0, 203.5]
    # 差额可由证据重算：(1010-1000) - ((506-500) + (203.5-200))
    recomputed = (station_values[1] - station_values[0]) - ((g1_values[1] - g1_values[0]) + (g2_values[1] - g2_values[0]))
    assert recomputed == shown["difference_kwh"]


# ---- 重复采集 ----

def test_duplicate_collection_does_not_double_count_evidence(tmp_path):
    db = str(tmp_path / "dup.sqlite3")
    configure_from_fixture(str(FIXTURES / "metering_config.json"), db)
    first = ingest_readings_fixture(str(FIXTURES / "metering_base.json"), db)
    second = ingest_readings_fixture(str(FIXTURES / "metering_base.json"), db)
    assert first["changed"] == 6
    assert second["changed"] == 0
    assert second["readings"] == 6
    first_check = run_check(db, STATION, P1_START, P1_END)
    second_check = run_check(db, STATION, P1_START, P1_END)
    assert first_check["difference_kwh"] == second_check["difference_kwh"] == 0.5
    first_evidence = sorted(_component(first_check, STATION)["evidence_reading_ids"])
    second_evidence = sorted(_component(second_check, STATION)["evidence_reading_ids"])
    assert first_evidence == second_evidence
    # 推导结果替换而非累积，原始事实不变
    assert second_check["check_id"] == first_check["check_id"]
    counts = sqlite3.connect(db).execute("SELECT COUNT(*) FROM meter_checks").fetchone()[0]
    assert counts == 1


# ---- 漏报：分表缺边界读数 ----

def test_missing_boundary_blocks_until_reading_arrives(tmp_path):
    db = str(tmp_path / "missing.sqlite3")
    _setup(db, "metering_config.json", "metering_base_missing_g2_end.json")
    result = run_check(db, STATION, P1_START, P1_END)
    _assert_not_packaged_as_loss(result)
    blocker = result["blockers"][0]
    assert blocker["reason"] == "missing_boundary"
    assert blocker["point_id"] == "G-2-M"
    assert blocker["boundary_start"] == P1_END
    # 缺边界的枪没有被补零，已齐证的枪仍是事实
    assert _component(result, "G-2-M")["kwh"] is None
    assert _component(result, "G-1-M")["kwh"] == 6.0
    # 新补边界点后，该时段被定位待复核，复核后按手算确认
    ingest_readings_fixture(str(FIXTURES / "metering_scenario_missing_boundary_fix.json"), db)
    pending = list_pending(db)
    assert pending == [{"station_point_id": STATION, "period_start": P1_START, "period_end": P1_END}]
    rechecked = recheck_pending(db)["checks"][0]
    assert rechecked["status"] == "confirmed_within_tolerance"
    assert rechecked["difference_kwh"] == 0.5
    assert list_pending(db) == []


# ---- 归零：累计读数复位 ----

def test_zero_reset_requires_explicit_period_evidence(tmp_path):
    db = str(tmp_path / "reset.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json", "metering_scenario_reset.json")
    result = run_check(db, STATION, P2_START, P2_END)
    _assert_not_packaged_as_loss(result)
    blocker = result["blockers"][0]
    assert blocker["reason"] == "reset_without_period_evidence"
    assert blocker["point_id"] == "G-1-M"
    assert (blocker["boundary_start"], blocker["boundary_end"]) == (P2_START, P2_END)
    # 负差 1.5-506.0 没有被取绝对值包装成 504.5
    assert _component(result, "G-1-M")["kwh"] is None
    # 周期证据（区间读数 4.0 kWh）补齐后才能重算
    ingest_readings_fixture(str(FIXTURES / "metering_scenario_reset_evidence.json"), db)
    rechecked = recheck_pending(db)["checks"][0]
    assert rechecked["status"] == "confirmed_within_tolerance"
    assert _component(rechecked, "G-1-M")["kwh"] == 4.0
    assert rechecked["station_total_kwh"] == 8.0
    assert rechecked["components_total_kwh"] == 7.5
    assert rechecked["difference_kwh"] == 0.5


# ---- 换表：表计代次变化 ----

def test_meter_change_requires_explicit_period_evidence(tmp_path):
    db = str(tmp_path / "change.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json", "metering_scenario_meter_change.json")
    result = run_check(db, STATION, P2_START, P2_END)
    _assert_not_packaged_as_loss(result)
    blocker = result["blockers"][0]
    assert blocker["reason"] == "meter_change_without_period_evidence"
    assert blocker["point_id"] == "G-1-M"
    ingest_readings_fixture(str(FIXTURES / "metering_scenario_meter_change_evidence.json"), db)
    rechecked = recheck_pending(db)["checks"][0]
    assert rechecked["status"] == "confirmed_within_tolerance"
    assert _component(rechecked, "G-1-M")["kwh"] == 4.0
    assert rechecked["difference_kwh"] == 0.5


# ---- 站枪归属变化 ----

def test_attribution_change_within_period_blocks_that_period(tmp_path):
    db = str(tmp_path / "attr.sqlite3")
    _setup(db, "metering_config_attribution.json", "metering_base.json", "metering_scenario_attribution.json")
    # G-3-M 在 00:30 才归属本站，[00:00, 01:00] 无法判断
    result = run_check(db, STATION, P1_START, P1_END)
    _assert_not_packaged_as_loss(result)
    blocker = result["blockers"][0]
    assert blocker["reason"] == "attribution_partial"
    assert blocker["point_id"] == "G-3-M"
    assert blocker["boundary_start"] == P3_START
    # [00:30, 01:00] 归属完整覆盖，按手算确认（差额带符号，不取绝对值）
    sub = run_check(db, STATION, P3_START, P1_END)
    assert sub["status"] == "confirmed_within_tolerance"
    assert sub["station_total_kwh"] == 5.0
    assert sub["components_total_kwh"] == pytest.approx(5.9)
    assert sub["difference_kwh"] == pytest.approx(-0.9)
    assert _component(sub, "G-3-M")["kwh"] == pytest.approx(0.4)


# ---- 边界冲突：来源对同一读数有分歧 ----

def test_conflicting_sources_block_instead_of_picking_favorable_one(tmp_path):
    db = str(tmp_path / "conflict.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json", "metering_scenario_conflict.json")
    result = run_check(db, STATION, P1_START, P1_END)
    _assert_not_packaged_as_loss(result)
    blocker = result["blockers"][0]
    assert blocker["reason"] == "conflicting_readings"
    assert blocker["point_id"] == STATION
    assert blocker["boundary_start"] == P1_END
    assert len(blocker["reading_ids"]) == 2
    # 没有按任一来源的值给出总量或差额，冲突读数也不进证据表
    assert _component(result, STATION)["status"] == "blocked"
    shown = show_check(db, STATION, P1_START, P1_END)
    evidence_ids = {row["id"] for row in shown["evidence"]}
    assert evidence_ids.isdisjoint(blocker["reading_ids"])
    # 两个分歧值都作为原始事实保留，供人工裁定
    rows = sqlite3.connect(db).execute(
        "SELECT value_kwh FROM meter_readings WHERE id IN (?, ?)",
        tuple(sorted(blocker["reading_ids"])),
    ).fetchall()
    assert sorted(row[0] for row in rows) == [1010.0, 1011.0]


# ---- 原始事实与推导结果分离 ----

def test_raw_facts_are_untouched_by_repeated_checks(tmp_path):
    db = str(tmp_path / "separation.sqlite3")
    _setup(db, "metering_config.json", "metering_base.json")
    run_check(db, STATION, P1_START, P1_END)
    run_check(db, STATION, P1_START, P1_END)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM meter_readings").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM meter_checks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM meter_check_evidence").fetchone()[0] == 6


# ---- CLI 冒烟 ----

def test_cli_ingest_check_and_show(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "cli.sqlite3")
    monkeypatch.setattr("sys.argv", ["metering", "configure", "--database", db, "--config", str(FIXTURES / "metering_config.json")])
    metering_cli.main()
    monkeypatch.setattr("sys.argv", ["metering", "ingest", "--database", db, "--fixture", str(FIXTURES / "metering_base.json")])
    metering_cli.main()
    monkeypatch.setattr("sys.argv", [
        "metering", "check", "--database", db, "--station", STATION,
        "--period-start", P1_START, "--period-end", P1_END,
    ])
    metering_cli.main()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "confirmed_within_tolerance"
    assert result["difference_kwh"] == 0.5
