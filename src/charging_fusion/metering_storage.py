"""计量证据与核查结果的 SQLite 存储。

原始事实表：meter_readings / point_attribution / station_check_config / rejected_meter_readings。
推导结果表：meter_checks / meter_check_components / meter_check_evidence / meter_check_blockers，
可由原始事实与配置重算，重复核查只替换不累积。
"""

import json
import sqlite3

from .metering_models import Attribution, CheckResult, MeterReading, StationConfig


class MeteringStore:
    """只负责计量相关 schema、幂等写入、核查结果持久化和查询。"""

    def __init__(self, database: str):
        self.conn = sqlite3.connect(database)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meter_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                record_key TEXT NOT NULL,
                point_id TEXT NOT NULL,
                point_level TEXT NOT NULL,
                reading_type TEXT NOT NULL,
                meter_epoch TEXT NOT NULL,
                measured_at TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                value_kwh REAL NOT NULL,
                raw_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, record_key)
            );
            CREATE TABLE IF NOT EXISTS rejected_meter_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                error TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS point_attribution (
                gun_point_id TEXT NOT NULL,
                station_point_id TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                PRIMARY KEY (gun_point_id, station_point_id, effective_from)
            );
            CREATE TABLE IF NOT EXISTS station_check_config (
                station_point_id TEXT PRIMARY KEY,
                tolerance_kwh REAL NOT NULL,
                tolerance_ratio REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meter_checks (
                check_id TEXT PRIMARY KEY,
                station_point_id TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                status TEXT NOT NULL,
                station_total_kwh REAL,
                components_total_kwh REAL,
                difference_kwh REAL,
                tolerance_kwh REAL NOT NULL,
                tolerance_ratio REAL NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS meter_check_components (
                check_id TEXT NOT NULL REFERENCES meter_checks(check_id) ON DELETE CASCADE,
                point_id TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                kwh REAL,
                PRIMARY KEY (check_id, point_id)
            );
            CREATE TABLE IF NOT EXISTS meter_check_evidence (
                check_id TEXT NOT NULL REFERENCES meter_checks(check_id) ON DELETE CASCADE,
                point_id TEXT NOT NULL,
                reading_id INTEGER NOT NULL REFERENCES meter_readings(id),
                PRIMARY KEY (check_id, point_id, reading_id)
            );
            CREATE TABLE IF NOT EXISTS meter_check_blockers (
                check_id TEXT NOT NULL REFERENCES meter_checks(check_id) ON DELETE CASCADE,
                point_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                boundary_start TEXT NOT NULL,
                boundary_end TEXT NOT NULL,
                detail TEXT NOT NULL,
                reading_ids_json TEXT NOT NULL,
                PRIMARY KEY (check_id, point_id, reason, boundary_start, boundary_end)
            );
        """)
        self.conn.commit()

    # ---- 原始事实：读数 ----

    def upsert_reading(self, reading: MeterReading) -> bool:
        """幂等写入：同来源同身份同值的重复采集不产生变化；同来源修正值会更新。"""
        before = self.conn.total_changes
        self.conn.execute("""
            INSERT INTO meter_readings(source, record_key, point_id, point_level, reading_type,
                                       meter_epoch, measured_at, period_start, period_end, value_kwh, raw_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, record_key) DO UPDATE SET
                point_level = excluded.point_level,
                value_kwh = excluded.value_kwh,
                raw_hash = excluded.raw_hash
            WHERE meter_readings.raw_hash <> excluded.raw_hash
        """, (
            reading.source,
            reading.record_key,
            reading.point_id,
            reading.point_level,
            reading.reading_type,
            reading.meter_epoch,
            reading.measured_at,
            reading.period_start,
            reading.period_end,
            reading.value_kwh,
            reading.raw_hash,
        ))
        self.conn.commit()
        return self.conn.total_changes > before

    def reject_reading(self, raw: object, error: str) -> None:
        record = raw if isinstance(raw, dict) else {"value": raw}
        self.conn.execute(
            "INSERT INTO rejected_meter_readings(source, error, raw_json) VALUES (?, ?, ?)",
            (record.get("source"), error, json.dumps(record, ensure_ascii=False, sort_keys=True)),
        )
        self.conn.commit()

    def readings_for_point(self, point_id: str, start: str, end: str) -> list[MeterReading]:
        """取测点计量周期落在 [start, end] 内的全部读数（含各来源版本）。"""
        rows = self.conn.execute("""
            SELECT id, source, point_id, point_level, reading_type, meter_epoch,
                   measured_at, period_start, period_end, value_kwh, record_key, raw_hash
            FROM meter_readings
            WHERE point_id = ? AND period_start >= ? AND period_end <= ?
            ORDER BY measured_at, id
        """, (point_id, start, end)).fetchall()
        return [
            MeterReading(
                id=row[0], source=row[1], point_id=row[2], point_level=row[3],
                reading_type=row[4], meter_epoch=row[5], measured_at=row[6],
                period_start=row[7], period_end=row[8], value_kwh=row[9],
                record_key=row[10], raw_hash=row[11],
            )
            for row in rows
        ]

    # ---- 原始事实：配置 ----

    def replace_config(self, attributions: list[Attribution], station_configs: list[StationConfig]) -> None:
        """整体替换归属与容差配置，并把既有核查全部标记为待复核。"""
        with self.conn:
            self.conn.execute("DELETE FROM point_attribution")
            self.conn.execute("DELETE FROM station_check_config")
            self.conn.executemany(
                "INSERT INTO point_attribution(gun_point_id, station_point_id, effective_from, effective_to) VALUES (?, ?, ?, ?)",
                [(a.gun_point_id, a.station_point_id, a.effective_from, a.effective_to) for a in attributions],
            )
            self.conn.executemany(
                "INSERT INTO station_check_config(station_point_id, tolerance_kwh, tolerance_ratio) VALUES (?, ?, ?)",
                [(c.station_point_id, c.tolerance_kwh, c.tolerance_ratio) for c in station_configs],
            )
            self.conn.execute("UPDATE meter_checks SET stale = 1")

    def attributions(self) -> list[Attribution]:
        rows = self.conn.execute(
            "SELECT gun_point_id, station_point_id, effective_from, effective_to FROM point_attribution ORDER BY gun_point_id, effective_from"
        ).fetchall()
        return [Attribution(row[0], row[1], row[2], row[3]) for row in rows]

    def station_config(self, station_point_id: str) -> StationConfig:
        row = self.conn.execute(
            "SELECT tolerance_kwh, tolerance_ratio FROM station_check_config WHERE station_point_id = ?",
            (station_point_id,),
        ).fetchone()
        if row is None:
            return StationConfig(station_point_id)
        return StationConfig(station_point_id, row[0], row[1])

    # ---- 推导结果：核查 ----

    def save_check(self, result: CheckResult) -> None:
        """替换式持久化核查结果：同一 (站, 时段) 的推导结果重算后覆盖，不累积。"""
        with self.conn:
            self.conn.execute("DELETE FROM meter_checks WHERE check_id = ?", (result.check_id,))
            self.conn.execute("""
                INSERT INTO meter_checks(check_id, station_point_id, period_start, period_end, status,
                                         station_total_kwh, components_total_kwh, difference_kwh,
                                         tolerance_kwh, tolerance_ratio, stale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                result.check_id, result.station_point_id, result.period_start, result.period_end,
                result.status, result.station_total_kwh, result.components_total_kwh,
                result.difference_kwh, result.tolerance_kwh, result.tolerance_ratio,
            ))
            for component in result.components:
                self.conn.execute(
                    "INSERT INTO meter_check_components(check_id, point_id, role, status, kwh) VALUES (?, ?, ?, ?, ?)",
                    (result.check_id, component.point_id, component.role, component.status, component.kwh),
                )
                for reading_id in component.evidence_ids:
                    self.conn.execute(
                        "INSERT INTO meter_check_evidence(check_id, point_id, reading_id) VALUES (?, ?, ?)",
                        (result.check_id, component.point_id, reading_id),
                    )
            for blocker in result.blockers:
                self.conn.execute(
                    """INSERT INTO meter_check_blockers(check_id, point_id, reason, boundary_start, boundary_end, detail, reading_ids_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.check_id, blocker.point_id, blocker.reason,
                        blocker.boundary_start, blocker.boundary_end, blocker.detail,
                        json.dumps(sorted(blocker.reading_ids)),
                    ),
                )

    def mark_checks_stale_for_reading(self, reading: MeterReading) -> None:
        """新读数落地后，把覆盖其计量周期的相关站核查标记为待复核。"""
        stations = {reading.point_id}
        rows = self.conn.execute(
            "SELECT DISTINCT station_point_id FROM point_attribution WHERE gun_point_id = ?",
            (reading.point_id,),
        ).fetchall()
        stations.update(row[0] for row in rows)
        placeholders = ", ".join("?" for _ in stations)
        self.conn.execute(
            f"""UPDATE meter_checks SET stale = 1
                WHERE station_point_id IN ({placeholders})
                  AND period_start <= ? AND period_end >= ?""",
            (*sorted(stations), reading.period_end, reading.period_start),
        )
        self.conn.commit()

    def pending_rechecks(self) -> list[dict]:
        """需要重新核查的时段（新补边界点或配置变更后）。"""
        rows = self.conn.execute(
            "SELECT station_point_id, period_start, period_end FROM meter_checks WHERE stale = 1 ORDER BY station_point_id, period_start"
        ).fetchall()
        return [{"station_point_id": row[0], "period_start": row[1], "period_end": row[2]} for row in rows]

    def get_check(self, station_point_id: str, start: str, end: str) -> dict | None:
        """查询一次核查的完整结果：结论、分量、阻止项和参与读数（沿差额追到读数）。"""
        check_id = f"{station_point_id}|{start}|{end}"
        row = self.conn.execute("""
            SELECT status, station_total_kwh, components_total_kwh, difference_kwh,
                   tolerance_kwh, tolerance_ratio, stale, computed_at
            FROM meter_checks WHERE check_id = ?
        """, (check_id,)).fetchone()
        if row is None:
            return None
        components = []
        for comp in self.conn.execute(
            "SELECT point_id, role, status, kwh FROM meter_check_components WHERE check_id = ? ORDER BY rowid",
            (check_id,),
        ).fetchall():
            evidence_ids = [
                item[0]
                for item in self.conn.execute(
                    "SELECT reading_id FROM meter_check_evidence WHERE check_id = ? AND point_id = ? ORDER BY reading_id",
                    (check_id, comp[0]),
                ).fetchall()
            ]
            components.append({
                "point_id": comp[0], "role": comp[1], "status": comp[2],
                "kwh": comp[3], "evidence_reading_ids": evidence_ids,
            })
        blockers = [
            {
                "point_id": item[0], "reason": item[1],
                "boundary_start": item[2], "boundary_end": item[3],
                "detail": item[4], "reading_ids": json.loads(item[5]),
            }
            for item in self.conn.execute(
                "SELECT point_id, reason, boundary_start, boundary_end, detail, reading_ids_json FROM meter_check_blockers WHERE check_id = ? ORDER BY rowid",
                (check_id,),
            ).fetchall()
        ]
        evidence = [
            {
                "id": item[0], "source": item[1], "point_id": item[2], "point_level": item[3],
                "reading_type": item[4], "meter_epoch": item[5], "measured_at": item[6],
                "period_start": item[7], "period_end": item[8], "value_kwh": item[9],
            }
            for item in self.conn.execute("""
                SELECT m.id, m.source, m.point_id, m.point_level, m.reading_type, m.meter_epoch,
                       m.measured_at, m.period_start, m.period_end, m.value_kwh
                FROM meter_check_evidence e JOIN meter_readings m ON m.id = e.reading_id
                WHERE e.check_id = ? ORDER BY m.id
            """, (check_id,)).fetchall()
        ]
        station_total = row[1]
        allowed = None
        if station_total is not None:
            allowed = max(row[4], row[5] * abs(station_total))
        return {
            "check_id": check_id,
            "station_point_id": station_point_id,
            "period_start": start,
            "period_end": end,
            "status": row[0],
            "station_total_kwh": station_total,
            "components_total_kwh": row[2],
            "difference_kwh": row[3],
            "tolerance_kwh": row[4],
            "tolerance_ratio": row[5],
            "allowed_kwh": allowed,
            "stale": bool(row[6]),
            "computed_at": row[7],
            "components": components,
            "blockers": blockers,
            "evidence": evidence,
        }

    def counts(self) -> dict[str, int]:
        return {
            "readings": self.conn.execute("SELECT COUNT(*) FROM meter_readings").fetchone()[0],
            "rejected": self.conn.execute("SELECT COUNT(*) FROM rejected_meter_readings").fetchone()[0],
            "checks": self.conn.execute("SELECT COUNT(*) FROM meter_checks").fetchone()[0],
            "stale_checks": self.conn.execute("SELECT COUNT(*) FROM meter_checks WHERE stale = 1").fetchone()[0],
        }

    def close(self) -> None:
        self.conn.close()
