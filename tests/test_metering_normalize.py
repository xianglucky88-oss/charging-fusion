import pytest

from charging_fusion.metering_models import DEFAULT_METER_EPOCH
from charging_fusion.metering_normalize import (
    canonical_timestamp,
    normalize_attribution,
    normalize_reading,
    normalize_station_config,
    validate_attribution_consistency,
)


def _cumulative(**overrides):
    payload = {
        "point_id": "G-1-M",
        "point_level": "gun",
        "reading_type": "cumulative",
        "measured_at": "2026-11-01T01:00:00Z",
        "value_kwh": 506.0,
    }
    payload.update(overrides)
    return {"source": "gateway-a", "payload": payload}


def test_cumulative_reading_defaults_period_to_measured_at_and_epoch_to_main():
    reading = normalize_reading(_cumulative())
    assert reading.period_start == "2026-11-01T01:00:00Z"
    assert reading.period_end == "2026-11-01T01:00:00Z"
    assert reading.meter_epoch == DEFAULT_METER_EPOCH
    assert reading.record_key
    assert reading.raw_hash


def test_interval_reading_requires_explicit_period():
    reading = normalize_reading(_cumulative(
        reading_type="interval",
        period_start="2026-11-01T01:00:00Z",
        period_end="2026-11-01T02:00:00Z",
        value_kwh=4.0,
    ))
    assert reading.period_start == "2026-11-01T01:00:00Z"
    assert reading.period_end == "2026-11-01T02:00:00Z"
    with pytest.raises(ValueError, match="period_start"):
        normalize_reading(_cumulative(reading_type="interval", value_kwh=4.0))


def test_interval_period_must_be_ordered():
    with pytest.raises(ValueError, match="before"):
        normalize_reading(_cumulative(
            reading_type="interval",
            period_start="2026-11-01T02:00:00Z",
            period_end="2026-11-01T01:00:00Z",
            value_kwh=4.0,
        ))


def test_cumulative_period_must_equal_measured_at_when_declared():
    with pytest.raises(ValueError, match="measured_at"):
        normalize_reading(_cumulative(period_start="2026-11-01T00:00:00Z"))


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), "12.3", True, None])
def test_value_kwh_must_be_a_non_negative_finite_number(value):
    with pytest.raises(ValueError, match="value_kwh"):
        normalize_reading(_cumulative(value_kwh=value))


@pytest.mark.parametrize("field,value", [
    ("point_level", "transformer"),
    ("reading_type", "pulse"),
])
def test_enums_are_validated(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_reading(_cumulative(**{field: value}))


def test_missing_contract_fields_are_rejected():
    with pytest.raises(ValueError, match="source"):
        normalize_reading({"payload": {}})
    with pytest.raises(ValueError, match="payload"):
        normalize_reading({"source": "gateway-a"})
    with pytest.raises(ValueError, match="point_id"):
        normalize_reading({"source": "gateway-a", "payload": {"point_level": "gun"}})
    with pytest.raises(ValueError, match="measured_at"):
        normalize_reading(_cumulative(measured_at="not-a-time"))


def test_timestamps_are_canonicalized_to_utc():
    assert canonical_timestamp("2026-11-01T08:00:00+08:00", "t") == "2026-11-01T00:00:00Z"
    assert canonical_timestamp("2026-11-01T00:00:00", "t") == "2026-11-01T00:00:00Z"


def test_attribution_contract_and_consistency():
    attribution = normalize_attribution({
        "gun_point_id": "G-1-M",
        "station_point_id": "ST-1-M",
        "effective_from": "2026-11-01T00:00:00Z",
    })
    assert attribution.effective_to is None
    with pytest.raises(ValueError, match="before"):
        normalize_attribution({
            "gun_point_id": "G-1-M",
            "station_point_id": "ST-1-M",
            "effective_from": "2026-11-01T01:00:00Z",
            "effective_to": "2026-11-01T00:00:00Z",
        })
    overlapping = [
        normalize_attribution({
            "gun_point_id": "G-1-M",
            "station_point_id": "ST-1-M",
            "effective_from": "2026-11-01T00:00:00Z",
            "effective_to": "2026-11-01T02:00:00Z",
        }),
        normalize_attribution({
            "gun_point_id": "G-1-M",
            "station_point_id": "ST-2-M",
            "effective_from": "2026-11-01T01:00:00Z",
        }),
    ]
    with pytest.raises(ValueError, match="overlapping"):
        validate_attribution_consistency(overlapping)
    adjacent = [
        overlapping[0],
        normalize_attribution({
            "gun_point_id": "G-1-M",
            "station_point_id": "ST-2-M",
            "effective_from": "2026-11-01T02:00:00Z",
        }),
    ]
    validate_attribution_consistency(adjacent)


def test_station_config_defaults_and_validation():
    config = normalize_station_config({"station_point_id": "ST-1-M"})
    assert config.tolerance_kwh == 0.0
    assert config.tolerance_ratio == 0.0
    with pytest.raises(ValueError, match="tolerance_kwh"):
        normalize_station_config({"station_point_id": "ST-1-M", "tolerance_kwh": -0.1})
