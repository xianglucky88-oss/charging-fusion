import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .models import Observation
from .reconstruct import boundary_evidence_ids, observation_ids_in, segment_signature


class ObservationStore:
    """Owns SQLite schema, idempotent evidence writes, rejections and segment builds.

    * An observation identity is (source, station_id, charger_id, observed_at,
      raw_hash).  The content hash keeps two contradictory samples at one instant
      as separate evidence instead of letting one overwrite the other.
    * ``order_id`` is a hint column only and is never part of identity.
    * Segments are materialised per build, so after late evidence arrives the
      store can say exactly which fragments were new, revised or removed.
    """

    def __init__(self, database: str):
        self.conn = sqlite3.connect(database)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_observations_table()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS rejected_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                observed_at TEXT,
                error TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS segment_builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                built_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                observation_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_segments (
                build_id INTEGER NOT NULL REFERENCES segment_builds(id) ON DELETE CASCADE,
                timeline_key TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                signature TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                PRIMARY KEY (build_id, segment_id)
            );
        """)
        self.conn.commit()

    def _create_observations_table(self) -> None:
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(observations)")]
        if "record_key" in columns:
            self._migrate_legacy_observations()
            return
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                station_id TEXT NOT NULL,
                charger_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                order_id TEXT,
                payload_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, station_id, charger_id, observed_at, raw_hash)
            );
            CREATE INDEX IF NOT EXISTS observations_timeline
                ON observations(source, station_id, charger_id, observed_at);
        """)
        self.conn.commit()

    def _migrate_legacy_observations(self) -> None:
        """Preserve rows written by the older order-id-keyed schema.

        The legacy record_key has no field boundaries, so the payload is the
        source of station/charger/status.  Unusable legacy rows stay in
        ``observations_legacy_v1`` rather than being guessed at, and the legacy
        table is reported in ``counts()`` so nothing silently disappears.
        """
        self.conn.execute("ALTER TABLE observations RENAME TO observations_legacy_v1")
        self.conn.executescript("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                station_id TEXT NOT NULL,
                charger_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                order_id TEXT,
                payload_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, station_id, charger_id, observed_at, raw_hash)
            );
            CREATE INDEX IF NOT EXISTS observations_timeline
                ON observations(source, station_id, charger_id, observed_at);
        """)
        rows = self.conn.execute(
            "SELECT source, observed_at, payload_json, raw_hash, first_seen_at "
            "FROM observations_legacy_v1"
        ).fetchall()
        for source, observed_at, payload_json, raw_hash, first_seen_at in rows:
            payload = json.loads(payload_json)
            station = payload.get("station_id")
            charger = payload.get("charger_id")
            status = payload.get("status")
            if not (station and charger and status):
                continue
            self.conn.execute("""
                INSERT OR IGNORE INTO observations
                    (source, station_id, charger_id, observed_at, status, order_id,
                     payload_json, raw_hash, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, station, charger, observed_at, status, payload.get("order_id"),
                  payload_json, raw_hash, first_seen_at))
        self.conn.commit()

    def upsert(self, observation: Observation) -> bool:
        payload = observation.payload
        cursor = self.conn.execute("""
            INSERT OR IGNORE INTO observations
                (source, station_id, charger_id, observed_at, status, order_id,
                 payload_json, raw_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            observation.source,
            str(payload.get("station_id")),
            str(payload.get("charger_id")),
            observation.observed_at,
            str(payload.get("status")),
            payload.get("order_id"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            observation.raw_hash,
        ))
        self.conn.commit()
        return cursor.rowcount > 0

    def reject(self, raw: object, error: str) -> None:
        record = raw if isinstance(raw, dict) else {"value": raw}
        self.conn.execute("INSERT INTO rejected_records(source, observed_at, error, raw_json) VALUES (?, ?, ?, ?)",
                          (record.get("source"), record.get("observed_at"), error,
                           json.dumps(record, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        result = {
            "observations": self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "rejected": self.conn.execute("SELECT COUNT(*) FROM rejected_records").fetchone()[0],
        }
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if "observations_legacy_v1" in tables:
            result["untouched_legacy_rows"] = self.conn.execute(
                "SELECT COUNT(*) FROM observations_legacy_v1"
            ).fetchone()[0]
        return result

    def load_observations(self) -> list[dict[str, Any]]:
        """Every evidence row, in canonical order; segmentation always recomputes."""
        rows = self.conn.execute("""
            SELECT id, source, station_id, charger_id, observed_at, status, order_id,
                   payload_json, raw_hash, first_seen_at
            FROM observations
            ORDER BY source, station_id, charger_id, observed_at, id
        """).fetchall()
        observations = []
        for row in rows:
            payload = json.loads(row[7])
            observations.append({
                "id": row[0],
                "source": row[1],
                "station_id": row[2],
                "charger_id": row[3],
                "observed_at": row[4],
                "status": row[5],
                "order_hint": row[6],
                "energy_kwh": payload.get("energy_kwh"),
                "payload": payload,
                "raw_hash": row[8],
                "first_seen_at": row[9],
            })
        return observations

    def replace_segments(self, segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Persist a new build and report which fragments the new evidence affected."""
        new_segments = list(segments)
        previous_build = self.conn.execute("SELECT MAX(id) FROM segment_builds").fetchone()[0]
        previous_details: list[dict[str, Any]] = []
        if previous_build is not None:
            for timeline_key, segment_id, ordinal, signature, detail_json in self.conn.execute("""
                SELECT timeline_key, segment_id, ordinal, signature, detail_json
                FROM session_segments WHERE build_id = ? ORDER BY timeline_key, ordinal
            """, (previous_build,)):
                detail = json.loads(detail_json)
                previous_details.append({
                    **detail,
                    "timeline_key": timeline_key,
                    "segment_id": segment_id,
                    "ordinal": ordinal,
                    "signature": signature,
                    "observation_ids": sorted(observation_ids_in(detail)),
                })
        observation_count = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        cursor = self.conn.execute(
            "INSERT INTO segment_builds(observation_count) VALUES (?)", (observation_count,)
        )
        build_id = cursor.lastrowid
        for segment in new_segments:
            self.conn.execute("""
                INSERT INTO session_segments(build_id, timeline_key, segment_id, ordinal,
                                             signature, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (build_id, segment["timeline_key"], segment["segment_id"], segment["ordinal"],
                  segment["signature"], json.dumps(segment, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()
        return {
            "build_id": build_id,
            "previous_build_id": previous_build,
            "observation_count": observation_count,
            **_compare_builds(previous_details, new_segments),
        }

    def latest_segments(self) -> list[dict[str, Any]]:
        """Read the most recent persisted materialisation."""
        latest = self.conn.execute("SELECT MAX(id) FROM segment_builds").fetchone()[0]
        if latest is None:
            return []
        rows = self.conn.execute("""
            SELECT detail_json FROM session_segments
            WHERE build_id = ? ORDER BY timeline_key, ordinal
        """, (latest,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        self.conn.close()


def _compare_builds(
    previous: list[dict[str, Any]], new_segments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe changed fragments by evidence, not by list position.

    A fragment keeps its published ``segment_id`` when its anchor observation
    survives.  A late sample that changes the anchor is still recognised as the
    *same* fragment by evidence overlap, so it is reported as ``revised`` rather
    than as an unrelated removal plus addition.
    """
    new_by_id = {segment["segment_id"]: segment for segment in new_segments}
    previous_by_id = {segment["segment_id"]: segment for segment in previous}
    matched_previous: set[str] = set()
    affected: list[dict[str, Any]] = []

    # Unchanged segments are matched first, so a candidate moved to a new
    # segment_id is not later mistaken for a revision of an unrelated fragment.
    for segment_id, segment in new_by_id.items():
        old = previous_by_id.get(segment_id)
        if old is not None and old["signature"] == segment["signature"]:
            matched_previous.add(segment_id)

    for segment_id, segment in new_by_id.items():
        old = previous_by_id.get(segment_id)
        if old is not None and old["signature"] == segment["signature"]:
            continue
        if old is None:
            new_ids = observation_ids_in(segment)
            candidates = [
                candidate for candidate in previous
                if candidate["timeline_key"] == segment["timeline_key"]
                and candidate["segment_id"] not in matched_previous
            ]
            overlapping = [
                candidate for candidate in candidates
                if set(candidate["observation_ids"]) & new_ids
            ]
            if overlapping:
                old = max(
                    overlapping,
                    key=lambda candidate: len(set(candidate["observation_ids"]) & new_ids),
                )
        affected.append({
            "kind": "revised" if old is not None else "new",
            "timeline_key": segment["timeline_key"],
            "segment_id": segment_id,
            "previous_segment_id": old["segment_id"] if old is not None else None,
            "ordinal": segment["ordinal"],
            "previous_ordinal": old["ordinal"] if old is not None else None,
            "observation_ids": sorted(observation_ids_in(segment)),
            "boundary_evidence_ids": sorted(boundary_evidence_ids(segment)),
            "changes": _describe_changes(old, segment),
        })
        if old is not None:
            matched_previous.add(old["segment_id"])

    for segment_id, old in previous_by_id.items():
        if segment_id in matched_previous or segment_id in new_by_id:
            continue
        affected.append({
            "kind": "removed",
            "timeline_key": old["timeline_key"],
            "segment_id": segment_id,
            "previous_segment_id": segment_id,
            "ordinal": None,
            "previous_ordinal": old["ordinal"],
            "observation_ids": sorted(old["observation_ids"]),
            "boundary_evidence_ids": sorted(boundary_evidence_ids(old)),
            "changes": _describe_changes(old, None),
        })

    unchanged = sum(
        1 for segment_id, segment in new_by_id.items()
        if segment_id in previous_by_id
        and previous_by_id[segment_id]["signature"] == segment["signature"]
    )
    return {
        "first_build": not previous,
        "affected": affected,
        "unchanged_segment_count": unchanged,
        "changed_segment_count": len(affected),
        "note": (
            "Segments are identified by stable segment_id plus evidence overlap; "
            "ordinal is only display order."
        ),
    }


def _describe_changes(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """A small, readable diff between two versions of one fragment."""
    if old is None:
        return {"added_observation_ids": sorted(observation_ids_in(new or {}))}
    if new is None:
        return {"removed_observation_ids": sorted(observation_ids_in(old))}
    old_ids = observation_ids_in(old)
    new_ids = observation_ids_in(new)
    old_start = old.get("start_boundary", {})
    new_start = new.get("start_boundary", {})
    old_end = old.get("end_boundary", {})
    new_end = new.get("end_boundary", {})
    changes: dict[str, Any] = {
        "added_observation_ids": sorted(new_ids - old_ids),
        "removed_observation_ids": sorted(old_ids - new_ids),
    }
    old_start_ids = set(old_start.get("evidence_observation_ids", []))
    new_start_ids = set(new_start.get("evidence_observation_ids", []))
    if old_start_ids != new_start_ids:
        changes["start_boundary_evidence_ids"] = {
            "added": sorted(new_start_ids - old_start_ids),
            "removed": sorted(old_start_ids - new_start_ids),
        }
    old_end_ids = set(old_end.get("evidence_observation_ids", []))
    new_end_ids = set(new_end.get("evidence_observation_ids", []))
    if old_end_ids != new_end_ids:
        changes["end_boundary_evidence_ids"] = {
            "added": sorted(new_end_ids - old_end_ids),
            "removed": sorted(old_end_ids - new_end_ids),
        }
    if old_start.get("basis") != new_start.get("basis") or old_start.get("earliest") != new_start.get("earliest"):
        changes["start_boundary"] = {
            "before": [old_start.get("earliest"), old_start.get("basis")],
            "after": [new_start.get("earliest"), new_start.get("basis")],
        }
    if old_end.get("basis") != new_end.get("basis") or old_end.get("latest") != new_end.get("latest"):
        changes["end_boundary"] = {
            "before": [old_end.get("latest"), old_end.get("basis")],
            "after": [new_end.get("latest"), new_end.get("basis")],
        }
    old_reasons = set(old.get("unconfirmed", []))
    new_reasons = set(new.get("unconfirmed", []))
    if old_reasons != new_reasons:
        changes["unconfirmed"] = {
            "added": sorted(new_reasons - old_reasons),
            "removed": sorted(old_reasons - new_reasons),
        }
    return changes
