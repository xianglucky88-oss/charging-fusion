# 城市充电设施多来源数据融合平台

该项目按单一职责拆分为：

- `sources.py`：读取 fixture 原始记录；
- `normalize.py`：字段映射、标准化、观测身份和质量校验；
- `storage.py`：SQLite schema、幂等写入、拒绝记录和分段构建；
- `reconstruct.py`：按证据重建充电活动片段、边界与未确认项；
- `pipeline.py`：只编排依赖，不承担文件或 SQL 细节；
- `application.py`：组合依赖并管理资源生命周期；
- `cli.py`：命令行参数和结果输出。

## 数据工程链路

`fixture -> source adapter -> normalization -> SQLite evidence -> reconstruction query`。

默认只使用本地 fixture，不连接真实站点，也不包含账号、连接串或 token。

## 充电会话的证据重建

查询回答的是**证据能证明什么**，不是"这条枪有一次会话"。对每个
`(source, station_id, charger_id)` 时间线，返回：

- 已观察到的 `charging` 活动片段（`segments`）；
- 每个片段的 `start_boundary` / `end_boundary`：`earliest`/`latest` 与包含性；
- 每个片段引用的观测行、边界依据（`basis`）和 `unconfirmed` 原因；
- `unresolved` 汇总，以及 `order_hint_links`（订单引用切换、跨片段重复）。

关键约定：

| 主题 | 约定 |
| --- | --- |
| 观测身份 | `(source, station_id, charger_id, observed_at, raw_hash)`。`order_id` 只是来源给的关联**提示**，不参与身份，也不承担订单/计费功能。 |
| 无 `order_id` 的状态 | `available`、`fault` 等采样照常入库；同一时刻的互补状态不会被覆盖。 |
| 排序 | 仅按观测时间，且只在同一来源内。 |
| 不同来源 | 枪号没有明确关联依据时分别回答；`cross_source_notes` 只提示同名，不合并。 |
| 长时间失联 | 超过 `--continuity-gap-seconds`（默认 900）的活动采样切成两段，仅以 `links.previous_segment_id` 标为"可能延续"，不默认一直在充电。 |
| `fault` | 证明该时刻没有充电，只把结束夹在区间内；不解释会话为何结束（`fault_does_not_explain_session_end`）。 |
| 同一时刻状态矛盾 | 两条观测都保留，片段时间线在该时刻被切开，`ambiguous_instant` 记录冲突；不用排序挑一个赢家。 |
| 同一时刻引用矛盾 | 状态仍然明确，该时刻继续作为证据；两个引用都保留并记录 `order_hint_conflict`。 |
| 观测时间 | 统一转成 UTC 再比较；`+01:00` 与 `Z` 的同一时刻归并，无时区的时间戳被拒绝，不猜时区。 |
| 订单引用切换/重复 | 切片并保留 `order_hint_switch` / `order_hint_reused` 链接；绝不按引用合并。 |
| `energy_kwh` | 原值保存（`energy_kwh_preserved`），不计算能量、费用或站枪差额。 |
| 单点采样 | 仍然输出片段，但开始/结束为 `unbounded`，标记 `single_sample_no_complete_session`，不补成完整会话。 |

边界口径：`charging` 采样证明"该时刻在充电"，`available`/`fault` 采样证明
"该时刻未在充电"。因此结束落在"最后一个 `charging` 采样"与"下一个明确的
非充电采样"之间（含两端）；开始落在"上一个明确的非充电采样"与"第一个
`charging` 采样"之间。二者都是夹逼，不是精确时刻。08:00 charging 与 08:20
available 只能说明这期间发生过结束，不能断言恰好在 08:20 停止。

## 运行

```bash
PYTHONPATH=src python3 -m charging_fusion.cli --fixture fixtures/sample.json --database /tmp/charging-fusion.sqlite3
PYTHONPATH=src python3 -m charging_fusion.cli --reconstruct /tmp/charging-fusion.sqlite3
PYTHONPATH=src python3 -m pytest tests
```

`--reconstruct` 会从观测表重新计算分段，并写入一次 `segment_builds` /
`session_segments` 快照。晚到观测补入后再查询，`build_impact.affected`
会按稳定 `segment_id`（锚点观测决定，外加证据重叠匹配）列出
`new` / `revised` / `removed` 的片段及具体变化；`ordinal` 只是显示顺序。

`fixtures/reconstruction_trajectory.json` 配合同样步骤可逐点走读，见
`docs/reconstruction-walkthrough.md`。

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
