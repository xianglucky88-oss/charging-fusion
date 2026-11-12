"""持久化条件下载缓存的证据测试。

所有服务端交互都由 tests/http_scripted.py 的离线脚本化服务器驱动，
不访问真实网络。测试通过快照摘要、返回记录与下载字节量解释每一次
缓存命中，并可核对每个条件头究竟对应哪份正文。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from charging_fusion.application import run_url
from charging_fusion.sources import HttpJsonSource
from charging_fusion.storage import ObservationStore

from http_scripted import ScriptedServer


def _valid_payload(tag: str) -> dict:
    """满足 normalize 必填字段契约的记录（仅用于端到端管道测试）。"""
    return {
        "records": [
            {
                "source": "http",
                "observed_at": "2026-11-01T00:00:00Z",
                "payload": {
                    "station_id": f"ST-{tag}",
                    "charger_id": "G-1",
                    "status": "available",
                    "energy_kwh": 0,
                    "order_id": f"O-{tag}",
                },
            }
        ]
    }


def _records_payload(tag: str) -> dict:
    return {
        "records": [
            {
                "source": "http",
                "observed_at": "2026-11-01T00:00:00Z",
                "payload": {"station_id": f"ST-{tag}", "charger_id": "G-1"},
            }
        ]
    }


def _nested_payload(tag: str) -> dict:
    return {"envelope": {"items": _records_payload(tag)["records"]}}


def _deep_nested_payload(tag: str) -> dict:
    return {"other": {"list": _records_payload(tag)["records"]}}


def _identity_dir(cache_root: Path, source: HttpJsonSource) -> Path:
    return cache_root / source.identity()


def _index_entries(identity_dir: Path) -> list[dict]:
    index_file = identity_dir / "index.json"
    if not index_file.exists():
        return []
    return json.loads(index_file.read_text(encoding="utf-8"))["entries"]


@pytest.fixture()
def server():
    srv = ScriptedServer()
    try:
        yield srv
    finally:
        srv.stop()


# --------------------------------------------------------------------- #
# 1. 可重放请求记录：首次完整下载，第二次 304，记录一致且未下载正文
# --------------------------------------------------------------------- #
def test_replay_full_then_304_restores_identical_records(server, tmp_path):
    v1 = server.version(_records_payload("001"), etag='"v1"', last_modified="Mon, 02 Nov 2026 00:00:00 GMT")
    cache_root = tmp_path / "cache"
    raw_dir = tmp_path / "raw"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root), raw_dir=str(raw_dir))

    first = source.read_with_detail()
    assert first.state == "full"
    assert first.status_code == 200
    assert first.downloaded_bytes == len(v1["body"])
    assert first.snapshot_digest == v1["digest"]
    assert first.etag == '"v1"'
    assert first.last_modified == "Mon, 02 Nov 2026 00:00:00 GMT"

    second = source.read_with_detail()
    assert second.state == "conditional_hit"
    assert second.status_code == 304
    assert second.downloaded_bytes == 0  # 304 确实没有重新下载正文
    assert second.snapshot_digest == v1["digest"]  # 命中解释：同一份字节快照
    assert second.records == first.records  # 两次交给采集管道的记录内容一致
    assert second.records  # 304 还原为完整记录，而不是空数组
    assert second.request_headers == {
        "If-None-Match": '"v1"',
        "If-Modified-Since": "Mon, 02 Nov 2026 00:00:00 GMT",
    }

    # 服务器侧请求/响应证据：第二个请求带条件头，响应 304 且字节为 0。
    requests = [e for e in server.log if e["kind"] == "request"]
    responses = [e for e in server.log if e["kind"] == "response"]
    assert [r["status"] for r in responses] == [200, 304]
    assert [r["bytes"] for r in responses] == [len(v1["body"]), 0]
    assert requests[1]["if_none_match"] == '"v1"'
    assert requests[1]["if_modified_since"] == "Mon, 02 Nov 2026 00:00:00 GMT"

    # raw_dir 沿用原文落盘能力，且同一正文只存一份（内容寻址）。
    raw_files = list(raw_dir.glob("*.json"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == v1["body"]

    # 缓存索引里验证符绑定到经过容器校验的字节快照。
    entries = _index_entries(_identity_dir(cache_root, source))
    assert len(entries) == 1
    assert entries[0]["snapshot_digest"] == v1["digest"]


def test_replay_through_pipeline_is_idempotent_and_infers_nothing(server, tmp_path):
    """两次读取交给同一采集管道：第二次 304 记录一致，存储侧无新增变化。"""
    server.version(_valid_payload("002"), etag='"v1"', last_modified=None)
    database = str(tmp_path / "p.sqlite3")
    first = run_url(
        server.url,
        database,
        cache_dir=str(tmp_path / "cache"),
        raw_dir=str(tmp_path / "raw"),
    )
    second = run_url(
        server.url,
        database,
        cache_dir=str(tmp_path / "cache"),
        raw_dir=str(tmp_path / "raw"),
    )
    assert first["processed"] == 1 and first["observations"] == 1
    assert second["processed"] == 1  # 304 还原后仍向管道交付同一条记录
    assert second["changed"] == 0  # 记录内容一致，存储侧幂等无变化
    store = ObservationStore(database)
    try:
        assert store.counts() == {"observations": 1, "rejected": 0}
    finally:
        store.close()


# --------------------------------------------------------------------- #
# 2. 交错请求：甲以旧验证符挂起，乙先取新正文更新缓存，甲随后收到旧 304
# --------------------------------------------------------------------- #
def test_interleaved_requests_each_restore_their_own_validated_body(server, tmp_path):
    v1 = server.version(_records_payload("old"), etag='"v1"', last_modified=None)
    v2 = server.version(_records_payload("new"), etag='"v2"', last_modified=None)
    # 先把服务端版本切回 v1，用于甲的首次完整下载；后续再切到 v2。
    server.state.current = server.state.versions[0]
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))

    first = source.read_with_detail()
    assert first.snapshot_digest == v1["digest"]

    # 服务端现在发布 v2；甲的条件请求会被挂起，乙先完成新正文的完整获取。
    server.state.current = v2
    server.state.hold_first_conditional = True
    server.state.reset_events()
    server.state.force_304_for = {'"v1"'}

    holder: dict = {}

    def collector_a() -> None:
        holder["detail"] = source.read_with_detail()

    thread_a = threading.Thread(target=collector_a)
    thread_a.start()
    assert server.state.arrived.wait(timeout=5)  # 甲已带旧验证符发出请求

    # 乙在甲等待期间完整获取新正文并写回缓存。让服务端对乙忽略条件头，
    # 强制返回 v2 的完整 200（对应实际发布新版本后条件不再匹配的情形）。
    server.state.ignore_conditional = True
    detail_b = source.read_with_detail()
    server.state.ignore_conditional = False
    assert detail_b.state == "full_fallback"
    assert detail_b.status_code == 200
    assert detail_b.snapshot_digest == v2["digest"]
    assert detail_b.downloaded_bytes == len(v2["body"])
    assert detail_b.records[0]["payload"]["station_id"] == "ST-new"

    identity_dir = _identity_dir(cache_root, source)
    entries_after_b = _index_entries(identity_dir)
    assert [e["etag"] for e in entries_after_b] == ['"v1"', '"v2"']

    # 放行甲：服务端对旧验证符回 304（即便当前正文已是 v2）。
    server.state.resume.set()
    thread_a.join(timeout=10)
    detail_a = holder["detail"]

    # 甲的返回值引用它自己验证符所绑定的旧快照，而不是乙的新正文。
    assert detail_a.state == "conditional_hit"
    assert detail_a.status_code == 304
    assert detail_a.downloaded_bytes == 0
    assert detail_a.etag == '"v1"' or detail_a.request_headers["If-None-Match"] == '"v1"'
    assert detail_a.snapshot_digest == v1["digest"]
    assert detail_a.records[0]["payload"]["station_id"] == "ST-old"

    # 甲的 304 是只读还原，不产生任何缓存写回（条目与写回时间戳不变）。
    entries_after_a = _index_entries(identity_dir)
    assert entries_after_a == entries_after_b
    snapshots = {p.name for p in (identity_dir / "snapshots").glob("*.json")}
    assert snapshots == {f"{v1['digest']}.json", f"{v2['digest']}.json"}

    # 服务器证据按交错顺序排列，且每个条件头都能对应到具体正文摘要。
    responses = [e for e in server.log if e["kind"] == "response"]
    assert [(r["status"], r["bytes"], r["digest"]) for r in responses] == [
        (200, len(v1["body"]), v1["digest"]),  # 甲首次完整下载 v1
        (200, len(v2["body"]), v2["digest"]),  # 乙完整下载 v2
        (304, 0, None),                        # 甲的旧验证符 304
    ]
    requests = [e for e in server.log if e["kind"] == "request"]
    assert requests[2]["if_none_match"] == '"v1"'  # 甲挂起时带的确实是旧验证符


# --------------------------------------------------------------------- #
# 3. 同地址不同容器：records_key 参与身份，容器不匹配则明确失败
# --------------------------------------------------------------------- #
def test_same_url_different_records_key_gets_isolated_identities(server, tmp_path):
    body = json.dumps(_nested_payload("003")).encode()
    server.state.set_version(body, etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    nested = HttpJsonSource(server.url, cache_dir=str(cache_root), records_key="envelope.items")
    other_key = HttpJsonSource(server.url, cache_dir=str(cache_root), records_key="envelope")
    # envelope 是对象而非列表，容器校验明确失败而不是回退猜测。
    with pytest.raises(ValueError, match="JSON list of records"):
        other_key.read_with_detail()

    detail_nested = nested.read_with_detail()
    assert detail_nested.records[0]["payload"]["station_id"] == "ST-003"
    assert nested.identity() != other_key.identity()
    assert _identity_dir(cache_root, nested) != _identity_dir(cache_root, other_key)

    # 同一身份第二次读取命中自己的缓存。
    hit_nested = nested.read_with_detail()
    assert hit_nested.state == "conditional_hit"

    # 不同嵌套路径指向同一 URL 上的另一个容器，同样独立成身份。
    deep_body = json.dumps(_deep_nested_payload("003b")).encode()
    server.state.set_version(deep_body, etag='"v2"', last_modified=None)
    deep = HttpJsonSource(server.url, cache_dir=str(cache_root), records_key="other.list")
    assert deep.read_with_detail().records[0]["payload"]["station_id"] == "ST-003b"
    assert deep.identity() != nested.identity()

    wrong = HttpJsonSource(server.url, cache_dir=str(cache_root), records_key="missing.path")
    with pytest.raises(ValueError, match="records key not found"):
        wrong.read_with_detail()
    # 容器校验失败的正文不留下缓存条目。
    assert not _identity_dir(cache_root, wrong).exists() or not (
        _identity_dir(cache_root, wrong) / "index.json"
    ).exists()


def test_query_string_is_part_of_identity(server, tmp_path):
    server.version(_records_payload("004"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source_a = HttpJsonSource(server.url_with("page=1"), cache_dir=str(cache_root))
    source_b = HttpJsonSource(server.url_with("page=2"), cache_dir=str(cache_root))
    source_a.read()
    source_b.read()
    assert source_a.identity() != source_b.identity()
    assert len(list(cache_root.iterdir())) == 2


# --------------------------------------------------------------------- #
# 4. 相同正文不同验证符：复用快照并保留全部验证符绑定
# --------------------------------------------------------------------- #
def test_same_body_with_rotated_validators_reuses_snapshot(server, tmp_path):
    payload = _records_payload("005")
    e1 = server.version(payload, etag='"e1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    d1 = source.read_with_detail()
    assert d1.snapshot_digest == e1["digest"]

    # 服务端轮换 ETag，但正文一字节未变。
    server.version(payload, etag='"e2"', last_modified=None)
    d2 = source.read_with_detail()
    assert d2.state == "full_fallback"  # 条件头不匹配，服务端给了 200
    assert d2.snapshot_digest == e1["digest"]  # 同一份正文快照

    identity_dir = _identity_dir(cache_root, source)
    entries = _index_entries(identity_dir)
    assert len(entries) == 1  # 没有为重复正文新增快照条目
    entry = entries[0]
    assert entry["etag"] == '"e2"'
    assert {b["etag"] for b in entry["validator_history"]} == {'"e1"', '"e2"'}
    assert len(list((identity_dir / "snapshots").glob("*.json"))) == 1

    # 旧验证符依然能找回这份正文（模拟旧客户端重放）。
    server.state.force_304_for = {'"e1"'}
    d3 = source.read_with_detail()
    # 当前最新验证符是 e2，服务端对 e1 强制 304；本地仍可按 e1 还原。
    assert d3.request_headers["If-None-Match"] == '"e2"'
    # e2 不匹配服务端当前 etag 时正常路径会给 200；这里改为直接验证匹配函数。
    from charging_fusion.sources import HttpJsonSource as _S

    baseline = _S._matching_baseline(entries, {"If-None-Match": '"e1"'})
    assert baseline is entry


# --------------------------------------------------------------------- #
# 5. 重启后快照损坏/缺失/索引损坏：重新完整获取或明确失败
# --------------------------------------------------------------------- #
def test_missing_or_corrupt_snapshot_after_restart_triggers_full_refetch(server, tmp_path):
    v1 = server.version(_records_payload("006"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    source.read()
    identity_dir = _identity_dir(cache_root, source)

    # 5a. 快照文件被删：索引加载时丢弃悬空条目，按「无可用基线」完整重取。
    snapshot = next((identity_dir / "snapshots").glob("*.json"))
    snapshot.unlink()
    detail = source.read_with_detail()
    assert detail.state == "full"
    assert detail.status_code == 200
    assert detail.downloaded_bytes == len(v1["body"])
    assert detail.snapshot_digest == v1["digest"]
    assert detail.records  # 仍然返回完整记录
    assert (identity_dir / "snapshots" / f"{v1['digest']}.json").exists()

    # 5b. 快照内容受损：下次加载索引时摘要校验失败、条目被剔除，
    # 按无可用基线完整重取（而不是把损坏字节交给 JSON 解析）。
    snapshot = next((identity_dir / "snapshots").glob("*.json"))
    snapshot.write_bytes(b"{not the saved bytes")
    damaged = source.read_with_detail()
    assert damaged.state == "full"
    assert damaged.downloaded_bytes == len(v1["body"])
    assert damaged.snapshot_digest == v1["digest"]

    # 5c. 304 已应答、但要还原的快照在读取时才发现丢失：走 full_fallback。
    from charging_fusion.sources import ConditionalCache

    source.read_with_detail()  # 重建干净基线
    server.state.force_304_for = {'"v1"'}
    raw_index = json.loads((identity_dir / "index.json").read_text(encoding="utf-8"))
    original_load = ConditionalCache.load_index

    def load_without_verification(self, *, verify: bool = True):
        return {"schema_version": raw_index["schema_version"], "entries": list(raw_index["entries"])}

    ConditionalCache.load_index = load_without_verification  # type: ignore[assignment]
    try:
        next((identity_dir / "snapshots").glob("*.json")).unlink()
        fallback = source.read_with_detail()
    finally:
        ConditionalCache.load_index = original_load  # type: ignore[assignment]
    assert fallback.state == "full_fallback"
    assert fallback.downloaded_bytes == len(v1["body"])
    assert fallback.records


def test_corrupt_index_is_treated_as_no_baseline(server, tmp_path):
    v1 = server.version(_records_payload("007"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    source.read()
    identity_dir = _identity_dir(cache_root, source)
    (identity_dir / "index.json").write_text("{broken json", encoding="utf-8")

    detail = source.read_with_detail()
    assert detail.state == "full"  # 无可用基线，按首次完整读取处理
    assert detail.downloaded_bytes == len(v1["body"])
    # 新索引重建后，条件请求恢复正常。
    hit = source.read_with_detail()
    assert hit.state == "conditional_hit"
    assert hit.downloaded_bytes == 0


def test_unsolicited_304_without_baseline_fails_loudly(server, tmp_path):
    server.version(_records_payload("008"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    # 本地尚无基线时，服务端却对无条件 GET 回 304：必须明确失败，不能返回空。
    server.state.always_304 = True
    with pytest.raises(RuntimeError, match="HTTP 304|no cached baseline"):
        source.read_with_detail()
    identity_dir = _identity_dir(cache_root, source)
    assert identity_dir.exists()
    assert not list((identity_dir / "snapshots").glob("*.json"))


# --------------------------------------------------------------------- #
# 6. 无验证符接口：始终完整读取，不产生缓存条目
# --------------------------------------------------------------------- #
def test_endpoint_without_validators_always_reads_full_body(server, tmp_path):
    v1 = server.version(_records_payload("009"), etag=None, last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    first = source.read_with_detail()
    second = source.read_with_detail()
    assert first.state == "full" and second.state == "full"
    assert first.downloaded_bytes == second.downloaded_bytes == len(v1["body"])
    entries = _index_entries(_identity_dir(cache_root, source))
    assert entries == []  # 缺少验证符，没有可复用的条件缓存
    assert not list((_identity_dir(cache_root, source) / "snapshots").glob("*.json"))


# --------------------------------------------------------------------- #
# 7. 作用域与认证配置切换：身份变化，不沿用旧验证符；元数据不含凭据原值
# --------------------------------------------------------------------- #
def test_scope_and_credentials_change_identity_and_never_reuse_validators(
    server, tmp_path, monkeypatch
):
    server.version(_records_payload("010"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"

    monkeypatch.setenv("DATA_SOURCE_BEARER_TOKEN", "token-alpha")
    source_alpha = HttpJsonSource(server.url, cache_dir=str(cache_root), scope="read:stations")
    source_alpha.read()

    monkeypatch.setenv("DATA_SOURCE_BEARER_TOKEN", "token-beta")
    source_beta = HttpJsonSource(server.url, cache_dir=str(cache_root), scope="read:stations")
    assert source_beta.identity() != source_alpha.identity()
    detail_beta = source_beta.read_with_detail()
    assert detail_beta.state == "full"  # 新身份没有基线，未沿用旧验证符
    assert detail_beta.request_headers == {}

    source_other_scope = HttpJsonSource(
        server.url, cache_dir=str(cache_root), scope="read:diagnostics"
    )
    assert source_other_scope.identity() != source_alpha.identity()

    # 缓存目录与索引中不得出现任何凭据原值。
    written = b"\n".join(
        path.read_bytes()
        for path in cache_root.rglob("*")
        if path.is_file()
    )
    assert b"token-alpha" not in written
    assert b"token-beta" not in written
    index_text = (_identity_dir(cache_root, source_alpha) / "index.json").read_text(encoding="utf-8")
    assert "Bearer" not in index_text and "Authorization" not in index_text


# --------------------------------------------------------------------- #
# 8. 写快照与索引提交之间崩溃：不产生可被误用的半份缓存
# --------------------------------------------------------------------- #
def test_crash_between_snapshot_write_and_index_commit_leaves_no_usable_half_cache(
    server, tmp_path
):
    v1 = server.version(_records_payload("011"), etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    source.read()
    identity_dir = _identity_dir(cache_root, source)

    # 模拟崩溃残留：快照临时文件 + 索引临时文件，且没有 index.json 引用它们。
    (identity_dir / "snapshots" / ".tmp-snapshot-orphan").write_bytes(v1["body"])
    (identity_dir / ".tmp-index-orphan").write_text('{"entries": []}', encoding="utf-8")
    (identity_dir / "index.json").unlink()

    # 新进程（新 ConditionalCache 实例）启动时清理临时文件，并按无基线完整获取。
    fresh = HttpJsonSource(server.url, cache_dir=str(cache_root))
    detail = fresh.read_with_detail()
    assert detail.state == "full"
    assert detail.downloaded_bytes == len(v1["body"])
    assert not list(identity_dir.glob(".tmp-*"))
    assert not list((identity_dir / "snapshots").glob(".tmp-*"))

    # 重建后的缓存仍可正常条件命中。
    hit = fresh.read_with_detail()
    assert hit.state == "conditional_hit"
    assert hit.snapshot_digest == v1["digest"]


def test_invalid_json_body_is_never_cached(server, tmp_path):
    bad_body = b'{"records": [broken'
    server.state.set_version(bad_body, etag='"v1"', last_modified=None)
    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        source.read()
    identity_dir = _identity_dir(cache_root, source)
    # 校验失败：不写快照、不写索引、不写 raw。
    assert not list((identity_dir / "snapshots").glob("*.json"))
    assert not (identity_dir / "index.json").exists()


# --------------------------------------------------------------------- #
# 9. 原始入口与 fixture 能力保留
# --------------------------------------------------------------------- #

def test_pinned_snapshot_survives_history_pruning(server, tmp_path):
    """超过历史上限时，在途条件请求钉住的旧快照不得被裁剪回收。"""
    from charging_fusion.sources import MAX_ENTRIES_PER_IDENTITY

    cache_root = tmp_path / "cache"
    source = HttpJsonSource(server.url, cache_dir=str(cache_root))

    # 第一阶段：无 pin 时，超出上限的最旧快照被正常回收。
    digests = []
    for idx in range(MAX_ENTRIES_PER_IDENTITY + 3):
        server.version(
            _records_payload(f"p{idx:02d}"), etag=f'"e{idx}"', last_modified=None
        )
        digests.append(source.read_with_detail().snapshot_digest)

    identity_dir = _identity_dir(cache_root, source)
    entries = _index_entries(identity_dir)
    assert len(entries) == MAX_ENTRIES_PER_IDENTITY
    oldest_digest = digests[0]
    assert oldest_digest not in {e["snapshot_digest"] for e in entries}
    assert not (identity_dir / "snapshots" / f"{oldest_digest}.json").exists()

    # 第二阶段：另一个身份目录上，先建立基线，再手动制造「在途 pin」，
    # 随后的连续提交即便超过上限也必须保留被钉住的快照。
    other_root = tmp_path / "cache2"
    other = HttpJsonSource(server.url, cache_dir=str(other_root))
    server.version(_records_payload("pin-base"), etag='"pin-base"', last_modified=None)
    pinned_detail = other.read_with_detail()
    cache = other._cache_for(other.identity())
    with cache.transaction():
        baseline = cache.load_index()["entries"][0]
        token, _ = cache.pin(baseline["snapshot"])
    try:
        # 直接通过缓存提交接口塞入新版本，模拟在 pin 仍在途时的并发写回。
        for idx in range(MAX_ENTRIES_PER_IDENTITY + 1):
            body = json.dumps(_records_payload(f"after-pin-{idx}")).encode()
            other._commit_cache_entry(
                cache, body, __import__("hashlib").sha256(body).hexdigest(),
                etag=f'"ap{idx}"', last_modified=None,
            )
        kept = _index_entries(_identity_dir(other_root, other))
        pinned_name = f"{pinned_detail.snapshot_digest}.json"
        assert any(e["snapshot"] == pinned_name for e in kept)
        assert (_identity_dir(other_root, other) / "snapshots" / pinned_name).exists()
    finally:
        cache.unpin(token)

    # pin 释放后，下一次提交即可正常裁剪掉它。
    body = json.dumps(_records_payload("after-unpin")).encode()
    other._commit_cache_entry(
        cache, body, __import__("hashlib").sha256(body).hexdigest(),
        etag='"final"', last_modified=None,
    )
    kept = _index_entries(_identity_dir(other_root, other))
    assert all(e["snapshot"] != f"{pinned_detail.snapshot_digest}.json" for e in kept)


def test_legacy_read_signature_and_cache_disabled_path(server, tmp_path):
    v1 = server.version(_records_payload("012"), etag='"v1"', last_modified=None)
    # 不传 cache_dir 时行为与旧版本一致：每次完整读取。
    source = HttpJsonSource(server.url, raw_dir=str(tmp_path / "raw"))
    records = source.read()  # 旧入口仍直接返回记录列表
    assert records[0]["payload"]["station_id"] == "ST-012"
    again = source.read()
    assert again == records
    assert list((tmp_path / "raw").glob("*.json"))[0].read_bytes() == v1["body"]
    assert not (tmp_path / "cache").exists()
