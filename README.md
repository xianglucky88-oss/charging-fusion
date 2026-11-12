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
  --raw-dir /tmp/charging-fusion-raw \
  --cache-dir /tmp/charging-fusion-cache \
  --scope read:stations
```

Replace `charging_fusion` and `charging-fusion` with this repository's package/project name. The endpoint must return a JSON list, or an object containing `records`, `data`, `items`, or `results`. Use `--records-key` for a different nested path. Optional authentication is read from `DATA_SOURCE_BEARER_TOKEN` or `DATA_SOURCE_API_KEY`; do not commit their values. `--raw-dir` stores immutable content-addressed copies of successful HTTP responses. HTTP and JSON failures stop the run instead of being recorded as successful ingestion.

External dependency types and versions: Python 3.11+, pytest 8.3.x for tests. The HTTP adapter uses Python's standard library and adds no runtime package.

## 持久化条件下载缓存（ETag / Last-Modified）

`--cache-dir DIR` 为 JSON HTTP 来源开启持久化条件下载缓存，布局为
`DIR/<request-identity>/{index.json,snapshots/<sha256>.json,.lock}`：

- 服务端在 200 响应上提供 `ETag` 或 `Last-Modified` 时，验证符与**已完整保存、
  且通过原有 UTF-8/JSON 容器校验的字节快照**绑定；校验失败的正文不进入缓存。
- 下一次请求自动带上 `If-None-Match` / `If-Modified-Since`；收到 304 时还原该
  验证符所绑定的快照并重新走容器校验，返回完整记录而不是空数组，且本次
  `downloaded_bytes` 为 0。没有任何验证符的接口始终完整读取、不写缓存条目。
- 请求身份包含 URL 的完整查询语义、`records_key`、调用者声明的 `--scope`，以及
  认证配置的 sha256 指纹（不保存凭据原值）。作用域变化或认证切换会得到不同
  身份目录，不会沿用旧验证符；缓存元数据中不存在 token/API key 原文。
- 交错请求的一致性：每个条件请求在发出前**钉住（pin）**它所验证的快照。采集甲
  以旧验证符发出请求后，即使乙先取回新正文并提交新缓存条目，甲随后收到的 304
  仍按验证符精确匹配回旧快照；甲的 304 路径是只读还原，不做任何缓存写回。被
  钉住的快照不会被历史裁剪回收。
- 崩溃安全：快照与索引都通过「同目录临时文件 + `fsync` + 原子 `replace`」提交，
  索引只在快照落盘后重写；写完快照与提交索引之间崩溃只会留下 `.tmp-*` 文件，
  下次启动清理，任何半份缓存都不会被索引引用。快照缺失、摘要不符或索引损坏
  时，该条目被丢弃并完整重取；没有本地基线却收到 304 会明确报错而非返回空。
- 相同正文轮换验证符时复用同一份快照，并在 `validator_history` 保留该快照的
  全部历史验证符，使每个条件头都能追溯到它实际验证过的那份正文。
- `--raw-dir` 的内容寻址原文落盘能力保持不变。

编程入口可调用 `HttpJsonSource(...).read_with_detail()` 获取 `ReadDetail`，其中
的快照摘要、返回记录与下载字节量用于解释每次命中；这些字段只描述来源读取
结果，不据此推断枪状态、会话或电量。原 `read()` 入口与 fixture/URL 调用方式
保持兼容。
