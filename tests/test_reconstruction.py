from pathlib import Path
import json
import sqlite3

import pytest

from charging_fusion.application import reconstruct_database, run_fixture
from charging_fusion.normalize import normalize
from charging_fusion.reconstruct import reconstruct
from charging_fusion.sources import FixtureSource
from charging_fusion.storage import ObservationStore

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _set(records, db):
    path = Path(db).with_suffix(".json")
    path.write_text(json.dumps(records), encoding="utf-8")
    return run_fixture(str(path), str(db))


def _query(db, gap=900):
    return reconstruct_database(str(db), continuity_gap_seconds=gap)


def _timeline(result, source, charger="G-01", station="ST-01"):
    for timeline in result["timelines"]:
        if (timeline["source"], timeline["station_id"], timeline["charger_id"]) == (source, station, charger):
            return timeline
    raise AssertionError(f"timeline not found: {source} {station} {charger}")


def _segments(timeline):
    return timeline["segments"]


def _status(record, status, *, at, source="s", station="ST-01", charger="G-01", order_id=None, energy=None):
    payload = {"station_id": station, "charger_id": charger, "status": status}
    if order_id is not None:
        payload["order_id"] = order_id
    if energy is not None:
        payload["energy_kwh"] = energy
    return {"source": source, "observed_at": at, "payload": payload}


# ---------------------------------------------------------------------------
# evidence contract and identity
# ---------------------------------------------------------------------------

def test_status_sample_without_order_id_is_accepted_and_kept_separate(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([_status(None, "available", at="2026-11-01T08:20:00Z")], db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT status, order_id FROM observations").fetchall()
    assert rows == [("available", None)]


def test_same_instant_contradictory_statuses_both_survive(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "available", at="2026-11-01T09:00:00Z", energy=0),
    ], db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT status FROM observations WHERE observed_at = ? ORDER BY id",
            ("2026-11-01T09:00:00Z",),
        ).fetchall()
    assert [row[0] for row in rows] == ["charging", "available"]
    result = _query(db)
    conflicts = [
        item for item in _timeline(result, "s")["unconfirmed"]
        if item["reason"] == "ambiguous_instant"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["statuses"] == ["available", "charging"]


def test_conflicting_order_hints_at_one_instant_do_not_erase_status(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1B"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
    ], db)
    timeline = _timeline(_query(db), "s")
    assert len(timeline["segments"]) == 1
    segment = timeline["segments"][0]
    assert segment["sample_count"] == 2
    assert segment["order_hints"] == ["O-1", "O-1B"]
    conflict = next(
        item for item in timeline["unconfirmed"] if item["reason"] == "ambiguous_instant"
    )
    assert conflict["reasons"] == ["order_hint_conflict"]
    assert conflict["statuses"] == ["charging"]


