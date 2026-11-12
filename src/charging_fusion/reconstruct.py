"""Evidence reconstruction for charger activity fragments.

This module never certifies a charging session.  Per (source, station_id,
charger_id) it answers: what activity was actually observed, where start and
end may fall, and which parts the evidence cannot settle.

Deliberate rules:

* Observation identity is (source, station_id, charger_id, observed_at,
  raw_hash).  ``order_id`` is a source-supplied correlation *hint* only; a
  status sample without one is still evidence.
* Ordering is by observation time inside one source only.  Different sources
  are separate timelines unless an explicit link is supplied; equal
  station/charger labels in two operators never merge into one session.
* A long unobserved gap is not continuity.  Active samples farther apart than
  ``continuity_gap_seconds`` are separate fragments, linked as *possible*
  continuation rather than merged.
* ``fault`` proves "not charging at that instant" but does not explain the
  session's end.  It brackets the end; it does not define it.
* Contradictory evidence at one instant is retained.  It cuts the fragment and
  is reported as an ambiguity; sorting order never picks a winning status.
* ``energy_kwh`` is copied through unchanged.  No energy, cost or station/
  charger difference is ever computed.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

ACTIVE_STATUS = "charging"
DEFAULT_CONTINUITY_GAP_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# time and identity helpers
# ---------------------------------------------------------------------------

def parse_time(value: str) -> datetime:
    """Parse the canonical ISO 8601 timestamp enforced by ``normalize``."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timeline_key(source: str, station_id: str, charger_id: str) -> str:
    """Stable JSON encoding of the timeline identity (safe as a dict/SQL key)."""
    return json.dumps([source, station_id, charger_id], ensure_ascii=False, separators=(",", ":"))


def observation_ref(observation: dict[str, Any]) -> dict[str, Any]:
    """The durable reference a segment keeps to one evidence row."""
    return {
        "id": observation["id"],
        "observed_at": observation["observed_at"],
        "status": observation["status"],
        "order_hint": observation["order_hint"],
        "energy_kwh": observation["energy_kwh"],
        "raw_hash": observation["raw_hash"],
        "first_seen_at": observation["first_seen_at"],
    }


def segment_signature(detail: dict[str, Any]) -> str:
    """Content signature covering evidence, boundaries and unconfirmed reasons.

    ``ordinal`` is excluded so a fragment whose content is untouched is not
    reported as changed merely because earlier fragments were added or removed.
    """
    stable = {
        k: v for k, v in detail.items()
        if k not in ("segment_id", "signature", "ordinal")
    }
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def observation_ids_in(detail: dict[str, Any]) -> set[int]:
    """All evidence ids that make up a persisted segment detail."""
    ids = set(detail.get("observation_ids", []))
    for instant in detail.get("instants", []):
        ids.update(instant.get("observation_ids", []))
    return ids


def boundary_evidence_ids(detail: dict[str, Any]) -> set[int]:
    ids = set()
    for key in ("start_boundary", "end_boundary"):
        ids.update(detail.get(key, {}).get("evidence_observation_ids", []))
    return ids


# ---------------------------------------------------------------------------
# instants: observations sharing one observed_at
# ---------------------------------------------------------------------------

