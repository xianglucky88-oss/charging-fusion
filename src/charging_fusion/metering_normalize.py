"""计量输入契约：读数、站枪归属和容差配置的字段级校验与规范化。

每条读数必须显式声明：测点（站级/枪级）、读数类型（累计/区间）、计量周期和测量时刻。
累计读数的计量周期是退化的，规范化后 period_start == period_end == measured_at。
"""

import hashlib
import json
import math
from datetime import datetime, timezone

from .metering_models import (
    DEFAULT_METER_EPOCH,
    POINT_LEVELS,
    READING_TYPE_CUMULATIVE,
    READING_TYPE_INTERVAL,
    READING_TYPES,
    Attribution,
    MeterReading,
    StationConfig,
)

_OPEN_END = "9999-12-31T23:59:59Z"


def canonical_timestamp(value: object, field: str) -> str:
    """把 ISO 8601 时间戳规范化为 UTC 'Z' 形式，保证可比较、可排序。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO 8601 timestamp string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO 8601 timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond:
        return parsed.isoformat().replace("+00:00", "Z")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_text(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return " ".join(value.split())


def _non_negative_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def normalize_reading(raw: object) -> MeterReading:
    """把一条原始记录校验并规范化为 MeterReading，不符合契约时抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("record must be an object")
    source_raw = raw.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise ValueError("source is required")
    source = source_raw.strip()
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload object is required")

    point_id = _required_text(payload, "point_id")
    point_level = _required_text(payload, "point_level")
    if point_level not in POINT_LEVELS:
        raise ValueError(f"point_level must be one of {POINT_LEVELS}")
    reading_type = _required_text(payload, "reading_type")
    if reading_type not in READING_TYPES:
        raise ValueError(f"reading_type must be one of {READING_TYPES}")
    epoch_raw = payload.get("meter_epoch")
    meter_epoch = DEFAULT_METER_EPOCH if epoch_raw in (None, "") else _required_text(payload, "meter_epoch")
    measured_at = canonical_timestamp(payload.get("measured_at"), "measured_at")
    value_kwh = _non_negative_number(payload.get("value_kwh"), "value_kwh")

    period_start_raw = payload.get("period_start")
    period_end_raw = payload.get("period_end")
    if reading_type == READING_TYPE_CUMULATIVE:
        # 累计读数的计量周期退化为测量时刻；显式填写时必须一致
        if period_start_raw is not None and canonical_timestamp(period_start_raw, "period_start") != measured_at:
            raise ValueError("cumulative reading period_start must equal measured_at")
        if period_end_raw is not None and canonical_timestamp(period_end_raw, "period_end") != measured_at:
            raise ValueError("cumulative reading period_end must equal measured_at")
        period_start = period_end = measured_at
    else:
        if period_start_raw is None or period_end_raw is None:
            raise ValueError("interval reading requires period_start and period_end")
        period_start = canonical_timestamp(period_start_raw, "period_start")
        period_end = canonical_timestamp(period_end_raw, "period_end")
        if not period_start < period_end:
            raise ValueError("interval period_start must be before period_end")

    record_key = "|".join([point_id, reading_type, meter_epoch, measured_at, period_start, period_end])
    canonical_payload = {
        "point_id": point_id,
        "point_level": point_level,
        "reading_type": reading_type,
        "meter_epoch": meter_epoch,
        "measured_at": measured_at,
        "period_start": period_start,
        "period_end": period_end,
        "value_kwh": value_kwh,
    }
    canonical = json.dumps({"source": source, "payload": canonical_payload}, sort_keys=True, ensure_ascii=False)
    raw_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return MeterReading(
        source=source,
        point_id=point_id,
        point_level=point_level,
        reading_type=reading_type,
        meter_epoch=meter_epoch,
        measured_at=measured_at,
        period_start=period_start,
        period_end=period_end,
        value_kwh=value_kwh,
        record_key=record_key,
        raw_hash=raw_hash,
    )


def normalize_attribution(raw: object) -> Attribution:
    """校验一条站枪归属配置。"""
    if not isinstance(raw, dict):
        raise ValueError("attribution must be an object")
    gun_point_id = _required_text(raw, "gun_point_id")
    station_point_id = _required_text(raw, "station_point_id")
    if gun_point_id == station_point_id:
        raise ValueError("gun_point_id and station_point_id must differ")
    effective_from = canonical_timestamp(raw.get("effective_from"), "effective_from")
    to_raw = raw.get("effective_to")
    effective_to = None if to_raw in (None, "") else canonical_timestamp(to_raw, "effective_to")
    if effective_to is not None and not effective_from < effective_to:
        raise ValueError("effective_from must be before effective_to")
    return Attribution(gun_point_id, station_point_id, effective_from, effective_to)


def normalize_station_config(raw: object) -> StationConfig:
    """校验一条站级容差配置，容差缺省为 0。"""
    if not isinstance(raw, dict):
        raise ValueError("station config must be an object")
    station_point_id = _required_text(raw, "station_point_id")
    tolerance_kwh = _non_negative_number(raw.get("tolerance_kwh", 0.0), "tolerance_kwh")
    tolerance_ratio = _non_negative_number(raw.get("tolerance_ratio", 0.0), "tolerance_ratio")
    return StationConfig(station_point_id, tolerance_kwh, tolerance_ratio)


def validate_attribution_consistency(attributions: list[Attribution]) -> None:
    """同一把枪在同一时刻不允许归属两个不同的站。"""
    by_gun: dict[str, list[Attribution]] = {}
    for attribution in attributions:
        by_gun.setdefault(attribution.gun_point_id, []).append(attribution)
    for gun_point_id, rows in by_gun.items():
        ordered = sorted(rows, key=lambda item: item.effective_from)
        for earlier, later in zip(ordered, ordered[1:]):
            earlier_end = earlier.effective_to or _OPEN_END
            if earlier_end > later.effective_from and earlier.station_point_id != later.station_point_id:
                raise ValueError(
                    f"overlapping attributions for {gun_point_id} to different stations "
                    f"{earlier.station_point_id} and {later.station_point_id}"
                )
