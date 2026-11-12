from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # 跨进程文件锁仅在 POSIX 上可用，Windows 上退化为进程内锁。
    import fcntl
except ImportError:  # pragma: no cover - 平台分支
    fcntl = None  # type: ignore[assignment]


CACHE_SCHEMA_VERSION = 1
# 同一身份最多保留多少份历史验证符/快照，超出后清理未被钉住的最旧条目。
MAX_ENTRIES_PER_IDENTITY = 10


class FixtureSource:
    """只负责从本地 fixture 读取原始记录，不做业务解析或持久化。"""

    def read(self, path: str) -> list[Any]:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("fixture must contain a JSON list")
        return value


@dataclass(frozen=True)
class ReadDetail:
    """一次来源读取的可审计证据。只描述来源读取，不推断枪状态、会话或电量。"""

    records: list[Any]
    state: str  # full | conditional_hit | full_fallback
    status_code: int  # 最终接受的 HTTP 状态码：200 或 304
    downloaded_bytes: int  # 本次实际下载的响应正文字节；304 为 0
    snapshot_digest: str  # 本次返回记录所引用字节快照的 sha256
    snapshot_path: str | None
    etag: str | None
    last_modified: str | None
    request_headers: dict[str, str]  # 实际发出的条件头（不含凭据）
    cache_enabled: bool
    identity: str | None
    note: str = ""


@dataclass
class _Pin:
    """对一次条件请求所验证快照的引用计数，防止交错请求期间被回收。"""

    snapshot: str
    count: int = 1


