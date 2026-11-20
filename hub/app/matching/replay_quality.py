"""Read-only observational companion to taste replay, never counterfactual outcomes."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app import discovery_adoption
from app.matching.quality import recording_quality
from app.mixes import recent_offers
from app.mixes.taste_snapshot import Subject
from app.models import PlayEvent
from app.recommendations import LEARNING_DAYS, Decision, summarize, wave_snapshot_time, with_learning_window


def observed_quality(connection, *, user_id: int, until: datetime, days: int = 30,
                     available_at: bool = True) -> dict:
    since = until - timedelta(days=days)
    span = max(days * 2, max(LEARNING_DAYS, days) + discovery_adoption.WINDOW.days)
    earliest = until - timedelta(days=span, hours=6)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table):
        # Table names are fixed by this module, never supplied by CLI input.
        return connection.execute(f'SELECT * FROM "{table}" WHERE user_id = ?', (user_id,)) if table in tables else []

    def stamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed is not None and parsed.tzinfo else parsed

    def snapshot(value):
        return json.loads(value) if isinstance(value, str) else value

    history = []
    unavailable = 0
    for source in rows("play_events"):
        row = dict(source)
        at = stamp(row["ts"])
        if at > until:
            continue
        received = stamp(row.get("received_at"))
        if available_at and (received is None or received > until):
            unavailable += 1
            continue
        # Quality consumes journal fields, not mutable current favorites or audio state.
        fields = {k: row[k] for k in ("id", "user_id", "event_id", "kind", "ref", "nd_song_id", "client",
            "context", "position_s", "duration_s", "playback_id", "play_trigger", "device_id", "listened_ms", "target_type",
            "superseded_scrobble", "reverts_event_id", "launch_origin", "launch_id") if k in row}
        fields.setdefault("target_type", "track")  # before typed favorites, every journal entry named a track
        history.append(PlayEvent(**fields, ts=at, received_at=received if available_at else at,
                                 target_data=snapshot(row.get("target_data")) or {}))
    events = [e for e in history if e.ts >= earliest]
    contexts = {e.context for e in events if e.context}
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
            context = f'{prefix}:{row["session_id"]}'
            if not session or context not in contexts:
                continue
            data = snapshot(row.get("decision_json"))
            at = row.get(time_field)
            if prefix == "wave":
                at = wave_snapshot_time(at, row.get("last_handed_at"), row.get("handed_count"), data)
            if at:
                decisions.append(Decision(user_id, context, row.get("ref"), row.get("nd_song_id"), stamp(at),
                    row.get("generator"), bool(row["familiar"]) if row.get("familiar") is not None else None,
                    session.get("taste_model"), data))
    mix_playlists = set()
    if "mixes" in tables and "mix_items" in tables:
        owned = {r["id"]: dict(r) for r in rows("mixes")}
        for mid, mix in owned.items():
            if mix.get("nd_playlist_id") and stamp(mix["created_at"]) <= until:
                mix_playlists.add(mix["nd_playlist_id"])
            if f"mix:{mid}" not in contexts:
                continue
            stats = snapshot(mix.get("stats_json")) or {}
            for source in connection.execute("SELECT * FROM mix_items WHERE mix_id = ? AND state != 'reserve'", (mid,)):
                row = dict(source)
                if row["state"] != "active" and not row.get("offered_at"):
                    continue
                decisions.append(Decision(user_id, f"mix:{mid}", row.get("ref"), row.get("nd_song_id"),
                    stamp(row.get("offered_at") or mix["created_at"]), row.get("generator"),
                    bool(row["familiar"]) if row.get("familiar") is not None else None,
                    stats.get("taste_model"), snapshot(row.get("features_json"))))
    result = summarize(user_id, events, decisions, days=days, now=until)
    with_learning_window(result, events, decisions, user_id=user_id, now=until)
    subjects = []
    if "favorite_subjects" in tables:
        for row in connection.execute("SELECT aliases, enriched_at FROM favorite_subjects WHERE target_type = 'track'"):
            if row["enriched_at"] and stamp(row["enriched_at"]) <= until:
                subjects.append(Subject("track", tuple(snapshot(row["aliases"]) or ())))
    balance = {}
    result["discovery_adoption"] = discovery_adoption.summarize(user_id, history, decisions, now=until, days=days,
        subjects=subjects, mix_playlist_ids=mix_playlists, balance=balance)
    result["discovery_balance"] = balance
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
    result["offer_repeats"] = recent_offers.snapshot_report(connection, tables, user_id, days=days, now=until)
    if result["offer_repeats"] is None:
        result["limitations"].append("No comparable offer denominator: this snapshot lacks dated playable-offer history.")
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
