import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .models import Observation

FIELD_MAP = {
    "station_id": "station_id",
    "charger_id": "charger_id",
    "status": "status",
    "energy_kwh": "energy_kwh",
    "order_id": "order_id",
}
KEY_FIELDS = ["station_id", "charger_id"]
REQUIRED_FIELDS = ["station_id", "charger_id", "status"]


def _text(value: Any) -> str:
    """Trim and collapse whitespace; preserve case because ids are not semantic."""
    return " ".join(str(value).split()) if isinstance(value, str) else value


def _order_hint(value: Any) -> str | None:
    """order_id is a source-provided correlation hint only, never an identity."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _canonical_time(value: Any) -> str:
    """Canonicalise an ISO 8601 instant so equal moments share one identity.

    ``2026-11-01T09:00:00+01:00`` and ``2026-11-01T08:00:00Z`` are the same
    observation instant and must not become two separate instants.  Naive
    timestamps are rejected rather than silently assumed to be UTC, because
    guessing a timezone would fabricate an ordering relation.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observed_at must be an ISO 8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("observed_at is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(raw: Any) -> Observation:
    """Convert one source record into an observation identity and validated payload.

    Identity is (source, station_id, charger_id, observed_at).  `order_id` is
    copied verbatim as a hint and is deliberately absent from the key: a status
    sample without an order reference must still be storable, and the same
    charger can report different order hints at different times without
    overwriting either observation.
    """
    if not isinstance(raw, dict):
        raise ValueError("record must be an object")
    source = _text(raw.get("source", ""))
    observed_at = _canonical_time(raw.get("observed_at"))
    payload = raw.get("payload")
    if not source or not observed_at or not isinstance(payload, dict):
        raise ValueError("source, observed_at and object payload are required")
    normalized: dict[str, Any] = {}
    for target, source_field in FIELD_MAP.items():
        value = _text(payload.get(source_field))
        normalized[target] = value
    missing = [field for field in REQUIRED_FIELDS if normalized.get(field) in (None, "")]
    if missing:
        raise ValueError("missing required fields: " + ",".join(missing))
    normalized["order_id"] = _order_hint(normalized.get("order_id"))
    key_values = [str(normalized.get(field)).strip().lower() for field in KEY_FIELDS]
    if any(not value for value in key_values):
        raise ValueError("identity is incomplete")
    key = "|".join(key_values)
    canonical = json.dumps(
        {"source": source, "observed_at": observed_at, "payload": normalized},
        sort_keys=True,
        ensure_ascii=False,
    )
    raw_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Observation(source, key, observed_at, normalized, raw_hash)
