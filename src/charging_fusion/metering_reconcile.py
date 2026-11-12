"""增量计算与站枪核查的纯函数。

共同约定：
- 边界对齐是严格的：只使用测量时刻恰好落在周期边界上的读数，绝不插值、不补零；
- 累计读数同一代次内出现负差（归零）或跨代次（换表）时，只有恰好覆盖该分段的
  区间读数（显式周期证据）才能重新计算增量，绝不把负差取绝对值；
- 同一读数身份下不同来源取值不一致时返回证据不足，绝不挑选一个有利的来源；
- 区间读数恰好覆盖整个核查周期时优先于累计差值（它是显式的周期证据）。
"""

from .metering_models import (
    REASON_ATTRIBUTION_PARTIAL,
    REASON_CONFLICTING_READINGS,
    REASON_METER_CHANGE_WITHOUT_EVIDENCE,
    REASON_MISSING_BOUNDARY,
    REASON_NO_READINGS,
    REASON_RESET_WITHOUT_EVIDENCE,
    READING_TYPE_CUMULATIVE,
    READING_TYPE_INTERVAL,
    STATUS_EXCEEDS_TOLERANCE,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_WITHIN_TOLERANCE,
    Attribution,
    Blocker,
    CheckResult,
    ComponentResult,
    Increment,
    MeterReading,
    Segment,
    StationConfig,
)

EPSILON = 1e-9


def _identity(reading: MeterReading) -> tuple:
    return (
        reading.reading_type,
        reading.meter_epoch,
        reading.measured_at,
        reading.period_start,
        reading.period_end,
    )


def _blocked(reason: str, point_id: str, start: str, end: str, detail: str, reading_ids: tuple[int, ...] = ()) -> Increment:
    return Increment(False, None, (), Blocker(reason, point_id, start, end, detail, reading_ids))


def compute_increment(
    point_id: str,
    readings: list[MeterReading],
    start: str,
    end: str,
) -> Increment:
    """计算单个测点在 [start, end] 内的电量增量，证据不足时返回 Blocker。"""
    if not readings:
        return _blocked(REASON_NO_READINGS, point_id, start, end, "no readings for point within period")

    # 按读数身份分组；组内取值不一致即来源分歧，直接阻止，不挑选来源
    groups: dict[tuple, list[MeterReading]] = {}
    for reading in readings:
        groups.setdefault(_identity(reading), []).append(reading)
    grouped: list[tuple[MeterReading, tuple[int, ...]]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.id if item.id is not None else 0)
        values = {round(item.value_kwh, 9) for item in ordered}
        if len(values) > 1:
            first = ordered[0]
            detail = "sources disagree on {} reading at {}: {}".format(
                first.reading_type,
                first.measured_at,
                ", ".join(f"id={item.id} source={item.source} value={item.value_kwh}" for item in ordered),
            )
            return _blocked(
                REASON_CONFLICTING_READINGS,
                point_id,
                first.period_start,
                first.period_end,
                detail,
                tuple(item.id for item in ordered),
            )
        grouped.append((ordered[0], tuple(item.id for item in ordered)))

    intervals = [item for item in grouped if item[0].reading_type == READING_TYPE_INTERVAL]
    cumulative = sorted(
        (item for item in grouped if item[0].reading_type == READING_TYPE_CUMULATIVE),
        key=lambda item: (item[0].measured_at, item[0].id if item[0].id is not None else 0),
    )

    # 显式周期证据：恰好覆盖整个周期的区间读数优先
    for representative, evidence_ids in intervals:
        if representative.period_start == start and representative.period_end == end:
            segment = Segment(start, end, representative.value_kwh, "interval", evidence_ids)
            return Increment(True, representative.value_kwh, (segment,), None)

    if not cumulative:
        return _blocked(
            REASON_MISSING_BOUNDARY,
            point_id,
            start,
            end,
            "no cumulative boundary readings and no interval reading exactly covering the period",
        )
    if cumulative[0][0].measured_at != start:
        return _blocked(
            REASON_MISSING_BOUNDARY,
            point_id,
            start,
            start,
            f"missing cumulative reading at period start {start}",
        )
    if cumulative[-1][0].measured_at != end:
        return _blocked(
            REASON_MISSING_BOUNDARY,
            point_id,
            end,
            end,
            f"missing cumulative reading at period end {end}",
        )

    segments: list[Segment] = []
    for (reading_a, ids_a), (reading_b, ids_b) in zip(cumulative, cumulative[1:]):
        seg_start, seg_end = reading_a.measured_at, reading_b.measured_at
        same_epoch = reading_a.meter_epoch == reading_b.meter_epoch
        if same_epoch and reading_b.value_kwh >= reading_a.value_kwh - EPSILON:
            kwh = max(0.0, reading_b.value_kwh - reading_a.value_kwh)
            segments.append(Segment(seg_start, seg_end, kwh, "cumulative_pair", ids_a + ids_b))
            continue
        # 归零（同代次负差）或换表（跨代次）：只有恰好覆盖该分段的区间读数才算周期证据
        reason = REASON_RESET_WITHOUT_EVIDENCE if same_epoch else REASON_METER_CHANGE_WITHOUT_EVIDENCE
        evidence = next(
            (item for item in intervals if item[0].period_start == seg_start and item[0].period_end == seg_end),
            None,
        )
        if evidence is None:
            if same_epoch:
                detail = (
                    f"cumulative value drops from {reading_a.value_kwh} to {reading_b.value_kwh} "
                    f"within meter epoch {reading_a.meter_epoch}"
                )
            else:
                detail = f"meter epoch changes from {reading_a.meter_epoch} to {reading_b.meter_epoch}"
            return _blocked(
                reason,
                point_id,
                seg_start,
                seg_end,
                detail + "; explicit interval evidence is required to recompute the increment",
                ids_a + ids_b,
            )
        segments.append(Segment(seg_start, seg_end, evidence[0].value_kwh, "interval", evidence[1]))

    return Increment(True, sum(segment.kwh for segment in segments), tuple(segments), None)


