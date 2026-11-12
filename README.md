# 城市充电设施多来源数据融合平台

该项目按单一职责拆分为：

- `sources.py`：读取 fixture 原始记录；
- `normalize.py`：字段映射、标准化、质量校验和稳定去重键；
- `storage.py`：SQLite schema、幂等写入、拒绝记录和统计；
- `pipeline.py`：只编排依赖，不承担文件或 SQL 细节；
- `application.py`：组合依赖并管理资源生命周期；
- `cli.py`：命令行参数和结果输出。

## 数据工程链路

`fixture -> source adapter -> normalization -> deduplication -> quality checks -> SQLite sink`。

默认只使用本地 fixture，不连接真实站点，也不包含账号、连接串或 token。

## 外部依赖类型与版本

- Python 3.11+
- pytest 8.3.x（开发/测试）

## 运行

```bash
PYTHONPATH=src python3 -m charging_fusion.cli --fixture fixtures/sample.json --database /tmp/charging-fusion.sqlite3
PYTHONPATH=src python3 -m pytest tests
```

## HTTP source mode


The default fixture mode remains deterministic and credential-free. For a real JSON endpoint, use:

```bash
PYTHONPATH=src python3 -m charging_fusion.cli \
  --url https://example.invalid/api/records \
  --database /tmp/charging-fusion.sqlite3 \
  --raw-dir /tmp/charging-fusion-raw
```

Replace `charging_fusion` and `charging-fusion` with this repository's package/project name. The endpoint must return a JSON list, or an object containing `records`, `data`, `items`, or `results`. Use `--records-key` for a different nested path. Optional authentication is read from `DATA_SOURCE_BEARER_TOKEN` or `DATA_SOURCE_API_KEY`; do not commit their values. `--raw-dir` stores immutable content-addressed copies of successful HTTP responses. HTTP and JSON failures stop the run instead of being recorded as successful ingestion.

External dependency types and versions: Python 3.11+, pytest 8.3.x for tests. The HTTP adapter uses Python's standard library and adds no runtime package.

## 站枪计量核查（metering reconciliation）

在订单观测之外，平台新增独立的计量输入契约与站枪核查链路。订单观测仍按原契约导入（`cli.py` 与 `observations` 表不变）；计量读数走 `metering_cli.py` 与独立的表。核查只回答"差额是否有计量依据"，不推断订单费用。

### 计量输入契约

每条读数必须显式声明：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `point_id` | 非空字符串 | 测点标识 |
| `point_level` | `station` / `gun` | 站级总表或枪级分表 |
| `reading_type` | `cumulative` / `interval` | 累计量（表码）或区间量（周期电量） |
| `measured_at` | ISO 8601 | 测量时刻，规范化为 UTC |
| `period_start` / `period_end` | ISO 8601 | 计量周期；区间读数必填且有序，累计读数缺省等于 `measured_at` |
| `value_kwh` | 非负有限数值 | 读数值 |
| `meter_epoch` | 字符串，缺省 `main` | 表计代次，换表后必须更换 |

站枪归属（`attributions`，半开区间 `[effective_from, effective_to)`）与站级容差（`tolerance_kwh` / `tolerance_ratio`）通过配置文件整体替换，同一把枪同一时刻不允许归属两个站。

### 读数解释、边界对齐与差额归因的共同约定

- 边界对齐是严格的：只使用测量时刻恰好落在周期边界上的读数，不插值、不补零。
- 累计链同一代次内非递减才可差分；出现负差（归零）或跨代次（换表）时，只有恰好覆盖该分段的区间读数（显式周期证据）才能重新计算增量，绝不把负差取绝对值。
- 恰好覆盖整个周期的区间读数优先于累计差值（它是显式周期证据）。
- 同一读数身份下不同来源取值不一致时返回证据不足，绝不挑选有利来源；同来源修正值按最后一次采集更新。
- 归属区间必须与核查周期完整覆盖；周期内新增枪（归属部分覆盖）直接阻止该周期判断。
- 证据不足时：总量/分量合计/差额一律为空，只返回阻止判断的测点与时间边界，缺少依据的分段不会被包装成确定损失。差额始终带符号存储。

### 原始事实与推导结果

- 原始事实：`meter_readings`、`point_attribution`、`station_check_config`、`rejected_meter_readings`。
- 推导结果：`meter_checks`、`meter_check_components`、`meter_check_evidence`、`meter_check_blockers`，可由原始事实重算，重复核查替换而非累积。
- 重复采集按 `(source, record_key)` 幂等，同一份计量证据不会重复计算；新读数或配置变更会把覆盖该计量周期的核查标记 `stale`，`pending`/`recheck` 定位并重算这些时段。

### 运行

```bash
PYTHONPATH=src python3 -m charging_fusion.metering_cli configure --config fixtures/metering_config.json --database /tmp/metering.sqlite3
PYTHONPATH=src python3 -m charging_fusion.metering_cli ingest --fixture fixtures/metering_base.json --database /tmp/metering.sqlite3
PYTHONPATH=src python3 -m charging_fusion.metering_cli check --station ST-1-M --period-start 2026-11-01T00:00:00Z --period-end 2026-11-01T01:00:00Z --database /tmp/metering.sqlite3
PYTHONPATH=src python3 -m charging_fusion.metering_cli show --station ST-1-M --period-start 2026-11-01T00:00:00Z --period-end 2026-11-01T01:00:00Z --database /tmp/metering.sqlite3
PYTHONPATH=src python3 -m charging_fusion.metering_cli pending --database /tmp/metering.sqlite3
PYTHONPATH=src python3 -m charging_fusion.metering_cli recheck --database /tmp/metering.sqlite3
```

### 可手算样例与场景验收

样例站 `ST-1-M`（容差 1.0 kWh）带枪 `G-1-M`、`G-2-M`，周期 `[00:00, 01:00]`：

- 总表 1000.0 → 1010.0 = **10.0**；G-1 500.0 → 506.0 = **6.0**；G-2 200.0 → 203.5 = **3.5**；
- 分量合计 9.5，差额 **+0.5** ≤ 1.0 → `confirmed_within_tolerance`，6 条参与读数可沿差额逐条追溯。

场景 fixtures 分别触发（对应 `tests/test_metering_acceptance.py`）：

| 场景 | fixture | 预期 |
| --- | --- | --- |
| 归零 | `metering_scenario_reset.json`（G-1 506.0 → 1.5） | `reset_without_period_evidence`，差额为空；补 `..._reset_evidence.json`（区间 4.0）后确认 0.5 |
| 漏报 | `metering_base_missing_g2_end.json` | `missing_boundary`（G-2-M @ 01:00）；补 `..._missing_boundary_fix.json` 后确认 0.5 |
| 换表 | `metering_scenario_meter_change.json`（代次 main → M2） | `meter_change_without_period_evidence`；补区间证据后确认 |
| 归属变化 | `metering_config_attribution.json`（G-3 自 00:30 归属） | [00:00, 01:00] 报 `attribution_partial`；[00:30, 01:00] 确认差额 −0.9（带符号） |
| 边界冲突 | `metering_scenario_conflict.json`（1010.0 vs 1011.0） | `conflicting_readings`，两个分歧值都保留待裁定 |