def test_duplicate_identical_record_is_idempotent(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    record = _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1")
    first = _set([record, record], db)
    second = _set([record], db)
    assert first["observations"] == 1
    assert second["changed"] == 0


def test_normalizer_does_not_put_order_id_in_identity():
    with_order = normalize(_status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"))
    without_order = normalize(_status(None, "charging", at="2026-11-01T08:00:00Z"))
    assert with_order.key == without_order.key


def test_equivalent_utc_offsets_share_one_observation_instant(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    first = _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1")
    second = _status(None, "available", at="2026-11-01T09:00:00+01:00")
    result = _set([first, second], db)
    assert result["observations"] == 2
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT observed_at FROM observations ORDER BY id").fetchall()
    assert rows == [("2026-11-01T08:00:00Z",), ("2026-11-01T08:00:00Z",)]
    timeline = _timeline(_query(db), "s")
    assert timeline["instants"][0]["ambiguous"] is True
    assert timeline["instants"][0]["statuses"] == ["available", "charging"]


def test_naive_or_invalid_time_is_rejected_not_guessed(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    result = _set([
        _status(None, "charging", at="2026-11-01T08:00:00"),
    ], db)
    assert result["rejected"] == 1
    assert result["observations"] == 0


def test_legacy_order_keyed_database_is_upgraded_not_dropped(tmp_path):
    db = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE observations (
                source TEXT NOT NULL, record_key TEXT NOT NULL, observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, raw_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, record_key, observed_at)
            );
        """)
        conn.execute(
            "INSERT INTO observations(source, record_key, observed_at, payload_json, raw_hash, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("operator-a", "st-01|g-01|o-1", "2026-11-01T08:00:00Z",
             json.dumps({"station_id": "ST-01", "charger_id": "G-01", "status": "charging", "order_id": "O-1"}),
             "hash1", "2026-11-02T00:00:00Z"),
        )
    store = ObservationStore(db)
    try:
        observations = store.load_observations()
    finally:
        store.close()
    assert len(observations) == 1
    assert observations[0]["order_hint"] == "O-1"
    assert observations[0]["first_seen_at"] == "2026-11-02T00:00:00Z"


# ---------------------------------------------------------------------------
# the 08:00 -> 08:20 walkthrough
# ---------------------------------------------------------------------------

def test_charging_then_available_brackets_end_without_claiming_0820_stop(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1", energy=5.0),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
    ], db)
    segment = _segments(_timeline(_query(db), "s"))[0]
    end = segment["end_boundary"]
    assert end["earliest"] == "2026-11-01T08:00:00Z"
    assert end["latest"] == "2026-11-01T08:20:00Z"
    assert end["lower_inclusive"] is True and end["upper_inclusive"] is True
    assert end["basis"] == "next_state_available"
    assert "single_sample_no_complete_session" in segment["unconfirmed"]
    assert segment["energy_kwh_preserved"] == [5.0]
    assert segment["observation_ids"] == [1]


def test_start_is_bracketed_by_previous_inactive_state(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "available", at="2026-11-01T07:50:00Z"),
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
    ], db)
    segment = _segments(_timeline(_query(db), "s"))[0]
    start = segment["start_boundary"]
    assert start["earliest"] == "2026-11-01T07:50:00Z"
    assert start["latest"] == "2026-11-01T08:00:00Z"
    assert start["basis"] == "after_prior_inactive_state"
    assert start["lower_inclusive"] is False


def test_fault_bounds_end_but_does_not_explain_it(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "fault", at="2026-11-01T09:10:00Z"),
    ], db)
    segment = _segments(_timeline(_query(db), "s"))[0]
    assert segment["end_boundary"]["basis"] == "next_state_fault"
    assert "fault_does_not_explain_session_end" in segment["unconfirmed"]
    assert segment["end_boundary"]["latest"] == "2026-11-01T09:10:00Z"


# ---------------------------------------------------------------------------
# gaps, ordering and sources
# ---------------------------------------------------------------------------

def test_long_gap_splits_and_does_not_assume_continuous_charging(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "charging", at="2026-11-01T12:00:00Z", order_id="O-1"),
    ], db)
    timeline = _timeline(_query(db), "s")
    assert len(timeline["segments"]) == 2
    first, second = timeline["segments"]
    assert first["end_boundary"]["basis"] == "unobserved_gap"
    assert first["end_boundary"]["latest"] is None
    assert "long_unobserved_gap_not_assumed_charging" in first["unconfirmed"]
    assert second["links"]["previous_segment_id"] == first["segment_id"]
    assert second["links"]["same_order_hint"] is True
    assert second["start_boundary"]["basis"] == "after_unobserved_gap"
    assert any(link["kind"] == "unobserved_gap" for link in timeline["order_hint_links"])
    # Same hint did not merge the fragments.
    assert len(timeline["segments"]) == 2


def test_short_gap_records_unsampled_interval(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "charging", at="2026-11-01T09:10:00Z", order_id="O-2"),
    ], db)
    segment = _segments(_timeline(_query(db), "s"))[0]
    assert segment["sample_count"] == 2
    assert segment["links"]["unobserved_intervals"][0]["seconds"] == 600.0


def test_contradictory_instant_is_retained_and_cuts_fragment(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "charging", at="2026-11-01T08:10:00Z", order_id="O-1"),
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "available", at="2026-11-01T09:00:00Z", energy=0),
        _status(None, "charging", at="2026-11-01T09:30:00Z", order_id="O-3"),
    ], db)
    timeline = _timeline(_query(db), "s")
    first, second = timeline["segments"]
    assert first["observation_ids"] == [1, 2]
    assert first["end_boundary"]["basis"] == "status_conflict"
    assert "instant_is_ambiguous" in first["unconfirmed"]
    # The contradictory instant itself is not evidence of a fragment: its
    # charging sample is reported but not used to build one.
    assert 3 not in second["observation_ids"]
    assert second["start_boundary"]["basis"] == "after_status_conflict"
    assert second["observation_ids"] == [5]
    assert any(item["reason"] == "ambiguous_instant" for item in timeline["unconfirmed"])


def test_order_hint_switch_is_preserved_not_merged(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "charging", at="2026-11-01T08:10:00Z", order_id="O-2"),
    ], db)
    timeline = _timeline(_query(db), "s")
    assert len(timeline["segments"]) == 2
    link = next(link for link in timeline["order_hint_links"] if link["kind"] == "order_hint_switch")
    assert link["from_order_hints"] == ["O-1"]
    assert link["to_order_hints"] == ["O-2"]
    assert timeline["segments"][0]["end_boundary"]["basis"] == "order_hint_change"


def test_reused_order_hint_on_disconnected_fragments_is_linked_not_merged(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
        _status(None, "charging", at="2026-11-01T09:30:00Z", order_id="O-1"),
    ], db)
    timeline = _timeline(_query(db), "s")
    assert len(timeline["segments"]) == 2
    second = timeline["segments"][1]
    assert second["reused_order_hints"] == [timeline["segments"][0]["segment_id"]]
    assert "order_hint_reused_across_disconnected_fragments" in second["unconfirmed"]
    reuse = next(link for link in timeline["order_hint_links"] if link["kind"] == "order_hint_reused")
    assert reuse["order_hints"] == ["O-1"]


def test_same_charger_labels_in_two_sources_are_not_stitched(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", source="operator-a", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z", source="operator-a"),
        _status(None, "charging", at="2026-11-01T08:30:00Z", source="operator-b", order_id="B-1"),
    ], db)
    result = _query(db)
    timeline_a = _timeline(result, "operator-a")
    timeline_b = _timeline(result, "operator-b")
    assert len(timeline_a["segments"]) == 1
    assert len(timeline_b["segments"]) == 1
    note = result["cross_source_notes"][0]
    assert note["merged"] is False and note["link_basis"] is None
    assert set(note["sources"]) == {"operator-a", "operator-b"}


def test_single_sample_remains_useful_but_not_completed(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([_status(None, "charging", at="2026-11-01T12:00:00Z", order_id="O-9", energy=3.0)], db)
    timeline = _timeline(_query(db), "s")
    assert len(timeline["segments"]) == 1
    segment = timeline["segments"][0]
    assert segment["start_boundary"]["earliest"] is None
    assert segment["end_boundary"]["latest"] is None
    assert segment["start_boundary"]["basis"] == "unbounded_before_first_evidence"
    assert segment["end_boundary"]["basis"] == "no_later_evidence"
    assert "single_sample_no_complete_session" in segment["unconfirmed"]
    assert segment["energy_kwh_preserved"] == [3.0]
    assert "One charging sample only" in timeline["conclusion"]


def test_status_evidence_with_no_charging_invents_no_segment(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([_status(None, "available", at="2026-11-01T08:00:00Z")], db)
    timeline = _timeline(_query(db), "s")
    assert timeline["segments"] == []
    assert timeline["unconfirmed"][0]["reason"] == "no_charging_activity_observed"


def test_energy_is_preserved_only(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2", energy=12.0),
        _status(None, "charging", at="2026-11-01T09:10:00Z", order_id="O-2", energy=13.1),
    ], db)
    segment = _segments(_timeline(_query(db), "s"))[0]
    assert segment["energy_kwh_preserved"] == [12.0, 13.1]
    assert "no energy" in segment["energy_note"]


# ---------------------------------------------------------------------------
# late arrival and build impact
# ---------------------------------------------------------------------------

def test_late_charging_sample_revises_only_the_affected_segment(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
    ], db)
    before = _query(db)
    _set([_status(None, "charging", at="2026-11-01T08:10:00Z", order_id="O-1", energy=7.0)], db)
    after = _query(db)
    impact = after["build_impact"]
    assert impact["previous_build_id"] == before["build_impact"]["build_id"]
    assert [item["kind"] for item in impact["affected"]] == ["revised"]
    revised = impact["affected"][0]
    assert revised["segment_id"] == before["segments"][0]["segment_id"]
    assert revised["changes"]["added_observation_ids"] == [3]
    assert "single_sample_no_complete_session" in revised["changes"]["unconfirmed"]["removed"]
    assert _segments(_timeline(after, "s"))[0]["sample_count"] == 2


def test_late_available_sample_can_extend_the_end_bracket(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([_status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1")], db)
    _query(db)
    _set([_status(None, "available", at="2026-11-01T15:00:00Z")], db)
    after = _query(db)
    segment = _segments(_timeline(after, "s"))[0]
    assert segment["end_boundary"]["basis"] == "next_state_available"
    assert segment["end_boundary"]["latest"] == "2026-11-01T15:00:00Z"
    revised = after["build_impact"]["affected"][0]
    assert revised["kind"] == "revised"
    assert revised["changes"]["end_boundary"]["after"][0] == "2026-11-01T15:00:00Z"
    assert "end_not_bounded" in revised["changes"]["unconfirmed"]["removed"]


def test_late_conflicting_sample_removes_and_recomputes_segment(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "fault", at="2026-11-01T09:10:00Z"),
        _status(None, "charging", at="2026-11-01T09:40:00Z", order_id="O-3"),
    ], db)
    before = _query(db)
    assert len(before["segments"]) == 3
    _set([_status(None, "available", at="2026-11-01T09:00:00Z")], db)
    after = _query(db)
    impacted = {(item["kind"], item["segment_id"]) for item in after["build_impact"]["affected"]}
    assert ("removed", before["segments"][1]["segment_id"]) in impacted
    remaining = _segments(_timeline(after, "s"))
    assert len(remaining) == 2
    # Fragment 1 (08:00) keeps its unambiguous 08:20 close.  The 09:00 charging
    # fragment is removed by the conflict.  The 09:40 sample is bracketed by the
    # later unambiguous fault at 09:10, so the conflict does not silently win a
    # boundary: it stays reported below.
    assert remaining[0]["end_boundary"]["basis"] == "next_state_available"
    assert remaining[1]["start_boundary"]["basis"] == "after_prior_inactive_state"
    assert remaining[1]["start_boundary"]["earliest"] == "2026-11-01T09:10:00Z"
    assert remaining[1]["observation_ids"] == [5]
    conflicts = [
        item for item in _timeline(after, "s")["unconfirmed"]
        if item["reason"] == "ambiguous_instant"
    ]
    assert conflicts[0]["observation_ids"] == [3, 6]


def test_impact_is_identified_by_evidence_not_by_ordinal(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    _set([
        _status(None, "charging", at="2026-11-01T08:00:00Z", order_id="O-1"),
        _status(None, "available", at="2026-11-01T08:20:00Z"),
        _status(None, "charging", at="2026-11-01T09:00:00Z", order_id="O-2"),
        _status(None, "fault", at="2026-11-01T09:10:00Z"),
    ], db)
    before = _query(db)
    _set([_status(None, "available", at="2026-11-01T08:15:00Z")], db)
    after = _query(db)
    second_before = before["segments"][1]["segment_id"]
    assert after["build_impact"]["unchanged_segment_count"] == 1
    revised = [
        item for item in after["build_impact"]["affected"] if item["kind"] == "revised"
    ]
    assert len(revised) == 1
    assert revised[0]["segment_id"] != second_before
    assert revised[0]["previous_segment_id"] == before["segments"][0]["segment_id"]


# ---------------------------------------------------------------------------
# fixture walkthrough
# ---------------------------------------------------------------------------

def test_walkthrough_fixture_is_point_by_point_explainable(tmp_path):
    db = str(tmp_path / "db.sqlite3")
    run_fixture(str(FIXTURES / "reconstruction_trajectory.json"), db)
    result = _query(db)
    timeline = _timeline(result, "operator-a")
    assert len(timeline["segments"]) == 3
    first, second, third = timeline["segments"]
    assert first["start_boundary"]["earliest"] == "2026-11-01T07:50:00Z"
    assert first["end_boundary"]["latest"] == "2026-11-01T08:20:00Z"
    assert second["start_boundary"]["earliest"] == "2026-11-01T08:20:00Z"
    assert second["end_boundary"]["basis"] == "next_state_fault"
    assert third["end_boundary"]["basis"] == "no_later_evidence"
    assert result["cross_source_notes"][0]["merged"] is False
    single = _timeline(result, "operator-a", charger="G-02")
    assert single["segments"][0]["sample_count"] == 1
    assert "single_sample_no_complete_session" in single["segments"][0]["unconfirmed"]
    assert len(result["timelines"]) == 3
