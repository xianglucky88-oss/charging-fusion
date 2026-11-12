"""计量子系统的组合根：组装依赖、管理资源生命周期、序列化结果。"""

import json
from pathlib import Path
from typing import Any

from .metering_models import CheckResult
from .metering_normalize import canonical_timestamp
from .metering_pipeline import MeterReadingIngestionPipeline, StationCheckRunner, load_config
from .metering_storage import MeteringStore
from .sources import FixtureSource


def check_result_to_dict(result: CheckResult) -> dict[str, Any]:
    allowed = None
    if result.station_total_kwh is not None:
        allowed = max(result.tolerance_kwh, result.tolerance_ratio * abs(result.station_total_kwh))
    return {
        "check_id": result.check_id,
        "station_point_id": result.station_point_id,
        "period_start": result.period_start,
        "period_end": result.period_end,
        "status": result.status,
        "station_total_kwh": result.station_total_kwh,
        "components_total_kwh": result.components_total_kwh,
        "difference_kwh": result.difference_kwh,
        "tolerance_kwh": result.tolerance_kwh,
        "tolerance_ratio": result.tolerance_ratio,
        "allowed_kwh": allowed,
        "components": [
            {
                "point_id": component.point_id,
                "role": component.role,
                "status": component.status,
                "kwh": component.kwh,
                "evidence_reading_ids": list(component.evidence_ids),
            }
            for component in result.components
        ],
        "blockers": [
            {
                "reason": blocker.reason,
                "point_id": blocker.point_id,
                "boundary_start": blocker.boundary_start,
                "boundary_end": blocker.boundary_end,
                "detail": blocker.detail,
                "reading_ids": list(blocker.reading_ids),
            }
            for blocker in result.blockers
        ],
    }


def ingest_readings_fixture(fixture: str, database: str) -> dict[str, int]:
    records = FixtureSource().read(fixture)
    store = MeteringStore(database)
    try:
        return MeterReadingIngestionPipeline(records, store).run()
    finally:
        store.close()


def configure_from_fixture(fixture: str, database: str) -> dict[str, int]:
    payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
    store = MeteringStore(database)
    try:
        return load_config(store, payload)
    finally:
        store.close()


def _canonical_period(period_start: str, period_end: str) -> tuple[str, str]:
    start = canonical_timestamp(period_start, "period_start")
    end = canonical_timestamp(period_end, "period_end")
    if not start < end:
        raise ValueError("period_start must be before period_end")
    return start, end


def run_check(database: str, station_point_id: str, period_start: str, period_end: str) -> dict[str, Any]:
    start, end = _canonical_period(period_start, period_end)
    store = MeteringStore(database)
    try:
        result = StationCheckRunner(store).run(station_point_id, start, end)
        return check_result_to_dict(result)
    finally:
        store.close()


def recheck_pending(database: str) -> dict[str, Any]:
    """重新核查所有被标记的时段，返回最新结论。"""
    store = MeteringStore(database)
    try:
        runner = StationCheckRunner(store)
        pending = store.pending_rechecks()
        results = [runner.run(item["station_point_id"], item["period_start"], item["period_end"]) for item in pending]
        return {"rechecked": len(results), "checks": [check_result_to_dict(result) for result in results]}
    finally:
        store.close()


def list_pending(database: str) -> list[dict[str, str]]:
    store = MeteringStore(database)
    try:
        return store.pending_rechecks()
    finally:
        store.close()


def show_check(database: str, station_point_id: str, period_start: str, period_end: str) -> dict[str, Any] | None:
    start, end = _canonical_period(period_start, period_end)
    store = MeteringStore(database)
    try:
        return store.get_check(station_point_id, start, end)
    finally:
        store.close()
