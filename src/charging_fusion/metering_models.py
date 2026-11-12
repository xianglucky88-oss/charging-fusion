"""计量契约与核查结果的领域模型。只定义数据结构，不包含 IO、SQL 或计算逻辑。"""

from dataclasses import dataclass

# 测点级别：站级总表或枪级分表
POINT_LEVEL_STATION = "station"
POINT_LEVEL_GUN = "gun"
POINT_LEVELS = (POINT_LEVEL_STATION, POINT_LEVEL_GUN)

# 读数类型：累计量（表码）或区间量（周期电量）
READING_TYPE_CUMULATIVE = "cumulative"
READING_TYPE_INTERVAL = "interval"
READING_TYPES = (READING_TYPE_CUMULATIVE, READING_TYPE_INTERVAL)

# 未声明表计代次时使用的默认代次
DEFAULT_METER_EPOCH = "main"

# 核查结论状态
STATUS_WITHIN_TOLERANCE = "confirmed_within_tolerance"
STATUS_EXCEEDS_TOLERANCE = "confirmed_exceeds_tolerance"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

# 阻止判断的原因码（证据不足，而非差额）
REASON_NO_READINGS = "no_readings"
REASON_MISSING_BOUNDARY = "missing_boundary"
REASON_RESET_WITHOUT_EVIDENCE = "reset_without_period_evidence"
REASON_METER_CHANGE_WITHOUT_EVIDENCE = "meter_change_without_period_evidence"
REASON_CONFLICTING_READINGS = "conflicting_readings"
REASON_ATTRIBUTION_PARTIAL = "attribution_partial"


@dataclass(frozen=True)
class MeterReading:
    """一条计量读数（原始事实）。id 由存储层分配，落库前为 None。"""

    source: str
    point_id: str
    point_level: str
    reading_type: str
    meter_epoch: str
    measured_at: str
    period_start: str
    period_end: str
    value_kwh: float
    record_key: str
    raw_hash: str
    id: int | None = None


@dataclass(frozen=True)
class Attribution:
    """枪级测点归属站级测点的有效区间，[effective_from, effective_to) 半开，None 表示开放。"""

    gun_point_id: str
    station_point_id: str
    effective_from: str
    effective_to: str | None


@dataclass(frozen=True)
class StationConfig:
    """站级核查容差配置。"""

    station_point_id: str
    tolerance_kwh: float = 0.0
    tolerance_ratio: float = 0.0


@dataclass(frozen=True)
class Segment:
    """增量的一段可计算分片及其参与读数。"""

    start: str
    end: str
    kwh: float
    kind: str  # "cumulative_pair" 或 "interval"
    evidence_ids: tuple[int, ...]


@dataclass(frozen=True)
class Blocker:
    """阻止判断的证据缺口：指明测点、时间边界和原因。"""

    reason: str
    point_id: str
    boundary_start: str
    boundary_end: str
    detail: str
    reading_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Increment:
    """单测点区间增量：要么可计算（ok=True），要么被 Blocker 阻止。"""

    ok: bool
    kwh: float | None
    segments: tuple[Segment, ...]
    blocker: Blocker | None


@dataclass(frozen=True)
class ComponentResult:
    """核查中一个测点（站或枪）的增量结果。"""

    point_id: str
    role: str  # "station" 或 "gun"
    status: str  # "ok" 或 "blocked"
    kwh: float | None
    evidence_ids: tuple[int, ...]
    blocker: Blocker | None


@dataclass(frozen=True)
class CheckResult:
    """一次站枪核查的完整结果。证据不足时总量/分量合计/差额一律为 None。"""

    station_point_id: str
    period_start: str
    period_end: str
    status: str
    station_total_kwh: float | None
    components_total_kwh: float | None
    difference_kwh: float | None
    tolerance_kwh: float
    tolerance_ratio: float
    components: tuple[ComponentResult, ...]
    blockers: tuple[Blocker, ...]

    @property
    def check_id(self) -> str:
        return f"{self.station_point_id}|{self.period_start}|{self.period_end}"