class ConditionalCache:
    """持久化的条件下载缓存。

    每个请求身份对应一个目录：``index.json`` 只在原子重写后变化，快照文件为
    一次写入、永不原地修改。索引中的每个验证符条目都必须指向一份完整保存且
    sha256 校验通过的字节快照；快照缺失或损坏的条目会被丢弃。缓存元数据只
    保存验证符、快照文件名和非敏感指纹，绝不保存凭据原值。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshot_dir = self.root / "snapshots"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".lock"
        self._thread_lock = threading.RLock()
        self._pins: dict[int, _Pin] = {}
        self._pin_counter = 0
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        self._cleanup_temporary_files()

    @contextmanager
    def transaction(self):
        """进程内互斥 + 同机跨进程互斥，覆盖「快照写入与索引提交」整体。"""
        with self._thread_lock:
            with open(self.lock_path, "r+") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _cleanup_temporary_files(self) -> None:
        """清理上次进程在写完快照/索引后、提交前崩溃留下的临时文件。"""
        for base in (self.root, self.snapshot_dir):
            for temporary in base.glob(".tmp-*"):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load_index(self, *, verify: bool = True) -> dict[str, Any]:
        try:
            raw = self.index_path.read_text(encoding="utf-8")
            index = json.loads(raw)
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            # 索引缺失或损坏：视为没有可用基线，由调用方完整重取；
            # 失去引用的快照目录保留磁盘文件但不会被再次引用。
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": []}
        entries = index.get("entries")
        if not isinstance(index, dict) or not isinstance(entries, list):
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": []}
        if not verify:
            # 测试/诊断用途：跳过快照存在性校验，保留索引原样。
            return {"schema_version": CACHE_SCHEMA_VERSION, "entries": entries}
        verified: list[dict[str, Any]] = []
        for entry in entries:
            if self._entry_snapshot_ok(entry):
                verified.append(entry)
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": verified}

    def _entry_snapshot_ok(self, entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        snapshot = entry.get("snapshot")
        digest = entry.get("snapshot_digest")
        if not isinstance(snapshot, str) or not isinstance(digest, str):
            return False
        if Path(snapshot).name != snapshot or snapshot.startswith("."):
            return False  # 拒绝任何路径逃逸的条目
        path = self.snapshot_dir / snapshot
        try:
            return _sha256_file(path) == digest
        except OSError:
            return False

    def snapshot_path(self, snapshot_name: str) -> Path:
        return self.snapshot_dir / snapshot_name

    def read_snapshot(self, entry: dict[str, Any]) -> bytes:
        path = self.snapshot_dir / entry["snapshot"]
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["snapshot_digest"]:
            raise ValueError("cached snapshot failed digest verification")
        return body

    def pin(self, snapshot_name: str) -> tuple[int, _Pin]:
        with self._thread_lock:
            self._pin_counter += 1
            token = self._pin_counter
            for pin in self._pins.values():
                if pin.snapshot == snapshot_name:
                    pin.count += 1
                    return token, pin
            pin = _Pin(snapshot_name)
            self._pins[token] = pin
            return token, pin

    def unpin(self, token: int) -> None:
        with self._thread_lock:
            pin = self._pins.get(token)
            if pin is None:
                return
            pin.count -= 1
            if pin.count <= 0:
                del self._pins[token]

    def _pinned_snapshots(self) -> set[str]:
        with self._thread_lock:
            return {pin.snapshot for pin in self._pins.values()}

    def write_snapshot(self, body: bytes, digest: str) -> str:
        """原子写入快照；调用方必须已持有 transaction 锁。"""
        snapshot_name = f"{digest}.json"
        target = self.snapshot_dir / snapshot_name
        if target.exists():
            if _sha256_file(target) == digest:
                return snapshot_name
            # 同名但内容不符（极少见的外部破坏）：先写临时文件再替换。
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=self.snapshot_dir,
            prefix=".tmp-snapshot-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return snapshot_name

    def commit_index(self, entries: list[dict[str, Any]]) -> None:
        """原子提交索引；调用方必须已持有 transaction 锁。

        裁剪规则：只保留最近 ``MAX_ENTRIES_PER_IDENTITY`` 个验证符绑定，但任何
        被在途请求钉住的快照对应条目都会加回，避免交错请求的 304 失去基线。
        """
        pinned = self._pinned_snapshots()
        previous = self.load_index(verify=False)["entries"]
        previous_snapshots = {entry["snapshot"] for entry in previous}
        # 同一快照可能因验证符轮换而重复出现，先按快照去重再裁剪。
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            if entry["snapshot"] in seen:
                continue
            seen.add(entry["snapshot"])
            deduped.append(entry)
        kept = deduped[-MAX_ENTRIES_PER_IDENTITY:]
        kept_snapshots = {entry["snapshot"] for entry in kept}
        # 任何被在途请求钉住、但不在最近窗口内的条目都加回；总数可能暂时
        # 超过上限，以保证交错请求的 304 永远能找到它验证的基线。
        for entry in deduped[:-MAX_ENTRIES_PER_IDENTITY]:
            if entry["snapshot"] in pinned and entry["snapshot"] not in kept_snapshots:
                kept.insert(0, entry)
                kept_snapshots.add(entry["snapshot"])
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "entries": kept,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".tmp-index-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.index_path)
        self._prune_orphan_snapshots(kept, pinned, previous_snapshots)

    def _prune_orphan_snapshots(
        self,
        entries: list[dict[str, Any]],
        pinned: set[str],
        previous_snapshots: set[str],
    ) -> None:
        """只回收「上一版索引引用过、新版不再引用且未被钉住」的快照。

        索引从未提及的文件（并发写入中、或其他进程的文件）一律保留，避免
        误删不属于本次提交的数据。
        """
        referenced = {entry["snapshot"] for entry in entries} | pinned
        for name in previous_snapshots - referenced:
            try:
                (self.snapshot_dir / name).unlink()
            except FileNotFoundError:
                pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HttpJsonSource:
    """通过标准库 HTTP 客户端读取 JSON，并可将原始响应落盘。

    当提供 ``cache_dir`` 时启用持久化条件下载缓存：服务端返回的 ETag 与
    Last-Modified 会绑定到已完整保存、并通过原有 JSON 容器校验的字节快照。
    下一次携带条件头请求，304 时还原该快照而不是返回空数组；没有验证符的
    接口始终完整读取。
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        raw_dir: str | None = None,
        records_key: str | None = None,
        bearer_token_env: str = "DATA_SOURCE_BEARER_TOKEN",
        api_key_env: str = "DATA_SOURCE_API_KEY",
        cache_dir: str | None = None,
        scope: str | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must use http:// or https://")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.url = url
        self.timeout = timeout
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.records_key = records_key
        self.bearer_token_env = bearer_token_env
        self.api_key_env = api_key_env
        self.scope = scope
        # 认证配置在构造时冻结：之后环境变量的切换不会改写既有实例的身份，
        # 而新实例会携带新指纹，从而不会沿用旧验证符。只保留非可逆指纹。
        self._auth = self._auth_fingerprint()
        # 缓存根目录；每个请求身份使用独立子目录，互不共享验证符与快照。
        self.cache_root = Path(cache_dir) if cache_dir else None
        # 同一身份复用同一个 ConditionalCache：并发请求因此共享 pin 计数，
        # 交错请求钉住的快照不会被另一线程的历史裁剪回收。
        self._caches: dict[str, ConditionalCache] = {}
        self._caches_guard = threading.Lock()

    def _cache_for(self, identity: str) -> ConditionalCache | None:
        if self.cache_root is None:
            return None
        # 身份是 sha256 十六进制摘要，直接作为目录名，不存在路径逃逸面。
        with self._caches_guard:
            cache = self._caches.get(identity)
            if cache is None:
                cache = ConditionalCache(self.cache_root / identity)
                self._caches[identity] = cache
            return cache

    # ------------------------------------------------------------------ #
    # 身份与凭据
    # ------------------------------------------------------------------ #
    def _credential_values(self) -> tuple[str | None, str | None]:
        # 每次调用实时读取环境，保证认证配置切换能被身份识别捕获。
        return os.environ.get(self.bearer_token_env), os.environ.get(self.api_key_env)

    def _auth_fingerprint(self) -> dict[str, str | None]:
        # 只保存凭据的 sha256 指纹用于判断「认证配置是否切换」，不保存原值。
        bearer, api_key = self._credential_values()
        return {
            "bearer_env": self.bearer_token_env,
            "bearer_sha256": hashlib.sha256(bearer.encode("utf-8")).hexdigest()
            if bearer
            else None,
            "api_key_env": self.api_key_env,
            "api_key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            if api_key
            else None,
        }

    def identity(self) -> str:
        """请求身份：URL 完整查询语义 + records_key + 调用者声明的访问作用域。

        认证配置以非可逆指纹纳入身份；作用域变化或认证切换会得到不同身份，
        从而不会沿用旧验证符。
        """
        parts = urlsplit(self.url)
        material = {
            "scheme": parts.scheme,
            "host": parts.hostname,
            "port": parts.port,
            "path": parts.path,
            # query 原样保留，保证 a=1&a=2、参数顺序等完整查询语义参与身份。
            "query": parts.query,
            "records_key": self.records_key,
            "scope": self.scope,
            "auth": self._auth,
        }
        canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _headers(self, *, conditional: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "data-ingestion-adapter/0.1",
        }
        token, api_key = self._credential_values()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-API-Key"] = api_key
        if conditional:
            headers.update(conditional)
        return headers

    def _save_raw(self, body: bytes) -> None:
        if self.raw_dir is None:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.url.encode("utf-8") + b"\0" + body).hexdigest()
        target = self.raw_dir / f"{digest}.json"
        if target.exists():
            return
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=self.raw_dir, prefix=f".{digest}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
        temporary.replace(target)

    @staticmethod
    def _select_records(value: Any, records_key: str | None) -> list[Any]:
        selected = value
        if records_key:
            for part in records_key.split("."):
                if not isinstance(selected, dict) or part not in selected:
                    raise ValueError(f"records key not found: {records_key}")
                selected = selected[part]
        elif isinstance(selected, dict):
            for key in ("records", "data", "items", "results"):
                candidate = selected.get(key)
                if isinstance(candidate, list):
                    selected = candidate
                    break
        if not isinstance(selected, list):
            raise ValueError("source response must contain a JSON list of records")
        return selected

    # ------------------------------------------------------------------ #
    # 读取入口
    # ------------------------------------------------------------------ #
    def read(self, _path: str | None = None) -> list[Any]:
        """原有入口：返回记录列表，行为与旧版本兼容（缓存未启用时完全一致）。"""
        return self.read_with_detail().records

    def read_with_detail(self) -> ReadDetail:
        identity = self.identity()
        cache = self._cache_for(identity)
        if cache is None:
            return self._full_read(
                cache=None, identity=None, conditional={}, state="full"
            )
        with cache.transaction():
            index = cache.load_index()
            latest = index["entries"][-1] if index["entries"] else None
            if latest is None:
                conditional: dict[str, str] = {}
                pin_token = None
            else:
                conditional = self._conditional_headers(latest)
                pin_token, _pin = cache.pin(latest["snapshot"])
        try:
            return self._request(cache, identity, conditional, pin_token)
        finally:
            # pin 的生命周期统一收口于此：200 完整读取、304 还原、异常都会
            # 经过这里，避免任何路径漏放导致快照被永久钉住、裁剪失效。
            if pin_token is not None:
                cache.unpin(pin_token)

    def _request(
        self,
        cache: ConditionalCache | None,
        identity: str | None,
        conditional: dict[str, str],
        pin_token: int | None,
    ) -> ReadDetail:
        request = urllib.request.Request(
            self.url, headers=self._headers(conditional=conditional), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                if status == 304:
                    return self._handle_not_modified(
                        cache, identity, pin_token, conditional
                    )
                if status != 200:
                    raise RuntimeError(f"source returned HTTP {status}")
                body = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return self._handle_not_modified(
                    cache, identity, pin_token, conditional
                )
            raise RuntimeError(f"source returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"source request failed: {exc.reason}") from exc
        return self._accept_full_body(
            cache,
            body,
            headers,
            identity,
            state="full" if not conditional else "full_fallback",
            sent_conditional=conditional,
        )

    @staticmethod
    def _conditional_headers(entry: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]
        return headers

    def _handle_not_modified(
        self,
        cache: ConditionalCache | None,
        identity: str | None,
        pin_token: int | None,
        sent_conditional: dict[str, str],
    ) -> ReadDetail:
        """304 处理：必须还原它所验证的那份响应，绝不返回空数组。"""
        if cache is None or pin_token is None:
            # 没有本地基线却收到 304：无法还原响应，明确失败而不是返回空。
            raise RuntimeError("source returned HTTP 304 but no cached baseline is available")
        with cache.transaction():
            index = cache.load_index()
            entry = self._matching_baseline(index["entries"], sent_conditional)
            if entry is None:
                baseline_available = False
            else:
                try:
                    body = cache.read_snapshot(entry)
                    baseline_available = True
                except (OSError, ValueError):
                    baseline_available = False
        if not baseline_available:
            # 缓存被删/受损/基线已回收：放弃验证符，完整重取。
            # 快照引用由外层 read_with_detail 的统一释放逻辑负责。
            return self._full_read(
                cache=cache,
                identity=identity,
                conditional={},
                state="full_fallback",
                note="304 baseline missing or corrupt; re-fetched full body",
            )
        # 还原快照后仍需通过原有 JSON 容器校验，保证返回的是可用记录。
        value = self._decode(body)
        records = self._select_records(value, self.records_key)
        return ReadDetail(
            records=records,
            state="conditional_hit",
            status_code=304,
            downloaded_bytes=0,
            snapshot_digest=entry["snapshot_digest"],
            snapshot_path=str(cache.snapshot_path(entry["snapshot"])),
            etag=entry.get("etag"),
            last_modified=entry.get("last_modified"),
            request_headers=dict(sent_conditional),
            cache_enabled=True,
            identity=identity,
            note="304 restored from validated snapshot",
        )

    @staticmethod
    def _bind_validators(
        entry: dict[str, Any], etag: str | None, last_modified: str | None
    ) -> None:
        """把一组验证符绑定到条目快照，保留历史验证符而不是覆盖。"""
        entry.setdefault("validator_history", [])
        binding = {"etag": etag, "last_modified": last_modified}
        if binding not in entry["validator_history"]:
            entry["validator_history"].append(binding)
        entry["etag"] = etag
        entry["last_modified"] = last_modified

    @staticmethod
    def _matching_baseline(
        entries: list[dict[str, Any]], sent_conditional: dict[str, str]
    ) -> dict[str, Any] | None:
        """找到本次条件头实际验证的那条基线。

        交错场景下索引可能已被另一个请求更新：必须按验证符精确匹配（含同一
        快照的历史验证符），而不是拿索引里的最新条目，避免把乙的新正文误当
        成甲的旧响应。
        """

        def matches(entry: dict[str, Any]) -> bool:
            candidates = [entry] + list(entry.get("validator_history", []))
            if "If-None-Match" in sent_conditional:
                wanted = sent_conditional["If-None-Match"]
                return any(c.get("etag") == wanted for c in candidates if c.get("etag"))
            if "If-Modified-Since" in sent_conditional:
                wanted = sent_conditional["If-Modified-Since"]
                return any(
                    c.get("last_modified") == wanted
                    for c in candidates
                    if c.get("last_modified")
                )
            return False

        for entry in reversed(entries):
            if matches(entry):
                return entry
        return None

    def _accept_full_body(
        self,
        cache: ConditionalCache | None,
        body: bytes,
        headers: Any,
        identity: str | None,
        *,
        state: str,
        sent_conditional: dict[str, str],
        note: str = "",
    ) -> ReadDetail:
        # 先做原有 UTF-8/JSON/容器校验，校验不过的正文绝不进入缓存。
        value = self._decode(body)
        records = self._select_records(value, self.records_key)
        etag = headers.get("ETag")
        last_modified = headers.get("Last-Modified")
        digest = hashlib.sha256(body).hexdigest()
        self._save_raw(body)
        snapshot_path: str | None = None
        # 服务端未提供任何验证符时，该响应无法支撑后续条件请求，不写入缓存；
        # 缺少验证符的接口因此始终走完整读取。
        if cache is not None and identity is not None and (etag or last_modified):
            snapshot_path = self._commit_cache_entry(
                cache, body, digest, etag, last_modified
            )
        return ReadDetail(
            records=records,
            state=state,
            status_code=200,
            downloaded_bytes=len(body),
            snapshot_digest=digest,
            snapshot_path=snapshot_path,
            etag=etag,
            last_modified=last_modified,
            request_headers=dict(sent_conditional),
            cache_enabled=cache is not None,
            identity=identity,
            note=note,
        )

    def _commit_cache_entry(
        self,
        cache: ConditionalCache,
        body: bytes,
        digest: str,
        etag: str | None,
        last_modified: str | None,
    ) -> str:
        with cache.transaction():
            index = cache.load_index()
            entries = index["entries"]
            snapshot_name = cache.write_snapshot(body, digest)
            # 相同正文 + 新验证符：复用同一份快照；保留历史验证符绑定并追加
            # 当前验证符，使每个条件头都能找回它实际验证过的那份正文。
            same_snapshot = [
                entry for entry in entries if entry["snapshot_digest"] == digest
            ]
            if same_snapshot:
                target_entry = same_snapshot[-1]
                self._bind_validators(target_entry, etag, last_modified)
                target_entry["updated_at"] = _utc_now()
            else:
                entry: dict[str, Any] = {
                    "etag": etag,
                    "last_modified": last_modified,
                    "snapshot": snapshot_name,
                    "snapshot_digest": digest,
                    "body_bytes": len(body),
                    "stored_at": _utc_now(),
                    "updated_at": _utc_now(),
                }
                # 同一服务端可能同时返回两个验证符，两者都绑定到这份快照。
                self._bind_validators(entry, etag, last_modified)
                entries.append(entry)
            cache.commit_index(entries)
            return str(cache.snapshot_path(snapshot_name))
    def _full_read(
        self,
        *,
        cache: ConditionalCache | None,
        identity: str | None,
        conditional: dict[str, str],
        state: str,
        note: str = "",
    ) -> ReadDetail:
        request = urllib.request.Request(
            self.url, headers=self._headers(conditional=conditional), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"source returned HTTP {response.status}")
                body = response.read()
                headers = response.headers
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"source returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"source request failed: {exc.reason}") from exc
        return self._accept_full_body(
            cache,
            body,
            headers,
            identity,
            state=state,
            sent_conditional=conditional,
            note=note,
        )

    @staticmethod
    def _decode(body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("source response is not valid UTF-8 JSON") from exc


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
