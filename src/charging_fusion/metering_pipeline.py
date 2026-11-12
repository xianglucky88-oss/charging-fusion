"""计量链路的编排：读数导入、配置加载和站枪核查。不承担文件或 SQL 细节。"""

from collections.abc import Iterable
from typing import Any

from .metering_models import CheckResult
from .metering_normalize import (
    normalize_attribution,
    normalize_reading,
    normalize_station_config,
    validate_attribution_consistency,
)
from .metering_reconcile import attribution_overlaps, run_station_check
from .metering_storage import MeteringStore


class MeterReadingIngestionPipeline:
    """编排读数导入：规范化 -> 幂等写入 -> 标记受影响核查为待复核。"""

    def __init__(self, records: Iterable[Any], store: MeteringStore):
        self.records = records
        self.store = store

    def run(self) -> dict[str, int]:
        processed = changed = rejected = 0
        for raw in self.records:
            processed += 1
            try:
                reading = normalize_reading(raw)
            except (TypeError, ValueError, KeyError) as exc:
                rejected += 1
                self.store.reject_reading(raw, str(exc))
                continue
            if self.store.upsert_reading(reading):
                changed += 1
                self.store.mark_checks_stale_for_reading(reading)
        result = {"processed": processed, "changed": changed, "rejected": rejected}
        result.update(self.store.counts())
        return result


def load_config(store: MeteringStore, config_payload: dict[str, Any]) -> dict[str, int]:
    """整体替换站枪归属与容差配置；配置非法时抛错且不落库。"""
    if not isinstance(config_payload, dict):
        raise ValueError("config must be a JSON object")
    attributions = [normalize_attribution(item) for item in config_payload.get("attributions", [])]
    station_configs = [normalize_station_config(item) for item in config_payload.get("station_configs", [])]
    validate_attribution_consistency(attributions)
    store.replace_config(attributions, station_configs)
    return {"attributions": len(attributions), "station_configs": len(station_configs)}


class StationCheckRunner:
    """编排一次站枪核查：取数 -> 纯函数核查 -> 替换式持久化。"""

    def __init__(self, store: MeteringStore):
        self.store = store

    def run(self, station_point_id: str, start: str, end: str) -> CheckResult:
        attributions = self.store.attributions()
        config = self.store.station_config(station_point_id)
        station_readings = self.store.readings_for_point(station_point_id, start, end)
        gun_points = sorted({
            attribution.gun_point_id
            for attribution in attributions
            if attribution.station_point_id == station_point_id
            and attribution_overlaps(attribution, start, end)
        })
        gun_readings = {gun: self.store.readings_for_point(gun, start, end) for gun in gun_points}
        result = run_station_check(
            station_point_id, start, end, station_readings, gun_readings, attributions, config
        )
        self.store.save_check(result)
        return result
