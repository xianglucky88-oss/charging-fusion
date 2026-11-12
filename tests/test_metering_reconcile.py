from charging_fusion.metering_models import (
    REASON_CONFLICTING_READINGS,
    REASON_METER_CHANGE_WITHOUT_EVIDENCE,
    REASON_MISSING_BOUNDARY,
    REASON_NO_READINGS,
    REASON_RESET_WITHOUT_EVIDENCE,
    MeterReading,
)
from charging_fusion.metering_reconcile import compute_increment

T0 = "2026-11-01T00:00:00Z"
T1 = "2026-11-01T01:00:00Z"
T2 = "2026-11-01T02:00:00Z"


def _reading(rid, value, measured_at, *, epoch="main", rtype="cumulative", source="s1", point="P", start=None, end=None):
    return MeterReading(
        id=rid,
        source=source,
        point_id=point,
        point_level="gun",
        reading_type=rtype,
        meter_epoch=epoch,
        measured_at=measured_at,
        period_start=start if start is not None else measured_at,
        period_end=end if end is not None else measured_at,
        value_kwh=value,
        record_key=f"k{rid}",
        raw_hash=f"h{rid}",
    )


def test_cumulative_pair_increment():
    result = compute_increment("P", [_reading(1, 500.0, T0), _reading(2, 506.0, T1)], T0, T1)
    assert result.ok
    assert result.kwh == 6.0
    assert result.segments[0].kind == "cumulative_pair"
    assert sorted(result.segments[0].evidence_ids) == [1, 2]


def test_exact_interval_reading_is_direct_period_evidence():
    interval = _reading(3, 4.0, T2, rtype="interval", start=T1, end=T2)
    result = compute_increment("P", [interval], T1, T2)
    assert result.ok
    assert result.kwh == 4.0
    assert result.segments[0].kind == "interval"


def test_missing_boundary_is_blocked_not_interpolated():
    result = compute_increment("P", [_reading(1, 500.0, T0)], T0, T1)
    assert not result.ok
    assert result.blocker.reason == REASON_MISSING_BOUNDARY
    assert result.blocker.boundary_start == T1
    assert result.kwh is None


def test_no_readings_is_blocked():
    result = compute_increment("P", [], T0, T1)
    assert not result.ok
    assert result.blocker.reason == REASON_NO_READINGS


def test_zero_reset_is_blocked_and_never_absolutized():
    readings = [_reading(1, 506.0, T1), _reading(2, 1.5, T2)]
    result = compute_increment("P", readings, T1, T2)
    assert not result.ok
    assert result.blocker.reason == REASON_RESET_WITHOUT_EVIDENCE
    assert result.blocker.boundary_start == T1
    assert result.blocker.boundary_end == T2
    # 绝不把负差取绝对值：结果不是 504.5，而是没有增量
    assert result.kwh is None


def test_zero_reset_recomputed_only_with_explicit_interval_evidence():
    readings = [
        _reading(1, 506.0, T1),
        _reading(2, 1.5, T2),
        _reading(3, 4.0, T2, rtype="interval", start=T1, end=T2),
    ]
    result = compute_increment("P", readings, T1, T2)
    assert result.ok
    assert result.kwh == 4.0
    assert result.segments[0].kind == "interval"


def test_meter_change_is_blocked_without_period_evidence():
    readings = [_reading(1, 506.0, T1, epoch="M1"), _reading(2, 10.0, T2, epoch="M2")]
    result = compute_increment("P", readings, T1, T2)
    assert not result.ok
    assert result.blocker.reason == REASON_METER_CHANGE_WITHOUT_EVIDENCE


def test_meter_change_resolved_by_interval_evidence():
    readings = [
        _reading(1, 506.0, T1, epoch="M1"),
        _reading(2, 10.0, T2, epoch="M2"),
        _reading(3, 4.0, T2, rtype="interval", start=T1, end=T2),
    ]
    result = compute_increment("P", readings, T1, T2)
    assert result.ok
    assert result.kwh == 4.0


def test_chain_sums_computable_segments():
    readings = [_reading(1, 500.0, T0), _reading(2, 503.0, T1), _reading(3, 506.0, T2)]
    result = compute_increment("P", readings, T0, T2)
    assert result.ok
    assert result.kwh == 6.0
    assert len(result.segments) == 2


def test_conflicting_sources_block_instead_of_picking_one():
    readings = [
        _reading(1, 500.0, T0, source="s1"),
        _reading(2, 1010.0, T1, source="s1"),
        _reading(3, 1011.0, T1, source="s2"),
    ]
    result = compute_increment("P", readings, T0, T1)
    assert not result.ok
    assert result.blocker.reason == REASON_CONFLICTING_READINGS
    assert sorted(result.blocker.reading_ids) == [2, 3]
    assert result.kwh is None


def test_agreeing_sources_count_once_and_share_evidence():
    readings = [
        _reading(1, 500.0, T0, source="s1"),
        _reading(2, 506.0, T1, source="s1"),
        _reading(3, 506.0, T1, source="s2"),
    ]
    result = compute_increment("P", readings, T0, T1)
    assert result.ok
    assert result.kwh == 6.0
    assert sorted(result.segments[0].evidence_ids) == [1, 2, 3]
