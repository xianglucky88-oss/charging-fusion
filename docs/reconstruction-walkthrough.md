# 充电会话证据重建：逐点走读

对应 fixture：`fixtures/reconstruction_trajectory.json`。

```bash
PYTHONPATH=src python3 -m charging_fusion.cli \
  --fixture fixtures/reconstruction_trajectory.json --database /tmp/walk.db
PYTHONPATH=src python3 -m charging_fusion.cli --reconstruct /tmp/walk.db
```

## 轨迹

| # | 来源 | 时刻 (UTC) | 状态 | order_id | energy_kwh |
| --- | --- | --- | --- | --- | --- |
| 1 | operator-a | 07:50 | available | — | — |
| 2 | operator-a | 08:00 | charging | O-1 | 5.0 |
| 3 | operator-a | 08:20 | available | — | — |
| 4 | operator-a | 09:00 | charging | O-2 | 12.0 |
| 5 | operator-a | 09:10 | charging | O-2 | 13.1 |
| 6 | operator-a | 10:00 | fault | — | — |
| 7 | operator-a | 11:00 | charging | O-3 | 0.5 |
| 8 | operator-a | 12:00（G-02）| charging | O-9 | 3.0 |
| 9 | operator-b | 08:10 | available | — | — |

分段结果（`--reconstruct` 输出，节选）：

```
operator-a / ST-01 / G-01   （9 条观测中本时间线 7 条）
  seg-8feacc66d0e4  obs=[2]
    start [07:50, 08:00)                                               # after_prior_inactive_state
    end   [08:00, 08:20]   next_state_available  certain_bracket
    O-1   energy=[5.0]
    unconfirmed: start_bracketed_not_exact, end_bracketed_not_exact,
                 single_sample_no_complete_session
  seg-6791634f64db  obs=[4,5]
    start [08:20, 09:00)                                               # after_prior_inactive_state
    end   [09:10, 10:00]   next_state_fault      certain_bracket
    O-2   energy=[12.0, 13.1]  links.unobserved_intervals=[09:00→09:10]
    unconfirmed: start_bracketed_not_exact, end_bracketed_not_exact,
                 fault_does_not_explain_session_end
  seg-1b5ff27212c8  obs=[7]
    start [10:00, 11:00)                                               # after_prior_inactive_state
    end   [11:00, None)    no_later_evidence     uncertain_bracket
    O-3   energy=[0.5]
    unconfirmed: start_bracketed_not_exact, end_not_bounded,
                 single_sample_no_complete_session

operator-a / ST-01 / G-02
  seg-4d9a6a9ffe53  obs=[8]
    start (None, 12:00]   unbounded_before_first_evidence
    end   [12:00, None)   no_later_evidence
    O-9   energy=[3.0]
    unconfirmed: start_not_bounded, end_not_bounded, single_sample_no_complete_session

operator-b / ST-01 / G-01
  segments = []          # 只有 available，没有任何 charging 证据
  unconfirmed: no_charging_activity_observed (obs=[9])
```

## 逐点解释

1. **07:50 available（#1）**：证明该时刻未充电。它不是会话开始，只是给后续
   `charging` 采样一个下界。
2. **08:00 charging（#2）**：这是唯一直接见证"在充电"的采样。开始边界因此是
   `(07:50, 08:00]`——上一个未充电采样之后、本采样之前，具体时刻没有被采样。
   片段只有 1 个采样，所以额外标记 `single_sample_no_complete_session`。
3. **08:20 available（#3）**：结束边界是 `[08:00, 08:20]`。这正好对应题意：
   只能说明这期间发生过结束，不能断言恰好在 08:20 停止。`upper_inclusive=true`
   是因为 08:20 采样本身证明"该时刻未充电"，并不代表结束就落在 08:20。
4. **09:00 / 09:10 charging（#4、#5，O-2）**：上一个明确状态仍是 08:20
   `available`，所以新片段开始于 `(08:20, 09:00]`。两条同引用采样之间 10 分钟
   没有采样，记录为 `links.unobserved_intervals`，不宣称那 10 分钟一定在充电。
5. **10:00 fault（#6）**：结束边界 `[09:10, 10:00]`。`fault` 只证明该时刻未充电，
   所以它夹住结束，但不解释会话为何结束；`fault_does_not_explain_session_end`
   显式保留。
6. **11:00 charging（#7，O-3）**：10:00 fault 之后的新片段。结束没有后续证据，
   所以 `[11:00, None)` 且 `end_not_bounded`——沉默不等于继续充电。
7. **12:00 G-02 charging（#8）**：不同枪号，独立时间线。单点采样仍然有用：它
   证明该时刻 G-02 在充电，但开始 `(None, 12:00]`、结束 `[12:00, None)` 都不确定。
8. **08:10 operator-b available（#9）**：来源不同且没有明确关联依据，因此单独
   回答。它不并入 operator-a 的 08:00 片段，也不由于"看起来在 session 中间"
   而被当成会话结束证据。`cross_source_notes` 只提示同名设备，`merged=false`。

## 变更演示

以下三个场景都由同一套代码重建，`build_impact` 以稳定 `segment_id` 报告变化。

### A. 迟到插入一个充电点

