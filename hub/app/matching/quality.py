"""Explicit denominators for observed recording availability; no reconstructed history."""
from __future__ import annotations

from datetime import datetime

METRIC_VERSION = "recording-quality-v1"


def recording_quality(bindings, *, user_id: int, since: datetime, until: datetime) -> dict:
    rows = [r for r in bindings if r.user_id == user_id and since <= r.created_at <= until]
    known = [r for r in rows if r.served_at is not None and r.served_at <= until
             and r.full_verified is not None and r.verified_at is not None and r.verified_at <= until]
    full = sum(r.full_verified is True for r in known)
    return {"metric_version": METRIC_VERSION, "since": since, "until": until,
        "observations": len(rows), "known": len(known), "full": full,
        "not_full": len(known) - full, "unknown": len(rows) - len(known),
        "rate": full / len(known) if known else None,
        "provenance": sorted({r.provenance for r in rows}),
        "limitations": [
            "Знаменатель доли: только серверные наблюдения с проверкой полного файла или подтверждённым превью.",
            "HTTP-ответ, метаданные источника и начало звука сами по себе не доказывают полноту записи; это неизвестные исходы.",
            "Наблюдения привязки (повторные запросы в течение 5 минут объединяются), не число уникальных песен или прослушиваний.",
            "Все ваши источники воспроизведения, не только рекомендации. Старые прослушивания без привязки не восстановлены.",
            "Проверка длины/декодирования не подтверждает музыкальную идентичность: неверную версию можно сообщить отдельно.",
        ]}
