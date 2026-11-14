"""Read-only observational companion to taste replay, never counterfactual outcomes."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models import PlayEvent
from app.recommendations import Decision, summarize
from app.matching.quality import recording_quality


def observed_quality(connection, *, user_id: int, until: datetime, days: int = 30,
                     available_at: bool = True) -> dict:
    since = until - timedelta(days=days)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table):
        # Table names are fixed by this module, never supplied by CLI input.
        return connection.execute(f'SELECT * FROM "{table}" WHERE user_id = ?', (user_id,)) if table in tables else []

    def stamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed is not None and parsed.tzinfo else parsed

    events = []
    unavailable = 0
    for source in rows("play_events"):
        row = dict(source)
        at = stamp(row["ts"])
        if not since - timedelta(days=days, hours=6) <= at <= until:
            continue
        received = stamp(row.get("received_at"))
        if available_at and (received is None or received > until):
            unavailable += 1
            continue
        # Quality consumes journal fields, not mutable current favorites or audio state.
        fields = {k: row[k] for k in ("id", "user_id", "event_id", "kind", "ref", "nd_song_id", "client",
            "context", "position_s", "duration_s", "playback_id", "device_id", "listened_ms", "target_type",
            "superseded_scrobble", "reverts_event_id") if k in row}
        target = json.loads(row["target_data"]) if isinstance(row.get("target_data"), str) else row.get("target_data")
        events.append(PlayEvent(**fields, ts=at, target_data=target or {}))
    decisions = []
    for session_table, item_table, prefix, time_field in (
        ("wave_sessions", "wave_items", "wave", "first_handed_at"),
        ("smart_sessions", "smart_items", "smart", "handed_at"),
    ):
        if item_table not in tables:
            continue
        owned = {r["id"]: dict(r) for r in rows(session_table)}
        for source in rows(item_table):
            row = dict(source)
            session = owned.get(row["session_id"])
            if session and row.get(time_field):
                decisions.append(Decision(user_id, f'{prefix}:{row["session_id"]}', row.get("ref"),
                    row.get("nd_song_id"), stamp(row[time_field]), row["generator"], bool(row["familiar"]),
                    session.get("taste_model")))
    if "mixes" in tables and "mix_items" in tables:
        owned = {r["id"]: dict(r) for r in rows("mixes")}
        for mid, mix in owned.items():
            stats = json.loads(mix["stats_json"]) if mix.get("stats_json") else {}
            for source in connection.execute("SELECT * FROM mix_items WHERE mix_id = ? AND state != 'reserve'", (mid,)):
                row = dict(source)
                decisions.append(Decision(user_id, f"mix:{mid}", row.get("ref"), row.get("nd_song_id"),
                    stamp(mix["created_at"]), row["generator"], bool(row["familiar"]), stats.get("taste_model")))
    result = summarize(user_id, events, decisions, days=days, now=until)
    previous = summarize(user_id, [e for e in events if e.ts < since], decisions, days=days, now=since)
    fields = ("since", "until", "attempts", "eligible_plays", "completed", "early_skips",
              "completion_rate", "early_skip_rate", "policy_cohorts")
    result["comparison"] = {"current": {k: result[k] for k in fields},
                            "previous": {k: previous[k] for k in fields}}
    bindings = []
    for source in rows("recording_bindings"):
        row = dict(source)
        row["created_at"], row["served_at"] = stamp(row["created_at"]), stamp(row.get("served_at"))
        row["verified_at"] = stamp(row.get("verified_at"))
        row["full_verified"] = bool(row["full_verified"]) if row.get("full_verified") is not None else None
        bindings.append(SimpleNamespace(**row))
    result["recordings"] = recording_quality(bindings, user_id=user_id, since=since, until=until)
    result["unavailable_events"] = unavailable
    result["limitations"].extend([
        "Observed outcomes for the recorded policy only; no outcomes are imputed for the replay/shadow alternative.",
        "Adjacent date windows are descriptive, not randomized or causal; unknown policy versions remain separate.",
        "Missing recording tables/observations mean no measured availability, never zero full recordings.",
    ])
    for section in (result, result["comparison"]["current"], result["comparison"]["previous"], result["recordings"]):
        for field in ("since", "until"):
            section[field] = section[field].isoformat()
    return result