def attribution_overlaps(attribution: Attribution, start: str, end: str) -> bool:
    return attribution.effective_from < end and (attribution.effective_to is None or attribution.effective_to > start)


def attribution_covers(attribution: Attribution, start: str, end: str) -> bool:
    return attribution.effective_from <= start and (attribution.effective_to is None or attribution.effective_to >= end)


def _component(point_id: str, role: str, increment: Increment) -> ComponentResult:
    if increment.ok:
        evidence = tuple(sorted({rid for segment in increment.segments for rid in segment.evidence_ids}))
        return ComponentResult(point_id, role, "ok", increment.kwh, evidence, None)
    return ComponentResult(point_id, role, "blocked", None, (), increment.blocker)


def run_station_check(
    station_point_id: str,
    start: str,
    end: str,
    station_readings: list[MeterReading],
    gun_readings: dict[str, list[MeterReading]],
    attributions: list[Attribution],
    config: StationConfig,
) -> CheckResult:
    """对同一边界时段核查站级总量与枪级分量，返回可确认结论或证据缺口。"""
    blockers: list[Blocker] = []
    components: list[ComponentResult] = []

    # 归属覆盖：与周期有交集但无法覆盖整个周期的枪，阻止判断（如周期内新增枪）
    by_gun: dict[str, list[Attribution]] = {}
    for attribution in attributions:
        if attribution.station_point_id != station_point_id:
            continue
        if attribution_overlaps(attribution, start, end):
            by_gun.setdefault(attribution.gun_point_id, []).append(attribution)
    expected_guns: list[str] = []
    for gun_point_id in sorted(by_gun):
        rows = by_gun[gun_point_id]
        if any(attribution_covers(row, start, end) for row in rows):
            expected_guns.append(gun_point_id)
        else:
            spans = "; ".join(
                f"[{row.effective_from}, {row.effective_to or 'open'})"
                for row in sorted(rows, key=lambda item: item.effective_from)
            )
            first = min(rows, key=lambda item: item.effective_from)
            blockers.append(
                Blocker(
                    REASON_ATTRIBUTION_PARTIAL,
                    gun_point_id,
                    first.effective_from,
                    first.effective_to or "",
                    f"gun {gun_point_id} attribution to {station_point_id} covers {spans}, "
                    f"which does not span the whole check period [{start}, {end}]",
                )
            )

    station_increment = compute_increment(station_point_id, station_readings, start, end)
    components.append(_component(station_point_id, "station", station_increment))
    if not station_increment.ok:
        blockers.append(station_increment.blocker)

    gun_increments: dict[str, Increment] = {}
    for gun_point_id in expected_guns:
        increment = compute_increment(gun_point_id, gun_readings.get(gun_point_id, []), start, end)
        gun_increments[gun_point_id] = increment
        components.append(_component(gun_point_id, "gun", increment))
        if not increment.ok:
            blockers.append(increment.blocker)

    if blockers:
        # 证据不足：总量/合计/差额一律留空，绝不包装成确定损失
        return CheckResult(
            station_point_id,
            start,
            end,
            STATUS_INSUFFICIENT_EVIDENCE,
            None,
            None,
            None,
            config.tolerance_kwh,
            config.tolerance_ratio,
            tuple(components),
            tuple(blockers),
        )

    station_total = station_increment.kwh
    components_total = sum(increment.kwh for increment in gun_increments.values())
    difference = station_total - components_total
    allowed = max(config.tolerance_kwh, config.tolerance_ratio * abs(station_total))
    status = STATUS_WITHIN_TOLERANCE if abs(difference) <= allowed + EPSILON else STATUS_EXCEEDS_TOLERANCE
    return CheckResult(
        station_point_id,
        start,
        end,
        status,
        station_total,
        components_total,
        difference,
        config.tolerance_kwh,
        config.tolerance_ratio,
        tuple(components),
        (),
    )