def _instants(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group observations sharing one canonical instant.

    A *status* conflict makes the instant unusable as evidence: no status may be
    chosen.  An *order-hint* conflict is different - the status can still be
    unambiguous while two references disagree - so the instant stays usable and
    both hints are retained.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        grouped[observation["observed_at"]].append(observation)
    instants = []
    for observed_at, rows in sorted(grouped.items(), key=lambda item: parse_time(item[0])):
        statuses = {row["status"] for row in rows}
        hints = {row["order_hint"] for row in rows if row["order_hint"]}
        status_conflict = len(statuses) > 1
        hint_conflict = len(hints) > 1
        instants.append({
            "time": observed_at,
            "dt": parse_time(observed_at),
            "observations": sorted(rows, key=lambda row: row["id"]),
            "observation_ids": sorted(row["id"] for row in rows),
            "statuses": statuses,
            "status": next(iter(statuses)) if not status_conflict else None,
            "hints": hints,
            # Only a status conflict blocks the instant from carrying evidence.
            "ambiguous": status_conflict,
            "status_conflict": status_conflict,
            "hint_conflict": hint_conflict,
            "ambiguity_reasons": (["status_conflict"] if status_conflict else [])
            + (["order_hint_conflict"] if hint_conflict else []),
        })
    return instants


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------

_START_BASIS_NOTES = {
    "unbounded_before_first_evidence": (
        "No earlier state evidence. The start is unknown; this sample only witnesses "
        "activity at this instant."
    ),
    "after_prior_inactive_state": (
        "The next earlier sample reported a non-charging state, so the start lies after "
        "that instant and no later than this sample. It was not directly sampled."
    ),
    "after_status_conflict": (
        "Conflicting observations exist at the earlier instant; the start lies after it, "
        "and no conflicting status is chosen."
    ),
    "after_order_hint_change": (
        "The previous charging sample carried a different order hint. Order hints are "
        "correlation-only, so this fragment starts separately; it may still be one physical "
        "session."
    ),
    "after_unobserved_gap": (
        "No evidence in the gap. Silence is not treated as continued charging, so this "
        "fragment starts separately; it may be a continuation but that is not asserted."
    ),
}

_END_BASIS_NOTES = {
    "no_later_evidence": (
        "No later evidence. Activity is witnessed up to this sample; silence is not treated "
        "as ongoing charging, and no stop time is claimed."
    ),
    "status_conflict": (
        "Contradictory samples at this instant. The fragment stops being evidenced there "
        "because continuity cannot be proven, not because a status won."
    ),
    "order_hint_change": (
        "Two charging samples with different order hints bracket a possible transition. "
        "The hint is correlation-only and does not prove a session boundary."
    ),
    "unobserved_gap": (
        "The gap has no evidence. The fragment is not extended across it; the same physical "
        "session may continue, but that is not asserted."
    ),
}


def _boundary_unsigned(
    *, earliest: str | None, latest: str | None, basis: str,
    lower_inclusive: bool, upper_inclusive: bool, ids: list[int],
) -> dict[str, Any]:
    return {
        "earliest": earliest,
        "latest": latest,
        "lower_inclusive": lower_inclusive,
        "upper_inclusive": upper_inclusive,
        "basis": basis,
        "evidence_observation_ids": sorted(ids),
    }


def _end_note(basis: str, status: str | None) -> str:
    if basis.startswith("next_state_"):
        if status == "fault":
            return (
                "The fault sample proves the charger was not charging then, so the end lies "
                "between the last charging sample and this fault sample. Fault is a state "
                "observation, not an explanation of how or why the session ended."
            )
        return (
            f"The next unambiguous sample reported {status}, so the end lies between the last "
            f"charging sample and that sample. The state change only brackets the end."
        )
    return _END_BASIS_NOTES[basis]


# ---------------------------------------------------------------------------
# segment assembly
# ---------------------------------------------------------------------------

class _Segment:
    """Mutable during assembly; serialised into an immutable detail."""

    def __init__(self, timeline: dict[str, Any], start_boundary: dict[str, Any],
                 unconfirmed: list[str]):
        self.timeline = timeline
        self.start_boundary = start_boundary
        self.end_boundary: dict[str, Any] | None = None
        self.end_basis: str | None = None
        self.instants: list[dict[str, Any]] = []
        self.order_hints: list[str] = []
        self.energy_values: list[Any] = []
        self.unconfirmed = list(unconfirmed)
        self.links: dict[str, Any] = {}
        self.reused_order_hints: list[str] = []
        self.ordinal: int | None = None
        self.segment_id: str | None = None

    def append(self, instant: dict[str, Any]) -> None:
        self.instants.append(instant)
        for observation in instant["observations"]:
            if observation["order_hint"] and observation["order_hint"] not in self.order_hints:
                self.order_hints.append(observation["order_hint"])
            self.energy_values.append(observation["energy_kwh"])

    @property
    def sample_count(self) -> int:
        return sum(len(instant["observations"]) for instant in self.instants)

    @property
    def observation_ids(self) -> list[int]:
        return sorted(i for instant in self.instants for i in instant["observation_ids"])

    def detail(self) -> dict[str, Any]:
        if self.end_boundary is None:
            raise ValueError("segment closed without an end boundary")
        detail: dict[str, Any] = {
            "source": self.timeline["source"],
            "station_id": self.timeline["station_id"],
            "charger_id": self.timeline["charger_id"],
            "timeline_key": self.timeline["timeline_key"],
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "kind": "charging_observed",
            "observations": [observation_ref(o) for i in self.instants for o in i["observations"]],
            "observation_ids": self.observation_ids,
            "instants": [
                {"observed_at": i["time"], "observation_ids": i["observation_ids"]}
                for i in self.instants
            ],
            "sample_count": self.sample_count,
            "order_hints": list(self.order_hints),
            "energy_kwh_preserved": list(self.energy_values),
            "energy_note": (
                "energy_kwh values are preserved exactly as observed; no energy, cost, or "
                "station/charger difference is computed."
            ),
            "start_boundary": self.start_boundary,
            "end_boundary": self.end_boundary,
            "end_bracket_certain": self.end_basis not in (
                "no_later_evidence", "status_conflict", "order_hint_change", "unobserved_gap"
            ),
            "unconfirmed": list(self.unconfirmed),
            "links": dict(self.links),
            "reused_order_hints": list(self.reused_order_hints),
            "summary": self._summary(),
        }
        return detail

    def _summary(self) -> str:
        start = self.start_boundary
        end = self.end_boundary
        assert end is not None
        if start["earliest"] is None:
            start_text = f"no earlier bound (observed {start['latest']})"
        else:
            start_text = f"between {start['earliest']} and {start['latest']}"
        if end["latest"] is None:
            end_text = "unbounded after the last sample"
        else:
            end_text = f"between {end['earliest']} and {end['latest']}"
        return (
            f"Observed charging across {self.sample_count} sample(s). Start {start_text}; "
            f"end {end_text}; this is evidence, not a certified session."
        )

    def close(self, *, basis: str, status: str | None, latest: str | None,
              evidence_ids: list[int], certain: bool,
              extra_reasons: tuple[str, ...] = ()) -> None:
        last_time = self.instants[-1]["time"]
        self.end_basis = basis
        if basis == "no_later_evidence":
            self.end_boundary = _boundary_unsigned(
                earliest=last_time, latest=None, basis=basis,
                lower_inclusive=True, upper_inclusive=False, ids=self.instants[-1]["observation_ids"],
            )
            self.unconfirmed.append("end_not_bounded")
        elif basis in ("status_conflict", "unobserved_gap", "order_hint_change"):
            # The fragment stops being evidenced here, but the activity's end is
            # not bounded: a continuation, a later sample of the same session, or
            # a genuine stop are all possible.
            self.end_boundary = _boundary_unsigned(
                earliest=last_time, latest=None, basis=basis,
                lower_inclusive=True, upper_inclusive=False, ids=evidence_ids,
            )
            self.unconfirmed.append({
                "status_conflict": "end_at_status_conflict",
                "unobserved_gap": "long_unobserved_gap_not_assumed_charging",
                "order_hint_change": "order_hint_change_may_not_be_session_boundary",
            }[basis])
        else:  # a concrete non-active state closed the fragment
            self.end_boundary = _boundary_unsigned(
                earliest=last_time, latest=latest, basis=basis,
                lower_inclusive=True, upper_inclusive=True, ids=evidence_ids,
            )
            self.unconfirmed.append("end_bracketed_not_exact")
            if status == "fault":
                self.unconfirmed.append("fault_does_not_explain_session_end")
        self.end_boundary["note"] = _end_note(basis, status)
        self.end_boundary["certainty"] = "certain_bracket" if certain else "uncertain_bracket"
        for reason in extra_reasons:
            if reason not in self.unconfirmed:
                self.unconfirmed.append(reason)
        if self.sample_count == 1 and "single_sample_no_complete_session" not in self.unconfirmed:
            self.unconfirmed.append("single_sample_no_complete_session")


def _start_boundary(
    instant: dict[str, Any], context: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    if context is None:
        boundary = _boundary_unsigned(
            earliest=None, latest=instant["time"],
            basis="unbounded_before_first_evidence",
            lower_inclusive=False, upper_inclusive=True,
            ids=instant["observation_ids"],
        )
        reasons = ["start_not_bounded"]
    else:
        boundary = _boundary_unsigned(
            earliest=context["time"], latest=instant["time"], basis=context["basis"],
            lower_inclusive=False, upper_inclusive=True, ids=context["evidence_ids"],
        )
        reasons = ["start_bracketed_not_exact"]
        if context["basis"] in ("after_order_hint_change", "after_unobserved_gap"):
            reasons.append("start_may_be_a_continuation")
    boundary["note"] = _START_BASIS_NOTES[boundary["basis"]]
    boundary["certainty"] = (
        "unbounded" if boundary["earliest"] is None else "bracketed_not_exact"
    )
    return boundary, reasons


def _build_timeline(observations: list[dict[str, Any]], continuity_gap_seconds: int) -> dict[str, Any]:
    first = observations[0]
    timeline = {
        "source": first["source"],
        "station_id": first["station_id"],
        "charger_id": first["charger_id"],
        "timeline_key": timeline_key(first["source"], first["station_id"], first["charger_id"]),
        "continuity_gap_seconds": continuity_gap_seconds,
        "evidence_count": len(observations),
        "evidence": [observation_ref(o) for o in observations],
    }
    instants = _instants(observations)
    segments: list[_Segment] = []
    conflict_events: list[dict[str, Any]] = []
    order_hint_links: list[dict[str, Any]] = []
    current: _Segment | None = None
    # State of the instant immediately before the next charging sample.
    context: dict[str, Any] | None = None

    def close_current(**kwargs: Any) -> _Segment:
        nonlocal current
        assert current is not None
        current.close(**kwargs)
        segments.append(current)
        closed = current
        current = None
        return closed

    for instant in instants:
        if instant["hint_conflict"] and not instant["status_conflict"]:
            # The status is unambiguous, so the instant remains evidence; the
            # disagreeing references are still reported rather than dropped.
            conflict_events.append({
                "observed_at": instant["time"],
                "reasons": ["order_hint_conflict"],
                "statuses": sorted(instant["statuses"]),
                "order_hints": sorted(instant["hints"]),
                "observation_ids": instant["observation_ids"],
            })
        if instant["ambiguous"]:
            conflict_events.append({
                "observed_at": instant["time"],
                "reasons": instant["ambiguity_reasons"],
                "statuses": sorted(instant["statuses"]),
                "order_hints": sorted(instant["hints"]),
                "observation_ids": instant["observation_ids"],
            })
            if current is not None:
                closed = close_current(
                    basis="status_conflict", status=None, latest=instant["time"],
                    evidence_ids=instant["observation_ids"],
                    certain=False,
                    extra_reasons=("instant_is_ambiguous",),
                )
                # Preserve that the closing instant itself is contradictory.
                closed.links["closed_by_ambiguous_instant"] = instant["observation_ids"]
            context = {
                "basis": "after_status_conflict", "time": instant["time"],
                "evidence_ids": instant["observation_ids"],
                "charging_reported_but_ambiguous": ACTIVE_STATUS in instant["statuses"],
            }
            continue

        status = instant["status"]
        if status == ACTIVE_STATUS:
            if current is None:
                start_boundary, start_reasons = _start_boundary(instant, context)
                current = _Segment(timeline, start_boundary, start_reasons)
                if context is not None and context["basis"] in (
                    "after_order_hint_change", "after_unobserved_gap",
                ):
                    current.links["previous_segment_ordinal"] = len(segments)
                current.append(instant)
                context = None
                continue

            previous_instant = current.instants[-1]
            previous_dt = parse_time(previous_instant["time"])
            gap = (instant["dt"] - previous_dt).total_seconds()
            new_hints = instant["hints"]
            switched = bool(
                current.order_hints and new_hints
                and not (set(current.order_hints) & new_hints)
            )
            if switched:
                closed = close_current(
                    basis="order_hint_change", status=None, latest=None,
                    evidence_ids=instant["observation_ids"],
                    certain=False,
                    extra_reasons=("order_hint_switch_observed",),
                )
                hint_link = {
                    "kind": "order_hint_switch",
                    "from_order_hints": list(closed.order_hints),
                    "to_order_hints": sorted(new_hints),
                    "from_observation_id": previous_instant["observation_ids"][-1],
                    "to_observation_id": instant["observation_ids"][0],
                    "gap_seconds": gap,
                    "note": "A hint switch is preserved; it does not by itself prove a new session.",
                }
                order_hint_links.append(hint_link)
                start_boundary, start_reasons = _start_boundary(instant, {
                    "basis": "after_order_hint_change",
                    "time": previous_instant["time"],
                    "evidence_ids": previous_instant["observation_ids"],
                })
                current = _Segment(timeline, start_boundary, start_reasons)
                current.links["previous_segment_ordinal"] = len(segments)
                current.append(instant)
                continue
            if gap > continuity_gap_seconds:
                closed = close_current(
                    basis="unobserved_gap", status=None, latest=None,
                    evidence_ids=instant["observation_ids"],
                    certain=False,
                )
                order_hint_links.append({
                    "kind": "unobserved_gap",
                    "from_observation_id": previous_instant["observation_ids"][-1],
                    "to_observation_id": instant["observation_ids"][0],
                    "gap_seconds": gap,
                    "same_order_hint": bool(
                        closed.order_hints and new_hints
                        and set(closed.order_hints) & new_hints
                    ),
                    "note": "A gap does not prove either continuity or a stop.",
                })
                start_boundary, start_reasons = _start_boundary(instant, {
                    "basis": "after_unobserved_gap",
                    "time": previous_instant["time"],
                    "evidence_ids": previous_instant["observation_ids"],
                })
                current = _Segment(timeline, start_boundary, start_reasons)
                current.links["previous_segment_ordinal"] = len(segments)
                current.links["same_order_hint"] = bool(
                    closed.order_hints and new_hints
                    and set(closed.order_hints) & new_hints
                )
                current.append(instant)
                continue
            if gap > 0:
                current.links.setdefault("unobserved_intervals", []).append({
                    "from": previous_instant["time"],
                    "to": instant["time"],
                    "seconds": gap,
                    "note": "No sample inside this interval; continuity is not asserted there.",
                })
            current.append(instant)
            continue

        # Unambiguous, non-charging state evidence.
        if current is not None:
            close_current(
                basis=f"next_state_{status}", status=status, latest=instant["time"],
                evidence_ids=instant["observation_ids"],
                certain=True,
            )
        context = {
            "basis": "after_prior_inactive_state", "time": instant["time"],
            "status": status, "evidence_ids": instant["observation_ids"],
        }

    if current is not None:
        close_current(
            basis="no_later_evidence", status=None, latest=None,
            evidence_ids=[], certain=False,
        )

    # Assign stable identifiers before cross-segment links, because reuse links
    # and gap links refer to published segment ids.
    for ordinal, segment in enumerate(segments, start=1):
        segment.ordinal = ordinal
        anchor = segment.observation_ids[0]
        segment.segment_id = "seg-" + hashlib.sha256(
            f"{timeline['timeline_key']}|{anchor}".encode("utf-8")
        ).hexdigest()[:12]

    # A hint reused on disconnected fragments is a preserved link, never a merge.
    for index, segment in enumerate(segments):
        for earlier in segments[:index]:
            shared = sorted(set(segment.order_hints) & set(earlier.order_hints))
            if not shared:
                continue
            if earlier.segment_id not in segment.reused_order_hints:
                segment.reused_order_hints.append(earlier.segment_id)
            reason = "order_hint_reused_across_disconnected_fragments"
            if reason not in segment.unconfirmed:
                segment.unconfirmed.append(reason)
            order_hint_links.append({
                "kind": "order_hint_reused",
                "order_hints": shared,
                "earlier_segment_id": earlier.segment_id,
                "later_segment_id": segment.segment_id,
                "earlier_observation_ids": earlier.observation_ids,
                "later_observation_ids": segment.observation_ids,
                "note": "Same hint on disconnected fragments; the fragments are not merged.",
            })

    details = []
    for segment in segments:
        detail = segment.detail()
        if "previous_segment_ordinal" in detail["links"]:
            previous = segments[detail["links"]["previous_segment_ordinal"] - 1]
            detail["links"]["previous_segment_id"] = previous.segment_id
        # Signature is computed last so resolved cross-segment links are covered.
        detail["signature"] = segment_signature(detail)
        details.append(detail)

    unconfirmed = []
    charging_in_status_conflict = [
        event for event in conflict_events
        if ACTIVE_STATUS in event["statuses"]
        and "status_conflict" in event["reasons"]
    ]
    if not details and charging_in_status_conflict:
        unconfirmed.append({
            "reason": "charging_reported_only_at_ambiguous_instants",
            "detail": (
                "At least one sample reported charging, but every such instant is "
                "contradicted by another status at the same instant. No fragment is "
                "built and no conflicting status is preferred."
            ),
            "observation_ids": sorted(
                i for event in charging_in_status_conflict for i in event["observation_ids"]
            ),
        })
    if not details and not charging_in_status_conflict:
        unconfirmed.append({
            "reason": "no_charging_activity_observed",
            "detail": "Status evidence exists but no sample reported charging; no fragment is invented.",
            "observation_ids": sorted(o["id"] for o in observations),
        })
    for event in conflict_events:
        unconfirmed.append({
            "reason": "ambiguous_instant",
            "observed_at": event["observed_at"],
            "reasons": event["reasons"],
            "statuses": event["statuses"],
            "order_hints": event["order_hints"],
            "observation_ids": event["observation_ids"],
            "note": (
                "All contradictory observations are retained; no ordering tie-break chose one."
                if "status_conflict" in event["reasons"]
                else "The status is unambiguous; only the order references disagree and both are kept."
            ),
        })

    timeline["instants"] = [
        {
            "observed_at": instant["time"],
            "observation_ids": instant["observation_ids"],
            "statuses": sorted(instant["statuses"]),
            "order_hints": sorted(instant["hints"]),
            "ambiguous": instant["ambiguous"],
            "ambiguity_reasons": instant["ambiguity_reasons"],
        }
        for instant in instants
    ]
    timeline["segments"] = details
    timeline["order_hint_links"] = order_hint_links
    timeline["unconfirmed"] = unconfirmed
    if not details and charging_in_status_conflict:
        timeline["conclusion"] = (
            "Charging was reported only at instants contradicted by another status at "
            "the same time. No fragment is built, and no conflicting status is chosen."
        )
    elif not details:
        timeline["conclusion"] = (
            "No charging activity is observed; status samples cannot establish a session "
            "or any start/end boundary."
        )
    elif len(details) == 1 and details[0]["sample_count"] == 1:
        timeline["conclusion"] = (
            "One charging sample only: real evidence of activity at that instant, but start "
            "and end remain unknown. It is not completed into a bounded session."
        )
    else:
        timeline["conclusion"] = (
            f"{len(details)} observed charging fragment(s). Every boundary is an evidence "
            f"bracket or an explicit unknown, not an exact session time."
        )
    return timeline


# ---------------------------------------------------------------------------
# whole-database reconstruction
# ---------------------------------------------------------------------------

def reconstruct(
    observations: list[dict[str, Any]],
    *,
    continuity_gap_seconds: int = DEFAULT_CONTINUITY_GAP_SECONDS,
) -> dict[str, Any]:
    """Group evidence by (source, station_id, charger_id) and build fragments."""
    if continuity_gap_seconds < 0:
        raise ValueError("continuity_gap_seconds must be non-negative")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        groups[timeline_key(observation["source"], observation["station_id"],
                            observation["charger_id"])].append(observation)
    timelines = [_build_timeline(groups[key], continuity_gap_seconds) for key in sorted(groups)]

    cross_source_notes = []
    by_charger: dict[tuple[str, str], set[str]] = defaultdict(set)
    for timeline in timelines:
        by_charger[(timeline["station_id"], timeline["charger_id"])].add(timeline["source"])
    for (station_id, charger_id), sources in sorted(by_charger.items()):
        if len(sources) > 1:
            cross_source_notes.append({
                "station_id": station_id,
                "charger_id": charger_id,
                "sources": sorted(sources),
                "link_basis": None,
                "merged": False,
                "note": (
                    "Same station/charger labels occur in several sources, but no explicit "
                    "identity link was supplied; each source is answered separately and no "
                    "continuous session is stitched across them."
                ),
            })

    unresolved = []
    for timeline in timelines:
        identity = {
            "source": timeline["source"],
            "station_id": timeline["station_id"],
            "charger_id": timeline["charger_id"],
            "timeline_key": timeline["timeline_key"],
        }
        for item in timeline["unconfirmed"]:
            unresolved.append({**identity, **item})
        for segment in timeline["segments"]:
            for reason in segment["unconfirmed"]:
                unresolved.append({
                    **identity,
                    "segment_id": segment["segment_id"],
                    "ordinal": segment["ordinal"],
                    "reason": reason,
                })

    segments = [segment for timeline in timelines for segment in timeline["segments"]]
    return {
        "continuity_gap_seconds": continuity_gap_seconds,
        "timeline_count": len(timelines),
        "segment_count": len(segments),
        "timelines": timelines,
        "segments": segments,
        "unresolved": unresolved,
        "cross_source_notes": cross_source_notes,
        "policy": {
            "ordering": "observation time only, within one source/station/charger timeline",
            "identity": "observation rows keyed by source/station/charger/time/raw_hash; order_id is a hint",
            "no_session_certification": True,
            "energy": "preserved only; no energy, cost, or station/charger difference computed",
            "source_merge": "never merged without an explicit link",
            "gaps": "not treated as continued charging",
            "fault": "bounds the end but does not explain it",
            "conflicts": "retained and reported; no tie-break winner chosen",
        },
    }