基线 `fixtures/reconstruction_baseline_1.json`（轨迹 #1–#3：07:50 available、
08:00 charging、08:20 available），随后补入 `08:10 charging / O-1 / 7.0`
（`fixtures/reconstruction_late_arrival.json`）。

```bash
PYTHONPATH=src python3 -m charging_fusion.cli \
  --fixture fixtures/reconstruction_baseline_1.json --database /tmp/walk.db
PYTHONPATH=src python3 -m charging_fusion.cli --reconstruct /tmp/walk.db

PYTHONPATH=src python3 -m charging_fusion.cli \
  --fixture fixtures/reconstruction_late_arrival.json --database /tmp/walk.db
PYTHONPATH=src python3 -m charging_fusion.cli --reconstruct /tmp/walk.db
```

第二次 `--reconstruct` 的 `build_impact`（实测）：

```json
{"kind":"revised", "segment_id":"seg-8feacc66d0e4",
 "previous_segment_id":"seg-8feacc66d0e4",
 "observation_ids":[2,4],
 "changes":{"added_observation_ids":[4],
            "unconfirmed":{"removed":["single_sample_no_complete_session"],
                           "added":[]}}}
```

结果：

- 08:00–08:20 片段并入 08:10 采样，`sample_count` 1→2；
- `single_sample_no_complete_session` 被移除；
- 结束夹逼区间仍是 `[08:10, 08:20]`：结束仍未被定位到 08:20；
- 只有这一段被 `revised`，`ordinal` 没变，其他时间线不受影响。

### B. 同一时刻状态改为冲突

基线 `fixtures/reconstruction_baseline_2.json`（08:00 charging/O-1、08:20
available、09:00 charging/O-2、09:40 charging/O-3），随后在 09:00 补入
`available`（`fixtures/reconstruction_conflict.json`）。实测变更：

```json
[
 {"kind":"removed", "segment_id":"seg-8292e821dfa7",
  "previous_ordinal":2, "changes":{"removed_observation_ids":[3]}},
 {"kind":"revised", "segment_id":"seg-6791634f64db",
  "previous_ordinal":3, "ordinal":2,
  "changes":{"start_boundary":{"before":["2026-11-01T09:00:00Z","after_order_hint_change"],
                               "after": ["2026-11-01T09:00:00Z","after_status_conflict"]},
             "unconfirmed":{"removed":["start_may_be_a_continuation"],"added":[]}}}
]
```

结果：

- 09:00 两个观测都以独立身份保留（`UNIQUE(..., raw_hash)`），
  `ambiguous_instant` 记录 `statuses=["available","charging"]`、`order_hints=["O-2"]`；
- 原先由 09:00 charging 构成的片段（且与 09:40 因引用切换而分开）被
  `removed`，其观测 id 出现在 `changes.removed_observation_ids`；
- 09:40 片段保留，但它的开始依据从"引用切换"改成"状态冲突"
  （`after_status_conflict`），不再带 `start_may_be_a_continuation`；
- 没有任何排序规则选择 available 或 charging 作为赢家：冲突只让该时刻无法
  承担证据角色，不把它解释成停止或开始；08:00 片段也不受牵连。

### C. 同一订单引用重复使用

基线同 B 的 `fixtures/reconstruction_baseline_2.json`，随后补入
`09:30 charging / O-1`（`fixtures/reconstruction_hint_reuse.json`）。

结果：

- 片段数 3→4，两段 O-1 片段不合并；
- `order_hint_links` 增加
  `kind="order_hint_reused", order_hints=["O-1"], earlier_segment_id=seg-…,
  later_segment_id=seg-…`；
- 后段 `reused_order_hints=[前段 segment_id]`，并带
  `order_hint_reused_across_disconnected_fragments`；
- 09:40 的 O-3 片段开始依据由 09:00 改为 09:30，因为它的最近前置证据变了；
  但仍是 `after_order_hint_change`，且 `sample_count=1`；
- 订单引用只是来源提示，重复出现不能把两段接成连续会话。

同样地，同一条枪上引用从 O-1 切到 O-2（如 08:00→09:00 的轨迹）会保留
`order_hint_switch` 链接并切成两段：引用切换被记录，但切换本身不证明会话
边界。C 中 09:00 O-2 → 09:30 O-1 → 09:40 O-3 连续两次切换都被保留为独立
链接。

## 晚到状态的查询路径

`reconstruct_database()` 每次都从 `observations` 表全量重算，只把上一次构建
当作对照基线。因此：

- 晚到观测补入后再调用 `--reconstruct`，`build_impact.affected` 列出
  `new` / `revised` / `removed` 的片段；
- 片段身份由锚点观测（`segment_id`）和证据重叠决定，`ordinal` 只是显示顺序；
- `changes` 给出新增/移除的观测 id，以及开始/结束边界与 `unconfirmed`
  的变化前后值；
- `--no-persist` 可以只查询不写快照。

## 无法确定的部分

所有片段都带 `unconfirmed`：开始/结束只是夹逼区间、单点采样不能补成完整
会话、长间隔不默认充电、`fault` 不解释结束原因、状态冲突和订单引用切换/
重复只保留链接。顶层 `unresolved` 汇总这些项，`policy` 再次声明身份、排序、
来源隔离和能量口径。`energy_kwh` 只按原值保存，不计算能量、费用或站枪差额。
