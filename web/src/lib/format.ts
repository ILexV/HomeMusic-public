export function fmtDuration(s?: number | null): string {
  if (s == null || !Number.isFinite(s) || s <= 0) return '';
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const ss = String(sec).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** Total length like "1 ч 12 мин" / "42 мин". */
export function fmtLength(s: number): string {
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин`;
  return `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

/** The Hub's times are UTC without a zone ("2026-10-20T08:15:00"); JS would read them as local time. */
export function parseTime(iso: string): Date {
  return new Date(/T[\d:.]+$/.test(iso) ? iso + 'Z' : iso);
}

export function fmtDate(iso?: string | null): string {
  if (!iso) return '';
  const d = iso.length === 10 ? new Date(iso + 'T00:00:00') : parseTime(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

export function todayRu(): string {
  return new Date().toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

export function fmtWhen(iso?: string | null): string {
  if (!iso) return '';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  const time = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const today = new Date();
  const yest = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return `сегодня в ${time}`;
  if (d.toDateString() === yest.toDateString()) return `вчера в ${time}`;
  return `${d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })} в ${time}`;
}

export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

export const tracksWord = (n: number) => `${n} ${plural(n, ['трек', 'трека', 'треков'])}`;

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`;
  return `${(bytes / 1024 ** 3).toFixed(1).replace('.', ',')} ГБ`;
}

export function recordTypeLabel(t?: string | null): string {
  switch (t) {
    case 'album':
      return 'Альбом';
    case 'single':
      return 'Сингл';
    case 'ep':
      return 'EP';
    case 'compile':
    case 'compilation':
      return 'Сборник';
    default:
      return t ? t : '';
  }
}

export function yearOf(a: { year?: number | null; release_date?: string | null }): number | null {
  if (a.year) return a.year;
  if (a.release_date) return Number(a.release_date.slice(0, 4)) || null;
  return null;
}

export function providerLabel(p?: string | null): string {
  switch (p) {
    case 'hitmoz':
      return 'hitmoz';
    case 'hitmos_fm':
      return 'hitmos.fm';
    case 'ytdlp':
      return 'YouTube';
    case 'yt_reupload':
      return 'YouTube · перезалив';
    case 'soundcloud':
      return 'SoundCloud';
    case 'slskd':
      return 'Soulseek';
    case 'upload':
      return 'Загрузка';
    default:
      return p ?? '';
  }
}

const WARNING_TEXT: Record<string, string> = {
  possibly_censored: 'Возможно, запикано',
  censored: 'Возможно, запикано',
  silence: 'Возможно, запикано',
  beeped: 'Возможно, запикано',
  low_bitrate: 'Низкий битрейт',
  duration_mismatch: 'Длительность отличается от каталога',
  clean_version: 'Возможно, цензурная версия',
  not_explicit: 'Не помечен как Explicit',
};

/** Warnings are free-form on the backend: a code string, a Russian message, or {code, message}. */
export function warningText(w: unknown): string {
  if (typeof w === 'string') return WARNING_TEXT[w] ?? w;
  if (w && typeof w === 'object') {
    const o = w as Record<string, unknown>;
    for (const k of ['message', 'text', 'msg']) if (typeof o[k] === 'string') return o[k] as string;
    for (const k of ['code', 'kind', 'type']) {
      if (typeof o[k] === 'string') return WARNING_TEXT[o[k] as string] ?? (o[k] as string);
    }
  }
  return String(w);
}

/** progress is 0..1 (or 0..100 — both tolerated). */
export function percent(progress: number): number {
  if (!Number.isFinite(progress)) return 0;
  const p = progress <= 1 ? progress * 100 : progress;
  return Math.max(0, Math.min(100, Math.round(p)));
}

export const DOWNLOAD_ACTIVE = ['queued', 'searching', 'downloading', 'tagging', 'importing'];

export function downloadStatusLabel(status: string, progress = 0): string {
  switch (status) {
    case 'queued':
      return 'В очереди';
    case 'searching':
      return 'Поиск';
    case 'downloading':
      return `Скачивание ${percent(progress)}%`;
    case 'tagging':
      return 'Теги';
    case 'importing':
      return 'Импорт';
    case 'done':
      return 'Готово';
    case 'failed':
      return 'Ошибка';
    case 'needs_review':
      return 'Нужен выбор';
    case 'cancelled':
      return 'Отменено';
    default:
      return status;
  }
}

export function assistantStatusLabel(status: string): string {
  switch (status) {
    case 'queued':
      return 'В очереди';
    case 'extracting':
      return 'Распознаю';
    case 'resolving':
      return 'Ищу в каталоге';
    case 'awaiting_confirmation':
      return 'Ждёт подтверждения';
    case 'confirmed':
      return 'Скачивается';
    case 'done':
      return 'Готово';
    case 'failed':
      return 'Ошибка';
    default:
      return status;
  }
}

export function inputKindLabel(kind: string): string {
  switch (kind) {
    case 'images':
      return 'Скриншоты';
    case 'link':
      return 'Ссылка';
    case 'text':
      return 'Текст';
    case 'chat':
      return 'Чат';
    default:
      return kind;
  }
}

export function stripExt(name: string): string {
  return name.replace(/\.[a-z0-9]{2,5}$/i, '');
}

export function toIntOrNull(v: string): number | null {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}

export const URL_RE = /https?:\/\/[^\s<>"']+/i;

/** AI spending: cents matter, fractions of a cent are shown while the sum is small. */
export function fmtUsd(v: number): string {
  if (!v) return '$0';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

// --- admin insights (INSIGHTS.md 5): the codes of the API, labelled here ------------------------------------------

/** A whole number the Russian way: "1 204". */
export const fmtNum = (n?: number | null): string => (n == null ? '—' : Math.round(n).toLocaleString('ru-RU'));

/** A weight or a share with a comma: "4,12". */
export function fmtDec(n?: number | null, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return '—';
  return n.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A signed weight: "+1,86" / "−0,88". */
export function fmtSigned(n: number, digits = 2): string {
  const s = fmtDec(Math.abs(n), digits);
  return n > 0 ? `+${s}` : n < 0 ? `−${s}` : s;
}

/** `part / whole` in percent: "46 %"; no whole → "—". */
export function fmtPct(part: number, whole: number): string {
  if (!whole) return '—';
  return `${Math.round((part / whole) * 100)} %`;
}

/** Listening time: "48 ч", "3 ч 20 мин", "12 мин". */
export function fmtListened(ms?: number | null): string {
  if (!ms) return '0 мин';
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} мин`;
  const h = Math.floor(m / 60);
  return h >= 10 ? `${h} ч` : `${h} ч ${m % 60} мин`;
}

/** A local date and time "30.10 21:14" (the Hub's naive UTC). */
export function fmtStamp(iso?: string | null): string {
  if (!iso) return '—';
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })} ${d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
}

/** A local date "30.10" of an ISO date or time. */
export function fmtDay(iso?: string | null): string {
  if (!iso) return '—';
  const d = iso.length === 10 ? new Date(iso + 'T00:00:00') : parseTime(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
}

const codeLabel = (map: Record<string, string>, fallback?: string) => (code?: string | null): string =>
  code == null ? fallback ?? '—' : map[code] ?? code;

/** What a launch played (`source`). */
export const sourceLabel = codeLabel({
  mix: 'Микс дня', wave: 'Моя волна', smart: 'Умное перемешивание', playlist: 'Плейлист', album: 'Альбом', artist: 'Исполнитель', liked: 'Понравившиеся',
  search: 'Поиск', queue: 'Очередь', continuous: 'Продолжение похожими', library: 'Библиотека', other: 'Другое',
  unknown: 'Без контекста', scrobble: 'История Navidrome',
}, 'Без контекста');

/** Where a wave was started (`origin`); `null`/`not_recorded` — an old client. */
export const waveOriginLabel = codeLabel({
  home_card: 'Главная', catalog_card: 'Каталог', track_menu: 'Меню трека', artist_menu: 'Меню: по исполнителю',
  player: 'Плеер', artist_page: 'Страница исполнителя', radio: 'Радиостанции', search_file: 'Файл из поиска',
  restart: 'Перезапуск', not_recorded: 'не записано',
}, 'не записано');

/** Where a list was launched (`launch_origin`). */
export const launchOriginLabel = codeLabel({
  mix_page: 'Страница микса', mix_page_track: 'Трек на странице микса', mix_card: 'Карточка микса', nd_playlist: 'Плейлист микса',
  album: 'Альбом', artist: 'Исполнитель', playlist: 'Плейлист', liked: 'Понравившиеся', search: 'Поиск', queue: 'Очередь',
  library: 'Библиотека', wave: 'Волна',
}, 'не записано');

/** What a wave was started from (`seed_kind`). */
export const seedKindLabel = codeLabel({
  own: 'Своя волна', genre: 'По жанру', track: 'По треку', track_artist: 'По исполнителю трека', artist: 'По исполнителю',
  library_artist: 'Радиостанция', file: 'По файлу', unknown: 'неизвестно',
});

/** How a launch was found (`attribution`). */
export const attributionLabel = codeLabel({
  launch_id: 'точно (id запуска)', wave_session: 'точно (сессия волны)', reconstructed: 'восстановлено по событиям',
  inferred: 'приблизительно', context: 'по контексту микса', playlist_id: 'по плейлисту микса',
  inferred_playlist: 'приблизительно (плейлист)',
});

export function outcomeLabel(outcome?: string | null, earlyAt?: number | null): string {
  switch (outcome) {
    case 'not_played': return 'не сыгран';
    case 'started': return 'начат';
    case 'heard': return 'прослушан';
    case 'finished': return 'дослушан';
    case 'early_skip': return earlyAt != null ? `пропуск на ${Math.round(earlyAt)} с` : 'ранний пропуск';
    case 'liked': return '♥';
    case 'disliked': return '👎';
    default: return outcome ?? '—';
  }
}

/** A contribution to a song's weight (`contributions[].kind`), with the position of a skip. */
export function contributionLabel(kind: string, positionS?: number | null): string {
  switch (kind) {
    case 'finish': return 'дослушал';
    case 'skip': return positionS != null ? `пропуск на ${Math.round(positionS)} с` : 'пропуск';
    case 'start': return 'начал';
    case 'like': return '♥';
    case 'dislike': return '👎 «Не нравится»';
    case 'dislike_legacy': return '👎 (старый список)';
    case 'download': return 'скачал';
    case 'album_like': return '♥ альбома';
    default: return kind;
  }
}

/** Why the taste map is not there (`MapOut.reason`). */
export const mapReasonLabel = codeLabel({
  model_v1: 'Модель v1 не знает граней и звукового пространства — переключитесь на v2.',
  embedder_off: 'Сервис embedder не настроен: карты звука нет.',
  embedder_down: 'Сервис embedder не отвечает: карта недоступна.',
  embedder_timeout: 'Сервис embedder не ответил вовремя: карта недоступна.',
  embedder_stale_space: 'Звуковое пространство обновилось во время запроса: откройте карту ещё раз.',
}, 'Карта недоступна.');

/** The embedder's state in the taste summary. */
export function embedderLabel(state: unknown): string {
  if (state && typeof state === 'object') return 'работает';
  const known: Record<string, string> = { off: 'не настроен', down: 'не отвечает', timeout: 'не ответил вовремя', v1: 'не нужен (v1)' };
  return known[String(state)] ?? String(state ?? '—');
}

/** Why the mix build dropped candidates (`funnel.rejected`). */
export const rejectLabel = codeLabel({
  disliked: '👎', version: 'версии (live, ремиксы…)', duration: 'длительность', recent: 'давно/недавно звучали',
  negative_score: 'отрицательная оценка',
});

/** A mix song's clean-up verdict after the mix closed. */
export function cleanupLabel(v?: string | null): string {
  if (!v) return '';
  if (v === 'trashed') return 'удалён после микса';
  if (v === 'nothing') return 'не скачался';
  if (v.startsWith('kept: ')) return `оставлен: ${v.slice(6)}`;
  return v;
}

export const mixStateLabel = codeLabel({ active: 'в миксе', reserve: 'запас', failed: 'не скачался' });

export const WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
