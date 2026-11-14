// Dev-only mock of the Hub API (`npm run dev:mock`). Not part of the production build.
// In-memory state, fake download progress and assistant pipeline, SSE on /api/events.
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin } from 'vite';
import type { FavoriteSubject, FavoriteTarget } from '../src/api/types.ts';
import { insightsMock } from './insights.ts';

type Json = Record<string, unknown>;

const now = () => new Date().toISOString();
let searchHistory: string[] = [];
// `size` only shows in the address (the real Hub has 1000 px and 250 px covers): lists must ask for the small one
const cover = (seed: string, size = 1000) => `/mock-cover/${encodeURIComponent(seed)}.svg?size=${size}`;

function hue(seed: string) {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

function coverSvg(seed: string) {
  const h = hue(seed);
  const letter = (seed.replace(/^[^\p{L}]+/u, '')[0] ?? '♪').toUpperCase();
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${h},55%,45%)"/><stop offset="1" stop-color="hsl(${(h + 60) % 360},60%,18%)"/></linearGradient></defs><rect width="300" height="300" fill="url(#g)"/><text x="150" y="190" font-size="150" font-family="Arial" font-weight="700" fill="rgba(255,255,255,.85)" text-anchor="middle">${letter}</text></svg>`;
}

// --- catalog --------------------------------------------------------------------------------------

const ARTISTS = [
  { ref: 'dz:3', source: 'dz', id: '3', name: 'Три дня дождя', picture_url: cover('Три дня дождя'), picture_small_url: cover('Три дня дождя', 250), nb_albums: 9, nb_fans: 412345 },
  { ref: 'dz:1', source: 'dz', id: '1', name: 'Кино', picture_url: cover('Кино'), picture_small_url: cover('Кино', 250), nb_albums: 20, nb_fans: 1203456 },
  { ref: 'dz:2', source: 'dz', id: '2', name: 'Земфира', picture_url: cover('Земфира'), picture_small_url: cover('Земфира', 250), nb_albums: 12, nb_fans: 803456 },
];

type Rel = { title: string; type: 'album' | 'single' | 'ep' | 'compile'; year: number; owned: 'full' | 'partial' | 'none'; n: number; explicit?: boolean };
const RELEASES: Rel[] = [
  { title: 'Мы так и не стали взрослыми', type: 'album', year: 2026, owned: 'none', n: 12, explicit: true },
  { title: 'Мы так и не стали взрослыми (Deluxe)', type: 'album', year: 2025, owned: 'partial', n: 16, explicit: true },
  { title: 'Магнит', type: 'album', year: 2023, owned: 'full', n: 11, explicit: true },
  { title: 'Я в порядке', type: 'album', year: 2022, owned: 'none', n: 13 },
  { title: 'Дежавю', type: 'single', year: 2026, owned: 'none', n: 1, explicit: true },
  { title: 'Перезаряжай', type: 'single', year: 2025, owned: 'full', n: 1 },
  { title: 'Акустика', type: 'ep', year: 2024, owned: 'partial', n: 5 },
  { title: 'Лучшее', type: 'compile', year: 2021, owned: 'none', n: 20 },
];

const TITLES = ['Демоны', 'Где ты', 'Перезаряжай', 'Не выводи меня', 'Вина', 'Космос', 'Прощание', 'Унесу', 'Не кури', 'Отпусти', 'Тихо', 'Мотылёк', 'Холодно', 'Пустота', 'Звёзды', 'Монета', 'Дежавю', 'Ссоры', 'Апрель', 'Кассета'];

function albumOut(i: number, artist = ARTISTS[0]) {
  const r = RELEASES[i];
  return {
    ref: `dz:${100 + i}`,
    source: 'dz',
    id: String(100 + i),
    title: r.title,
    artist: artist.name,
    artist_ref: artist.ref,
    record_type: r.type,
    release_date: `${r.year}-0${(i % 8) + 1}-15`,
    year: r.year,
    cover_url: cover(r.title),
    cover_small_url: cover(r.title, 250),
    nb_tracks: r.n,
    explicit: r.explicit ?? false,
    owned_status: r.owned,
    owned_count: r.owned === 'full' ? r.n : r.owned === 'partial' ? Math.ceil(r.n / 2) : 0,
    genre: 'Rock',
  };
}

const downloadingRefs = new Set<string>();
/** catalog tracks being downloaded only to be starred (♥): not «downloading», the ♥ shows filled */
const starRefs = new Set<string>();

function trackOut(albumIdx: number, n: number) {
  const r = RELEASES[albumIdx];
  const a = albumOut(albumIdx);
  const ref = `dz:${1000 + albumIdx * 100 + n}`;
  const owned = r.owned === 'full' || (r.owned === 'partial' && n <= Math.ceil(r.n / 2));
  return {
    ref,
    source: 'dz',
    id: ref.slice(3),
    title: r.n === 1 ? r.title : TITLES[(n - 1 + albumIdx) % TITLES.length],
    artist: a.artist,
    artists: n === 3 ? [a.artist, 'Мона'] : [a.artist],
    artist_ref: a.artist_ref,
    album_ref: a.ref,
    album_title: a.title,
    duration_s: 150 + ((n * 37 + albumIdx * 13) % 120),
    track_no: n,
    disc_no: 1,
    explicit: !!r.explicit && n % 3 !== 0,
    cover_url: a.cover_url,
    cover_small_url: a.cover_small_url,
    owned,
    library_track_id: owned ? 5000 + n : null,
    nd_song_id: owned ? `nd${5000 + n}` : null,
    downloading: downloadingRefs.has(ref),
    star_pending: starRefs.has(ref),
  };
}

/** Search results: 70 tracks (the 25th listed twice, as Deezer does), served 25 per page. */
function searchTracks() {
  const all = RELEASES.flatMap((r, i) => Array.from({ length: r.n }, (_, k) => trackOut(i, k + 1))).slice(0, 70);
  all.splice(25, 0, all[24]);
  return all;
}
const SEARCH_PAGE = 25;

// --- library -----------------------------------------------------------------------------------

let libAlbums = [
  { id: 1, uid: 'a1', title: 'Магнит', album_artist: 'Три дня дождя', year: 2023, release_type: 'album', genre: 'Rock', deezer_album_id: '102', track_count: 3, has_cover: true },
  { id: 2, uid: 'a2', title: 'Группа крови', album_artist: 'Кино', year: 1988, release_type: 'album', genre: 'Rock', deezer_album_id: '7', track_count: 2, has_cover: true },
  { id: 3, uid: 'a3', title: 'Вендетта', album_artist: 'Земфира', year: 2005, release_type: 'album', genre: 'Rock', deezer_album_id: null, track_count: 2, has_cover: true },
  { id: 4, uid: 'a4', title: 'Записи с репетиции', album_artist: 'Своя группа', year: 2026, release_type: 'compilation', genre: null, deezer_album_id: null, track_count: 1, has_cover: false },
];

let libTracks = [
  { id: 11, album_id: 1, title: 'Демоны', artist: 'Три дня дождя', track_no: 1, explicit: true, warnings: [] as unknown[] },
  { id: 12, album_id: 1, title: 'Где ты', artist: 'Три дня дождя', track_no: 2, explicit: false, warnings: ['possibly_censored'] },
  { id: 13, album_id: 1, title: 'Вина', artist: 'Три дня дождя feat. Мона', track_no: 3, explicit: true, warnings: [] },
  { id: 21, album_id: 2, title: 'Группа крови', artist: 'Кино', track_no: 1, explicit: false, warnings: [] },
  { id: 22, album_id: 2, title: 'Закрой за мной дверь, я ухожу', artist: 'Кино', track_no: 2, explicit: false, warnings: [] },
  { id: 31, album_id: 3, title: 'Блюз', artist: 'Земфира', track_no: 1, explicit: false, warnings: [] },
  { id: 32, album_id: 3, title: 'Мы разбиваемся', artist: 'Земфира', track_no: 2, explicit: false, warnings: [] },
  { id: 41, album_id: 4, title: 'Песня у костра', artist: 'Своя группа', track_no: 1, explicit: false, warnings: [] },
];

function libTrackOut(t: (typeof libTracks)[number]) {
  const a = libAlbums.find((x) => x.id === t.album_id);
  return {
    id: t.id,
    nd_song_id: `nd${t.id}`,
    album_id: t.album_id,
    artist: t.artist,
    title: t.title,
    album: a?.title ?? null,
    album_artist: a?.album_artist ?? null,
    track_no: t.track_no,
    disc_no: 1,
    year: a?.year ?? null,
    duration_s: 180 + (t.id % 7) * 11,
    bitrate_kbps: 320,
    codec: 'mp3',
    explicit: t.explicit,
    source: t.album_id === 4 ? 'upload' : 'hitmoz',
    locked: t.explicit,
    warnings: t.warnings,
    path: `library/${a?.album_artist}/${a?.year} - ${a?.title}/1-0${t.track_no} - ${t.title}.mp3`,
  };
}

function libAlbumOut(a: (typeof libAlbums)[number], withTracks = false) {
  const tracks = libTracks.filter((t) => t.album_id === a.id);
  return { ...a, track_count: tracks.length, nd_album_id: `ndal${a.id}`, tracks: withTracks ? tracks.map(libTrackOut) : null };
}

// --- the Navidrome library (/api/nd: the PWA in a plain browser) ---------------------------------------

// Navidrome ids are plain tokens: one per artist name, numbered in the order of the artists list (stable between runs).
const ndArtistIds = new Map<string, string>();
const ndArtistId = (name: string) => {
  if (!ndArtistIds.size) for (const n of [...libAlbums.map((a) => a.album_artist), ...WAVE_ARTISTS]) if (!ndArtistIds.has(n)) ndArtistIds.set(n, `ndar${ndArtistIds.size + 1}`);
  let id = ndArtistIds.get(name);
  if (!id) ndArtistIds.set(name, (id = `ndar${ndArtistIds.size + 1}`));
  return id;
};

function ndSongOut(t: (typeof libTracks)[number]) {
  const a = libAlbums.find((x) => x.id === t.album_id);
  const lt = libTrackOut(t);
  return {
    ref: `nd:nd${t.id}`, source: 'nd', id: `nd${t.id}`, title: t.title, artist: t.artist, artists: [t.artist],
    album_title: a?.title ?? null, duration_s: lt.duration_s, track_no: t.track_no, explicit: t.explicit,
    cover_url: a?.has_cover ? cover(`lib${t.album_id}`) : null, cover_small_url: a?.has_cover ? cover(`lib${t.album_id}`, 300) : null,
    owned: true, library_track_id: t.id, nd_song_id: `nd${t.id}`, downloading: false,
    nd_album_id: `ndal${t.album_id}`, nd_artist_id: ndArtistId(a?.album_artist ?? t.artist),
  };
}

function ndAlbumOut(a: (typeof libAlbums)[number], withTracks = false) {
  const tracks = libTracks.filter((t) => t.album_id === a.id);
  return {
    id: `ndal${a.id}`, title: a.title, artist: a.album_artist, artist_id: ndArtistId(a.album_artist), year: a.year,
    song_count: tracks.length, duration_s: tracks.reduce((n, t) => n + libTrackOut(t).duration_s, 0),
    cover_url: a.has_cover ? cover(`lib${a.id}`, 300) : null, starred: a.id === 2,
    tracks: withTracks ? tracks.map(ndSongOut) : null,
  };
}

function ndArtists() {
  const names = [...new Set([...libAlbums.map((a) => a.album_artist), ...WAVE_ARTISTS])];
  return names.map((name) => ({
    id: ndArtistId(name), name, album_count: libAlbums.filter((a) => a.album_artist === name).length,
    cover_url: cover(`artist-${name}`, 300), starred: name === 'Кино' || name === 'Земфира',
  }));
}

const likedSongs = new Set(['nd11', 'nd21', 'nd31', 'nd13']);
const playlistSongs = new Map<string, string[]>([
  ['pl1', ['nd11', 'nd12', 'nd21', 'nd22', 'nd31', 'nd32', 'nd41']],
  ['pl2', ['nd13', 'nd21', 'nd11']],
]);
const ndSong = (id: string) => libTracks.find((t) => `nd${t.id}` === id);

// --- downloads ----------------------------------------------------------------------------------

type DL = Json & { id: number; batch_id: number; status: string; progress: number; artist: string; title: string; ref?: string };
let dlSeq = 300;
let batchSeq = 40;
const hitmozCand = (artist: string, title: string, i: number) => ({
  provider: i === 2 ? 'ytdlp' : i === 1 ? 'hitmos_fm' : 'hitmoz',
  provider_id: `${i}-${title}`,
  artist,
  title: i === 0 ? title : `${title}${i === 1 ? ' (Explicit)' : ' (Live)'}`,
  duration_s: 200 + i * 4,
  bitrate_kbps: i === 2 ? 160 : 320,
  explicit: i !== 2,
  upload_date: i === 0 ? '2024-05-11' : i === 1 ? '2026-04-02' : null,
  page_url: i === 2 ? 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' : 'https://ru.hitmoz.org/song/123',
  cover_url: cover(title),
  album: i === 0 ? 'Магнит' : null,
  year: i === 0 ? 2023 : null,
  score: 0.95 - i * 0.12,
  extra: i === 1 ? { full_title: `${title} (Soundtrack Version (Explicit))` } : {},
});
// re-uploads by small channels: titled "(без цензуры)", an old SoundCloud copy, a copy titled "(без мата)"
const reuploadCand = (artist: string, title: string, i: number) => ({
  provider: i === 1 ? 'soundcloud' : 'yt_reupload',
  provider_id: `r${i}-${title}`,
  artist,
  title,
  duration_s: 154 + i,
  bitrate_kbps: i === 1 ? 160 : 129,
  explicit: i === 0 ? true : i === 2 ? false : null,
  upload_date: i === 1 ? '2023-10-23' : '2026-06-27',
  page_url: i === 1 ? 'https://soundcloud.com/user/track' : 'https://www.youtube.com/watch?v=voSx7C0LyhQ',
  cover_url: cover(title),
  score: 0.98 - i * 0.04,
  extra: {
    reupload: true,
    channel: ['Олег Овощенский', 'пук хрюк', 'music_hub'][i],
    original_title: [`${title} – ${artist} (без цензуры)`, `${artist} - ${title}`, `${artist} - ${title} (БЕЗ МАТА)`][i],
    ...(i === 0 ? { uncensored: true } : i === 2 ? { uncensored: false } : {}),
  },
});

let downloads: DL[] = [
  { id: 201, batch_id: 31, status: 'downloading', progress: 0.42, artist: 'Кино', title: 'Звезда по имени Солнце', album: 'Звезда по имени Солнце', provider: 'hitmoz', warnings: [], chosen: hitmozCand('Кино', 'Звезда по имени Солнце', 0) },
  { id: 202, batch_id: 31, status: 'queued', progress: 0, artist: 'Кино', title: 'Пачка сигарет', album: 'Звезда по имени Солнце', provider: null, warnings: [] },
  { id: 203, batch_id: 30, status: 'needs_review', progress: 0, artist: 'Скриптонит', title: 'Положение', album: 'Уроборос', provider: null, error: 'Подходящий файл не найден ни в одном источнике', warnings: [], candidates: [0, 1, 2].map((i) => hitmozCand('Скриптонит', 'Положение', i)) },
  { id: 204, batch_id: 29, status: 'done', progress: 1, artist: 'Три дня дождя', title: 'Где ты', album: 'Магнит', provider: 'hitmoz', warnings: ['possibly_censored'], track_id: 12, chosen: hitmozCand('Три дня дождя', 'Где ты', 0) },
  { id: 205, batch_id: 28, status: 'failed', progress: 0, artist: 'Земфира', title: 'Хочешь?', album: 'Прости меня моя любовь', provider: 'ytdlp', error: 'Длительность файла не совпадает с каталогом (±3 с)', warnings: [] },
  { id: 206, batch_id: 27, status: 'cancelled', progress: 0, artist: 'Мона', title: 'Нету интереса', album: null, provider: null, warnings: [] },
].map((d) => ({ ...d, created_at: now(), updated_at: new Date(Date.now() - d.id * 1000).toISOString() }));

// --- uploads / playlists / assistant ---------------------------------------------------------------

let uploadSeq = 70;
let uploads: Json[] = [
  { id: 61, orig_name: 'Репетиция - Песня у костра (live).mp3', size: 7_340_032, status: 'uploaded', tags: { title: 'Песня у костра', artist: 'Своя группа', album: 'Записи с репетиции', track_no: 2, year: 2026, has_cover: true, duration_s: 204 }, created_at: now() },
];

let playlists = [
  { id: 'pl1', name: 'В дорогу', song_count: 42, duration_s: 9840 },
  { id: 'pl2', name: 'Со скриншота 12.10.2026', song_count: 8, duration_s: 1820 },
];

type Task = Json & { id: number; status: string };
let taskSeq = 9;
const tasks: Task[] = [
  { id: 8, status: 'done', input_kind: 'link', context: { playlist_name: 'Плейлист дня' }, rows: [], suggested_playlist_name: 'Плейлист дня', cost_usd: 0, created_at: new Date(Date.now() - 86400000).toISOString() },
];

interface ChatMsg {
  role: string;
  content: string;
  images?: number;
  cost?: number;
  done?: string[];
  proposal?: { task_id: number; token: string; summary: string; actions: unknown[]; state: string };
}
const chats: Array<{ id: number; title: string; messages: ChatMsg[]; created_at: string; updated_at: string }> = [
  {
    id: 1,
    title: 'Скачай последний альбом Земфиры',
    messages: [
      { role: 'user', content: 'Скачай последний альбом Земфиры' },
      { role: 'assistant', content: 'Поставил в загрузку «Бордерлайн» — 12 треков.', cost: 0.0004, done: ['В загрузках: Земфира — Бордерлайн, 12 треков'] },
    ],
    created_at: new Date(Date.now() - 26 * 3600e3).toISOString(),
    updated_at: new Date(Date.now() - 26 * 3600e3).toISOString(),
  },
];
let chatSeq = 1;

function assistantRows() {
  const t = (albumIdx: number, n: number, extra: Json = {}) => ({ ...trackOut(albumIdx, n), ...extra });
  return [
    { index: 0, artist_raw: 'Три дня дождя', title_raw: 'Демоны', truncated: false, status: 'owned', best: t(2, 1), options: [], score: 1, library_track_id: 11, duration: '3:12' },
    { index: 1, artist_raw: 'Три дня дождя', title_raw: 'Мотылёк', truncated: false, status: 'auto', best: t(0, 12), options: [], score: 0.97, duration: '2:58' },
    { index: 2, artist_raw: 'Кино', title_raw: 'Звезда по имени Сол…', truncated: true, status: 'auto', best: { ...t(3, 2), title: 'Звезда по имени Солнце', artist: 'Кино', artists: ['Кино'], album_title: 'Звезда по имени Солнце', cover_url: cover('Звезда') }, options: [], score: 0.93 },
    {
      index: 3,
      artist_raw: 'Земфира',
      title_raw: 'Хочешь',
      truncated: false,
      status: 'confirm',
      best: null,
      options: [
        { ...t(3, 4), title: 'Хочешь?', artist: 'Земфира', artists: ['Земфира'], album_title: 'Прости меня моя любовь', cover_url: cover('Прости') },
        { ...t(3, 5), title: 'Хочешь? (Live)', artist: 'Земфира', artists: ['Земфира'], album_title: 'Концерт в Лужниках', cover_url: cover('Лужники') },
        { ...t(3, 6), title: 'Хочешь? (Cover)', artist: 'Земфира Tribute', artists: ['Земфира Tribute'], album_title: 'Трибьют', cover_url: cover('Трибьют') },
      ],
      score: 0.8,
    },
    { index: 4, artist_raw: 'Неизвестный арт', title_raw: 'Песня без назв…', truncated: true, status: 'not_found', best: null, options: [], score: 0.3 },
  ];
}

// --- SSE ------------------------------------------------------------------------------------------

const clients = new Set<ServerResponse>();
function emit(event: string, data: unknown) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const c of clients) c.write(msg);
}

function dlOut(d: DL) {
  const { ref, candidates: _c, star, requested, ...rest } = d;
  return { ...rest, track_ref: ref ?? null, star: !!star && !requested, liked: !!star };
}

function setDl(d: DL, patch: Json) {
  Object.assign(d, patch, { updated_at: now() });
  // a download queued only for ♥ that failed is shown to the user from now on, as the Hub does
  if (d.star && (d.status === 'failed' || d.status === 'needs_review')) d.requested = true;
  emit('download', dlOut(d));
}

function simulate(d: DL) {
  const steps: Array<[number, Json]> = [
    [800, { status: 'searching' }],
    [1500, { status: 'downloading', progress: 0.1, provider: 'hitmoz', chosen: hitmozCand(d.artist, d.title, 0) }],
  ];
  for (let p = 0.25; p < 1; p += 0.15) steps.push([700, { progress: Math.min(0.99, p) }]);
  steps.push([600, { status: 'tagging', progress: 1 }], [700, { status: 'importing' }], [900, { status: Math.random() < 0.15 ? 'failed' : 'done' }]);
  let t = 0;
  for (const [delay, patch] of steps) {
    t += delay;
    setTimeout(() => {
      if (d.status === 'cancelled') return;
      // a done download has a Navidrome song; downloads queued with ♥ are starred
      const song = patch.status === 'done' ? { nd_song_id: `nd${d.id}` } : {};
      if (patch.status === 'done' && d.star) starred.add(`nd${d.id}`);
      if (d.star && d.ref && (patch.status === 'done' || patch.status === 'failed')) {
        const subject = [...favoriteSubjects.values()].find((s) => s.target_type === 'track' && s.ref === d.ref && s.liked);
        if (subject && patch.status === 'done') {
          subject.nd_id = `nd${d.id}`;
          subject.aliases = [...new Set([...subject.aliases, `nd:nd${d.id}`])];
        } else if (subject) mockFavorite({ target_type: 'track', ref: d.ref }, false, `rollback:${d.id}`, now());
      }
      setDl(d, patch.status === 'failed' ? { ...patch, error: 'Файл не прошёл проверку ffprobe' } : { ...patch, ...song });
      if ((patch.status === 'done' || patch.status === 'failed') && d.ref) {
        downloadingRefs.delete(d.ref);
        starRefs.delete(d.ref);
      }
      if (patch.status === 'done') emit('library', { kind: 'imported', album_id: 1, track_id: 11 });
    }, t);
  }
}

const ACTIVE = ['queued', 'searching', 'downloading', 'tagging', 'importing'];

function enqueue(items: Array<{ artist: string; title: string; album?: string | null; ref?: string }>, star = false) {
  const batch_id = ++batchSeq;
  let joined = 0;
  const created = items.flatMap((it) => {
    // the track is being downloaded already: no second download (♥ stars it, «Скачать» makes a ♥ one visible)
    const cur = it.ref ? downloads.find((x) => x.ref === it.ref && ACTIVE.includes(x.status)) : undefined;
    if (cur && it.ref) {
      joined++;
      if (star && !cur.star) Object.assign(cur, { star: true, requested: true });
      else if (!star && cur.star && !cur.requested) {
        cur.requested = true;
        starRefs.delete(it.ref);
        downloadingRefs.add(it.ref);
      }
      emit('download', dlOut(cur));
      return [];
    }
    const d: DL = { id: ++dlSeq, batch_id, status: 'queued', progress: 0, artist: it.artist, title: it.title, album: it.album ?? null, provider: null, warnings: [], ref: it.ref, star, created_at: now(), updated_at: now() };
    if (it.ref) (star ? starRefs : downloadingRefs).add(it.ref);
    downloads.unshift(d);
    emit('download', dlOut(d));
    return [d];
  });
  created.forEach((d, i) => setTimeout(() => simulate(d), i * 2500));
  return { batch_id, queued: created.length + joined, already_owned: 0, expanding: false };
}

// --- imports (Перенести музыку) ----------------------------------------------------------------------
// Sign-in by code takes 8 s, reading the collection 3 s, matching 6 s; «Скачать всё» downloads over 20 s.

type ImpItem = { id: number; collection_id: number; position: number; kind: 'track' | 'album' | 'artist'; artist: string; title: string; album: string | null; duration_s: number | null; cover_url: string | null; ref: string | null; match: string; nd: string | null; picked?: number; skipped?: boolean };
type ImpColl = { id: number; kind: string; title: string; selected: boolean };
type Imp = { id: number; method: string; account: string | null; mode: string; keep_token: boolean; download_albums: boolean; has_token: boolean; created: number; started: number | null; cancelled: boolean; colls: ImpColl[]; items: ImpItem[] };
let impSeq = 0;
let impItemSeq = 0;
let imps: Imp[] = [];

const IMP_NAMES = ['Кукушка', 'Группа крови', 'Звезда по имени Солнце', 'Хочешь?', 'Мотылёк', 'Демоны', 'Где ты', 'Кассета', 'Искала', 'Прогулка', 'Город золотой', 'Осень', 'Ветер', 'Луна', 'Нет такого трека'];
const IMP_ARTISTS = ['Кино', 'Земфира', 'Три дня дождя', 'ДДТ', 'Аквариум', 'Мона'];

function newImport(method: string, account: string | null): Imp {
  const id = ++impSeq;
  const colls: ImpColl[] = [
    { id: id * 10 + 1, kind: 'likes', title: 'Мне нравится', selected: true },
    { id: id * 10 + 2, kind: 'playlist', title: 'В дорогу', selected: true },
    { id: id * 10 + 3, kind: 'playlist', title: 'Для работы — длинное название плейлиста, которое не влезает', selected: true },
    { id: id * 10 + 4, kind: 'albums', title: 'Альбомы', selected: true },
    { id: id * 10 + 5, kind: 'artists', title: 'Исполнители', selected: true },
    ...(method === 'profile' ? [] : [{ id: id * 10 + 6, kind: 'dislikes', title: 'Не нравится', selected: true }]),
  ];
  const sizes: Record<string, number> = { likes: 46, playlist: 12, albums: 5, artists: 3, dislikes: 3 };
  const items: ImpItem[] = [];
  for (const c of colls) {
    const n = c.title.startsWith('Для') ? 8 : sizes[c.kind];
    for (let i = 0; i < n; i++) {
      const k = (i * 7 + c.id) % IMP_NAMES.length;
      const artist = IMP_ARTISTS[(i + c.id) % IMP_ARTISTS.length];
      const match = c.kind === 'artists' ? (i === 2 ? 'not_found' : 'exact') : k === 14 || i % 11 === 5 ? 'not_found' : i % 9 === 4 ? 'probable' : i % 4 === 0 ? 'owned' : 'exact';
      const base = { id: ++impItemSeq, collection_id: c.id, position: i, artist, nd: match === 'owned' ? `nd${5001 + (i % 3)}` : null };
      if (c.kind === 'albums') items.push({ ...base, kind: 'album', title: RELEASES[i].title, album: null, duration_s: null, cover_url: cover(RELEASES[i].title), ref: `dz:${100 + i}`, match: i === 2 ? 'owned' : 'exact', nd: null });
      else if (c.kind === 'artists') items.push({ ...base, kind: 'artist', artist: '', title: ARTISTS[i].name, album: null, duration_s: null, cover_url: ARTISTS[i].picture_url, ref: match === 'exact' ? ARTISTS[i].ref : null, match });
      else items.push({ ...base, kind: 'track', title: IMP_NAMES[k], album: 'Альбом ' + artist, duration_s: 160 + ((i * 29) % 140), cover_url: cover(IMP_NAMES[k]), ref: match === 'not_found' ? null : `dz:${1000 + (i % 8) * 100 + 1 + (i % 5)}`, match });
    }
  }
  return { id, method, account, mode: 'economy', keep_token: false, download_albums: false, has_token: method !== 'profile', created: Date.now(), started: null, cancelled: false, colls, items };
}

function impStatus(m: Imp): string {
  const t = Date.now() - m.created;
  if (m.cancelled) return 'cancelled';
  if (m.started == null) {
    if (m.method === 'device' && t < 8000) return 'auth';
    if (t < (m.method === 'device' ? 11000 : 3000)) return 'fetching';
    return 'preview';
  }
  const s = Date.now() - m.started;
  return s < 6000 || (m.mode === 'full' && s < 26000) ? 'running' : 'done';
}

function impItemState(m: Imp, it: ImpItem): Json {
  const sel = m.items.filter((x) => m.colls.find((c) => c.id === x.collection_id)?.selected);
  const i = sel.indexOf(it);
  const s = m.started == null ? -1 : Date.now() - m.started;
  const coll = m.colls.find((c) => c.id === it.collection_id)!;
  if (it.skipped) return { status: 'skipped' };
  if (it.picked) {
    const d = downloads.find((x) => x.id === it.picked);
    return { status: 'queued', download_item_id: it.picked, download_status: d?.status ?? 'queued' };
  }
  if (m.started == null || (it.match !== 'owned' && s < (6000 * (i + 1)) / Math.max(1, sel.length))) return { status: it.match === 'owned' ? 'done' : 'pending', match: it.match === 'owned' ? 'owned' : null };
  if (coll.kind === 'dislikes') return { status: 'skipped' };
  if (it.match === 'not_found') return { status: 'not_found' };
  if (it.match === 'owned') return { status: 'done' };
  if (it.kind !== 'track') return { status: it.kind === 'album' ? 'waiting' : 'done' };
  if (m.mode === 'economy') return { status: 'waiting' };
  return s - 6000 > (20000 * (i + 1)) / sel.length ? { status: 'done', nd: `nd${7000 + it.id}` } : { status: 'queued', download_status: 'queued' };
}

function impItemOut(m: Imp, it: ImpItem): Json {
  const st = impItemState(m, it);
  const { nd, picked: _p, skipped: _s, ...rest } = it;
  return { ...rest, match: 'match' in st ? st.match : it.match, score: it.match === 'probable' ? 0.82 : 1, nd_song_id: (st.nd as string) ?? nd, download_item_id: null, download_status: null, error: null, ...st };
}

function impOut(m: Imp) {
  const status = impStatus(m);
  const sel = new Set(m.colls.filter((c) => c.selected).map((c) => c.id));
  const tracks = m.items.filter((x) => x.kind === 'track' && sel.has(x.collection_id) && m.colls.find((c) => c.id === x.collection_id)?.kind !== 'dislikes');
  const outs = tracks.map((x) => impItemOut(m, x));
  const owned = tracks.filter((x) => x.match === 'owned').length;
  const count = (f: (o: Json) => boolean) => outs.filter(f).length;
  const toDownload = tracks.length - owned;
  return {
    id: m.id, source: 'yandex', method: m.method, status, mode: m.mode, account: m.account ?? (status === 'auth' ? null : 'owner.yandex'),
    has_token: m.has_token, keep_token: m.keep_token, download_albums: m.download_albums,
    auth: status === 'auth' ? { user_code: 'hp5ukzu4', verification_url: 'https://ya.ru/device', expires_at: new Date(m.created + 300000).toISOString() } : null,
    error: null,
    stats: {
      tracks: tracks.length, owned, to_download: toDownload, est_bytes: toDownload * 9_400_000, free_bytes: 412_000_000_000,
      pending: count((o) => o.status === 'pending'), exact: count((o) => o.match === 'exact'), probable: count((o) => o.match === 'probable'),
      not_found: count((o) => o.status === 'not_found'), waiting: count((o) => o.status === 'waiting'),
      downloading: count((o) => o.status === 'queued'), done: count((o) => o.status === 'done' && o.match !== 'owned'), failed: 0,
    },
    collections: status === 'auth' || status === 'fetching' ? [] : m.colls.map((c) => ({
      id: c.id, kind: c.kind, title: c.title, selected: c.selected,
      count: m.items.filter((x) => x.collection_id === c.id).length,
      owned: m.items.filter((x) => x.collection_id === c.id && x.match === 'owned' && x.kind === 'track').length,
      cover_url: null, nd_playlist_id: c.kind === 'playlist' && m.started ? `pl${c.id}` : null,
    })),
    created_at: new Date(m.created).toISOString(), started_at: m.started ? new Date(m.started).toISOString() : null,
    finished_at: status === 'done' ? now() : null,
  };
}

// --- listening before download ---------------------------------------------------------------------

function trackByRef(ref: string) {
  const n = Number(ref.slice(3)) - 1000;
  return trackOut(Math.floor(n / 100), n % 100);
}

/** Tiny 8-bit 8 kHz WAV with a little melody (a different one per track), so the player has something to play. */
function wav(seconds: number, seed: string): Buffer {
  const rate = 8000;
  const n = Math.round(seconds * rate);
  const buf = Buffer.alloc(44 + n);
  buf.write('RIFF', 0, 'ascii');
  buf.writeUInt32LE(36 + n, 4);
  buf.write('WAVEfmt ', 8, 'ascii');
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(1, 22); // mono
  buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate, 28);
  buf.writeUInt16LE(1, 32);
  buf.writeUInt16LE(8, 34);
  buf.write('data', 36, 'ascii');
  buf.writeUInt32LE(n, 40);
  const base = 220 * 2 ** ((hue(seed) % 12) / 12);
  const steps = [0, 4, 7, 12, 7, 4, 2, 5];
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    const note = Math.floor(t / 0.4);
    const f = base * 2 ** (steps[note % steps.length] / 12);
    const env = Math.min(1, ((t % 0.4) / 0.4) * 8) * (1 - (t % 0.4) / 0.4);
    buf[44 + i] = 128 + Math.round(50 * env * Math.sin(2 * Math.PI * f * t));
  }
  return buf;
}

const listenPicks = new Map<string, Promise<Json | null>>();
/** Files the provider search showed, by their ref "file:<provider>:<id>" (the Hub remembers them the same way). */
const shownFiles = new Map<string, Json>();
/** Navidrome stars of the mock user (♥). */
const starred = new Set<string>(['nd5001', ...likedSongs]);
const favoriteSubjects = new Map<string, FavoriteSubject>();
const favoriteCommands: Json[] = [];
const favoriteEventIds = new Set<string>();
const favoriteControls = { typed: true, failNext: false, holdPending: true };

function mockFavorite(target: FavoriteTarget, liked: boolean, eventId: string, ts: string): FavoriteSubject {
  const id = target.nd_song_id ?? target.nd_album_id ?? target.nd_artist_id;
  const aliases = [...(target.ref ? [`ref:${target.ref}`] : []), ...(id ? [`nd:${id}`] : [])];
  const previous = [...favoriteSubjects.values()].find((s) => s.target_type === target.target_type && s.aliases.some((a) => aliases.includes(a)));
  const key = previous?.key ?? `${target.target_type}:${aliases[0]}`;
  const subject: FavoriteSubject = {
    target_type: target.target_type, key, ref: target.ref ?? previous?.ref, nd_id: id ?? previous?.nd_id,
    aliases: [...new Set([...(previous?.aliases ?? []), ...aliases])], liked, event_id: eventId,
    liked_since: liked ? (previous?.liked ? previous.liked_since : ts) : null,
    provenance: 'own', target_data: target.target_data,
  };
  favoriteSubjects.set(key, subject);
  if (target.target_type === 'track') {
    if (id) { if (liked) starred.add(id); else starred.delete(id); }
    if (target.ref) {
      if (liked && !id) {
        starRefs.add(target.ref);
        if (!favoriteControls.holdPending) {
          const t = RELEASES.flatMap((r, i) => Array.from({ length: r.n }, (_, n) => trackOut(i, n + 1))).find((x) => x.ref === target.ref);
          enqueue([{ artist: t?.artist ?? 'Исполнитель', title: t?.title ?? 'Трек', ref: target.ref }], true);
        }
      } else if (!liked) {
        starRefs.delete(target.ref);
        for (const d of downloads.filter((x) => x.ref === target.ref && x.star && ACTIVE.includes(x.status))) {
          d.star = false;
          if (!d.requested && d.status === 'queued') setDl(d, { status: 'cancelled' });
        }
      }
    }
  }
  return subject;
}
const playEvents: Json[] = [];
let dislikes: Json[] = [];
const listenAudio = new Map<string, Buffer>();

// «Моя волна»: the catalog's tracks (they play like any) under many artists
const WAVE_ARTISTS = ['Три дня дождя', 'Земфира', 'Кино', 'Сплин', 'Мураками', 'Pyrokinesis', 'Монеточка', 'Буерак', 'Пошлая Молли', 'Кис-кис', 'Ночные Снайперы', 'Дайте танк (!)'];
const waves = new Map<string, { played: string[]; skipped: Set<string>; title: string; seed: Json | null }>();

function waveTrack(ref: string) {
  const file = shownFiles.get(ref);
  if (file) return { ...file, ref, source: 'file', id: String(file.provider_id), title: String(file.title),
    artist: String(file.artist), artists: [String(file.artist)], owned: false,
    generator: 'seed', generator_title: 'С чего началась волна', familiar: false, sim: null };
  const library = libTracks.find((t) => `nd:nd${t.id}` === ref);
  if (library) return { ...ndSongOut(library), generator: 'seed', generator_title: 'С чего началась волна', familiar: true, sim: null };
  const t = trackByRef(ref);
  const artist = WAVE_ARTISTS[Number(ref.slice(3)) % WAVE_ARTISTS.length];
  return { ...t, artist, artists: [artist], artist_ref: `dz:9${WAVE_ARTISTS.indexOf(artist)}`, generator: 'ytm_radio', generator_title: 'Похожее: радио YouTube Music', familiar: t.owned, sim: null };
}

function wavePick(w: { played: string[]; skipped: Set<string> }, queued: string[], first: Json[] = []) {
  const pool = searchTracks().map((t) => waveTrack(t.ref));
  const order = [...queued.map((r) => pool.find((t) => t.ref === r)).filter((t) => !!t), ...pool];
  const out: Json[] = [...first];
  const recent = w.played.slice(-7).map((r) => waveTrack(r).artist);
  for (const t of order) {
    if (out.length >= 5) break;
    const seq = [...recent, ...out.map((x) => String(x.artist))].slice(-7);
    if (w.played.includes(t.ref) || out.some((x) => x.ref === t.ref) || seq.includes(t.artist) || w.skipped.has(t.artist)) continue;
    out.push(t);
  }
  return out;
}

// «Умное перемешивание» (MIXES.md 8): the order of a list's own songs and recommendations between them — one after
// every 3 own songs (every 2 in a list of 15 or fewer), as the Hub does it; «−» hides a track for the list, «+» adds it
const SMART_AHEAD = 3;
const smartSessions = new Map<string, { context: string; own: string[]; artistOf: Map<string, string>; filled: Map<number, string>; used: Set<string>; skipped: Set<string> }>();
const smartHidden = new Map<string, Set<string>>();
const smartOwnPer = (n: number) => (n > 15 ? 3 : 2);
const smartRecCount = (n: number) => (n ? Math.max(1, Math.floor(n / smartOwnPer(n))) : 0);
const smartSlotAfter = (n: number, k: number) => Math.min(n, smartOwnPer(n) * (k + 1)) - 1;
const smartSlotsAhead = (n: number, index: number) =>
  Array.from({ length: smartRecCount(n) }, (_, k) => k).filter((k) => smartSlotAfter(n, k) >= index).slice(0, SMART_AHEAD);

function smartFill(s: NonNullable<ReturnType<typeof smartSessions.get>>, slots: number[]) {
  const hidden = smartHidden.get(s.context) ?? new Set<string>();
  const ownRefs = new Set(s.own);
  const pool = searchTracks().map((tr) => waveTrack(tr.ref)).filter((tr, i, all) =>
    !ownRefs.has(tr.ref) && !hidden.has(tr.ref) && !s.used.has(tr.ref) && !s.skipped.has(String(tr.artist)) && all.findIndex((x) => x.ref === tr.ref) === i);
  const out: Json[] = [];
  for (const k of slots) {
    if (s.filled.has(k)) continue;
    const after = smartSlotAfter(s.own.length, k);
    const near = [s.own[after], s.own[after + 1]].map((id) => (id ? s.artistOf.get(id) : undefined));
    const recent = [...s.filled.values()].slice(-7).map((r) => String(waveTrack(r).artist));
    const pick = pool.find((tr) => !recent.includes(String(tr.artist)) && !near.includes(String(tr.artist))) ?? pool[0];
    if (!pick) break;
    pool.splice(pool.indexOf(pick), 1);
    s.filled.set(k, pick.ref);
    s.used.add(pick.ref);
    out.push({ ...pick, generator: 'ytm_radio', generator_title: 'Похожее: радио YouTube Music', after: s.own[after], slot: k });
  }
  return out;
}

/** The Hub "searching the sources": ~1.5 s once per track. dz:1109 is found nowhere, dz:1304 only as a preview. */
function listenPick(ref: string): Promise<Json | null> {
  let pick = listenPicks.get(ref);
  if (!pick && ref.startsWith('file:')) {
    // a provider's file: no search, but opening it takes a moment (SoundCloud: the Hub downloads a copy first)
    const c = shownFiles.get(ref);
    pick = delay(c?.provider === 'soundcloud' ? 2500 : 1000).then(() =>
      c
        ? {
            ref,
            kind: 'full',
            artist: c.artist,
            title: c.title,
            duration_s: c.duration_s ?? null,
            provider: c.provider,
            bitrate_kbps: c.bitrate_kbps ?? null,
            page_url: c.page_url,
            nd_song_id: null,
            stream_url: `/api/listen/stream?ref=${encodeURIComponent(ref)}`,
          }
        : null,
    );
    listenPicks.set(ref, pick);
  }
  if (!pick) {
    pick = delay(1500).then(() => {
      if (ref === 'dz:1109') return null;
      const t = trackByRef(ref);
      const preview = ref === 'dz:1304';
      const yt = Number(ref.slice(3)) % 2 === 1;
      return {
        ref,
        kind: t.owned ? 'library' : preview ? 'preview' : 'full',
        artist: t.artist,
        title: t.title,
        duration_s: preview ? 30 : t.duration_s,
        provider: preview || t.owned ? null : yt ? 'ytdlp' : 'hitmoz',
        bitrate_kbps: preview || t.owned ? null : yt ? 130 : 320,
        page_url: null,
        nd_song_id: t.owned ? t.nd_song_id : null,
        stream_url: `/api/listen/stream?ref=${encodeURIComponent(ref)}`,
      };
    });
    listenPicks.set(ref, pick);
  }
  return pick;
}

function sendRange(req: IncomingMessage, res: ServerResponse, body: Buffer, type: string) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '');
  res.setHeader('Accept-Ranges', 'bytes');
  res.setHeader('Content-Type', type);
  if (!m) {
    res.setHeader('Content-Length', body.length);
    res.statusCode = 200;
    res.end(body);
    return;
  }
  const start = m[1] ? Number(m[1]) : Math.max(0, body.length - Number(m[2]));
  const end = m[1] && m[2] ? Math.min(Number(m[2]), body.length - 1) : body.length - 1;
  if (start >= body.length) {
    res.statusCode = 416;
    res.setHeader('Content-Range', `bytes */${body.length}`);
    res.end();
    return;
  }
  res.statusCode = 206;
  res.setHeader('Content-Range', `bytes ${start}-${end}/${body.length}`);
  res.setHeader('Content-Length', end - start + 1);
  res.end(body.subarray(start, end + 1));
}

// --- HTTP helpers ---------------------------------------------------------------------------------

function send(res: ServerResponse, status: number, body?: unknown, type = 'application/json; charset=utf-8') {
  res.statusCode = status;
  res.setHeader('Content-Type', type);
  res.end(body === undefined ? '' : typeof body === 'string' ? body : JSON.stringify(body));
}

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  return Buffer.concat(chunks);
}

async function readJson(req: IncomingMessage): Promise<Json> {
  const b = await readBody(req);
  try {
    return b.length ? (JSON.parse(b.toString('utf8')) as Json) : {};
  } catch {
    return {};
  }
}

function multipartFiles(buf: Buffer): Array<{ field: string; filename: string; size: number }> {
  const text = buf.toString('latin1');
  const out: Array<{ field: string; filename: string; size: number }> = [];
  const re = /name="([^"]+)"; filename="([^"]*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) out.push({ field: m[1], filename: Buffer.from(m[2], 'latin1').toString('utf8'), size: Math.round(buf.length / 1.0) });
  return out;
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

// --- admin -----------------------------------------------------------------------------------------

/** Hub-style naive UTC time `sec` seconds ago. */
const ago = (sec: number) => new Date(Date.now() - sec * 1000).toISOString().slice(0, 19);

let inviteSeq = 2;
let invites = [
  { id: 2, code: 'K7QM-3XPA', note: 'Вера', status: 'active', created_at: ago(3600), expires_at: ago(-6 * 86400), used_at: null as string | null, used_by: null as string | null },
  { id: 1, code: 'PZ4N-8WRT', note: 'Анна', status: 'used', created_at: ago(9 * 86400), expires_at: ago(-86400), used_at: ago(8 * 86400), used_by: 'anna' },
];

function adminUser(over: Json) {
  return {
    id: null, username: '', name: '', is_admin: false, in_hub: true, created_at: ago(30 * 86400), last_seen_at: null,
    player_seen_at: null, google_email: null, invite_note: null, ai_cost_usd: 0, ai_cost_30d_usd: 0, ai_requests: 0,
    ai_last_at: null, downloads_done: 0, downloads_failed: 0, downloads_active: 0, tracks_added: 0, uploads: 0,
    starred_songs: 0, playlists: 0, ...over,
  };
}

function adminUsers() {
  const users = [
    adminUser({ id: 1, username: 'owner', name: 'Владелец', is_admin: true, last_seen_at: ago(20), player_seen_at: ago(3600),
      ai_cost_usd: 0.4123, ai_cost_30d_usd: 0.0871, ai_requests: 57, ai_last_at: ago(300), downloads_done: 412,
      downloads_failed: 3, tracks_added: 398, uploads: 5, starred_songs: 120, playlists: 7 }),
    adminUser({ id: 2, username: 'anna', name: 'Анна', last_seen_at: ago(3 * 3600), player_seen_at: ago(600),
      google_email: 'anna@gmail.com', invite_note: 'Анна', ai_cost_usd: 0.0042, ai_cost_30d_usd: 0.0042, ai_requests: 2,
      ai_last_at: ago(86400), downloads_done: 23, downloads_active: 1, tracks_added: 21, starred_songs: 31, playlists: 1 }),
    adminUser({ id: 3, username: 'boris', name: 'Борис', in_hub: false, player_seen_at: ago(5 * 86400), starred_songs: null, playlists: null }),
  ];
  return { users, ai_cost_usd: 0.4165, ai_cost_30d_usd: 0.0913, library_tracks: 1532 };
}

// --- «Микс дня» and the taste onboarding -------------------------------------------------------------
// The mock user starts with no history: «Что вы слушаете?» first; a pick (or «Собрать заново») builds a mix in ~4 s.
// Its songs that are not in the library "download" one every 3 s.

const MIX_MODES: Record<string, string> = { normal: 'Обычный', favorites: 'Любимое', discover: 'Незнакомое' };
const MIX_GENERATORS: Array<[string, string]> = [
  ['favorites', 'Любимое'],
  ['ytm_radio', 'Похожее: радио YouTube Music'],
  ['stash', 'Тайник'],
  ['yandex_similar', 'Незнакомое: похожие Яндекса'],
  ['lastfm_similar', 'Похожее: Last.fm'],
  ['similar_artists', 'Незнакомое: похожие исполнители'],
  ['new_releases', 'Новинки'],
];
const TASTE_ARTISTS = [
  ...ARTISTS,
  ...['Сплин', 'ДДТ', 'Аквариум', 'Би-2', 'Пошлая Молли', 'Монеточка', 'Нервы', 'Король и Шут', 'Lumen', 'Мумий Тролль', 'Полина Гагарина', 'Скриптонит', 'Баста', 'Звери', 'Ленинград'].map((name, i) => ({
    ref: `dz:${2001 + i}`,
    source: 'dz',
    id: String(2001 + i),
    name,
    picture_url: cover(name),
    picture_small_url: cover(name, 250),
    nb_albums: 5 + i,
    nb_fans: 90000 + i * 13000,
  })),
];
const mixSettings = { mode: 'normal', mix_hour: 5, artists: [] as Array<{ ref: string; name: string }> };
let mixBuildAt = 0; // a build is under way until then
let mixSeq = 0;
type MockMix = { id: number; created_at: string; started: number; mode: string; tracks: Json[] };
let curMix: MockMix | null = null;
const pastMixes: MockMix[] = []; // replaced mixes stay readable, as in the Hub

function makeMix() {
  const seen = new Set<string>();
  const catalog = searchTracks()
    .filter((t) => !seen.has(t.ref) && !!seen.add(t.ref))
    .slice(0, 44);
  // songs only the library has: `source: "nd"`
  const own = libTracks.slice(0, 6).map((t) => {
    const lt = libTrackOut(t);
    return {
      ref: `nd:${lt.nd_song_id}`,
      source: 'nd',
      id: lt.nd_song_id,
      title: lt.title,
      artist: lt.artist,
      artists: [lt.artist],
      artist_ref: null,
      album_ref: null,
      album_title: lt.album,
      duration_s: lt.duration_s,
      cover_url: cover(lt.album ?? lt.title),
      cover_small_url: cover(lt.album ?? lt.title, 250),
      owned: true,
      library_track_id: lt.id,
      nd_song_id: lt.nd_song_id,
      downloading: false,
    };
  });
  const tracks = [...catalog, ...own]
    .map((t, i) => ({ t, k: (i * 37) % 50 }))
    .sort((a, b) => a.k - b.k)
    .map(({ t }, i) => {
      const [generator, generator_title] = MIX_GENERATORS[(i * 3) % MIX_GENERATORS.length];
      return { ...t, position: i, generator, generator_title, generators: [generator], familiar: i % 5 < 2, score: Math.round((3 - i * 0.04) * 100) / 100, sim: i % 4 ? 0.3 - i * 0.004 : null };
    });
  if (curMix) pastMixes.push(curMix);
  curMix = { id: ++mixSeq, created_at: now(), started: Date.now(), mode: mixSettings.mode, tracks };
}

function mixSettingsOut() {
  const next = new Date();
  next.setHours(mixSettings.mix_hour, 0, 0, 0);
  if (next <= new Date()) next.setDate(next.getDate() + 1);
  return { ...mixSettings, timezone: 'Europe/Moscow', next_build_at: next.toISOString(), nd_playlist_id: curMix ? 'pl-mix' : null };
}

function mixOut(mix = curMix) {
  if (!mix) return null;
  const elapsed = Date.now() - mix.started;
  let waiting = 0;
  const tracks = mix.tracks.map((t) => {
    if (t.owned) return t;
    const ready = elapsed > ++waiting * 3000;
    return { ...t, owned: ready, downloading: !ready, nd_song_id: ready ? `ndm${t.id}` : null, library_track_id: ready ? 9000 + Number(t.id) : null };
  });
  const mode = mix.mode;
  const top = [...new Set(tracks.map((t) => String(t.artist)))].slice(0, 3);
  return {
    id: mix.id,
    title: mode === 'discover' ? 'Новое для смелых: инди и рок на вечер' : 'Вечерний русский рок и эмо на четверг',
    description: `${top.join(', ')} и другие: ${tracks.filter((t) => t.familiar).length} знакомых и ${tracks.filter((t) => !t.familiar).length} новых песен — для тёплого вечера после работы.`,
    mode,
    mode_title: MIX_MODES[mode],
    created_at: mix.created_at,
    nd_playlist_id: 'pl-mix',
    playlist_status: 'published',
    playlist_error: null,
    ready: tracks.filter((t) => t.owned).length,
    total: tracks.length,
    tracks,
    generators: Object.fromEntries(MIX_GENERATORS.map(([g]) => [g, { tracks: 50, used: 7, seconds: 1.2 }])),
  };
}

function mixToday() {
  if (mixBuildAt && Date.now() >= mixBuildAt) {
    mixBuildAt = 0;
    makeMix();
  }
  return { mix: mixOut(), building: mixBuildAt > 0, settings: mixSettingsOut(), onboarding: !curMix && !mixBuildAt && !mixSettings.artists.length };
}

// --- router ----------------------------------------------------------------------------------------

async function handle(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
  const url = new URL(req.url ?? '/', 'http://x');
  const p = decodeURIComponent(url.pathname);
  const m = req.method ?? 'GET';
  const authed = (req.headers.cookie ?? '').includes('hm_session=mock');

  if (p.startsWith('/mock-cover/')) {
    send(res, 200, coverSvg(p.slice(12).replace(/\.svg$/, '')), 'image/svg+xml');
    return true;
  }
  if (p === '/healthz') return send(res, 200, { ok: true, navidrome: true, providers: {}, capabilities: favoriteControls.typed ? ['typed_favorites_v1'] : [] }), true;
  if (!p.startsWith('/api/')) return false;

  await delay(150 + Math.random() * 250);

  if (p === '/api/auth/login' && m === 'POST') {
    const b = await readJson(req);
    if (!b.username || b.password !== 'test') return send(res, 401, { detail: 'Неверный логин или пароль' }), true;
    res.setHeader('Set-Cookie', 'hm_session=mock; Path=/; HttpOnly; SameSite=Lax');
    return send(res, 200, { id: 1, username: String(b.username), is_admin: true, public_music_url: 'https://203.0.113.10:8444' }), true;
  }
  if (p === '/api/auth/options') return send(res, 200, { invites: true, email_signup: true, google_client_id: null }), true;
  // email registration: the letter's code is always 123456
  if (p === '/api/auth/email/code' && m === 'POST') {
    const b = await readJson(req);
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(b.email ?? '').trim())) return send(res, 400, { detail: 'Проверьте адрес почты' }), true;
    return send(res, 200, { resend_in: 60 }), true;
  }
  if (p === '/api/auth/email/register' && m === 'POST') {
    const b = await readJson(req);
    if (String(b.code ?? '').replace(/\D/g, '') !== '123456') return send(res, 400, { detail: 'Неверный код: осталось попыток — 4' }), true;
    if (String(b.password ?? '').length < 6) return send(res, 400, { detail: 'Пароль — не короче 6 символов' }), true;
    res.setHeader('Set-Cookie', 'hm_session=mock; Path=/; HttpOnly; SameSite=Lax');
    return send(res, 200, { id: 6, username: String(b.username), is_admin: false, email: String(b.email).trim().toLowerCase(), public_music_url: 'https://203.0.113.10:8444' }), true;
  }
  if (p === '/api/auth/register' && m === 'POST') {
    const b = await readJson(req);
    const code = String(b.code ?? '').toUpperCase().replace(/[^0-9A-Z]/g, '');
    const inv = invites.find((i) => i.code.replace('-', '') === code && i.status === 'active');
    if (!inv) return send(res, 400, { detail: 'Код приглашения не подходит: проверьте его или попросите новый' }), true;
    if (String(b.password ?? '').length < 6) return send(res, 400, { detail: 'Пароль — не короче 6 символов' }), true;
    Object.assign(inv, { status: 'used', used_at: now().slice(0, 19), used_by: String(b.username) });
    res.setHeader('Set-Cookie', 'hm_session=mock; Path=/; HttpOnly; SameSite=Lax');
    return send(res, 200, { id: 5, username: String(b.username), is_admin: false, public_music_url: 'https://203.0.113.10:8444' }), true;
  }
  if (!authed) return send(res, 401, { detail: 'Требуется вход' }), true;

  if (p === '/api/system/health' && m === 'GET') {
    return send(res, 200, {
      ok: true, status: 'ok', navidrome: true,
      components: { database: 'ok', navidrome: 'ok', workers: 'ok', embedder: 'disabled' },
      queue: { status: 'ok', counts: { queued: 2 }, oldest_age_seconds: { queued: 30 } },
      disk: { music: { status: 'ok', free_bytes: 10 * 1024 ** 3, total_bytes: 100 * 1024 ** 3 } },
      backups: { external_enabled: false }, background: { status: 'ok', tasks: {} },
    }), true;
  }
  if (p === '/api/recommendations/quality' && m === 'GET') {
    const days = Number(url.searchParams.get('days')) || 30;
    const until = now();
    const since = new Date(Date.parse(until) - days * 86400_000).toISOString();
    const empty = { attempts: 0, eligible_plays: 0, completed: 0, early_skips: 0,
      completion_rate: null, early_skip_rate: null, policy_cohorts: [] };
    return send(res, 200, {
      ...empty, days, since, until, metric_version: 'mock-empty', provenance: 'dev mock without listening history',
      starts: 0, playback_errors: 0, interrupted: 0, new_track_plays: 0, new_track_likes: 0, repeats: 0,
      new_track_like_rate: null, repeat_rate: null, sources: [], limitations: ['Демонстрационный режим: истории прослушиваний нет.'],
      comparison: {
        current: { ...empty, since, until },
        previous: { ...empty, since: new Date(Date.parse(since) - days * 86400_000).toISOString(), until: since },
        limitations: ['Нет данных для сравнения.'],
      },
      recordings: { metric_version: 'mock-empty', observations: 0, known: 0, full: 0, not_full: 0, unknown: 0,
        rate: null, provenance: [], limitations: ['Проверки полных записей не выполнялись.'] },
    }), true;
  }

  // Deterministic QA controls; these routes exist only in the dev mock.
  if (p === '/api/mock/favorites') {
    if (m === 'POST') {
      const b = await readJson(req);
      for (const key of ['typed', 'failNext', 'holdPending'] as const) if (typeof b[key] === 'boolean') favoriteControls[key] = b[key];
      if (b.reset) { favoriteSubjects.clear(); favoriteCommands.length = 0; favoriteEventIds.clear(); starRefs.clear(); }
      for (const s of (b.subjects as FavoriteSubject[] | undefined) ?? []) favoriteSubjects.set(s.key, s);
      for (const id of (b.stale_song_ids as string[] | undefined) ?? []) starred.add(id);
      if (b.fail_ref) {
        const ref = String(b.fail_ref);
        starRefs.delete(ref);
        mockFavorite({ target_type: 'track', ref }, false, `rollback:${Date.now()}`, now());
        emit('download', { id: 99999, track_ref: ref, status: 'failed', liked: false });
      }
    }
    return send(res, 200, { ...favoriteControls, subjects: [...favoriteSubjects.values()], commands: favoriteCommands, events: playEvents }), true;
  }

  const insight = insightsMock(p, url);
  if (insight) return send(res, insight.status, insight.body), true;
  if (p === '/api/admin/users') return send(res, 200, adminUsers()), true;
  const au = p.match(/^\/api\/admin\/users\/(\d+)$/);
  if (au) {
    const u = adminUsers().users.find((x) => x.id === Number(au[1]));
    if (!u) return send(res, 404, { detail: 'Нет такого пользователя' }), true;
    return send(res, 200, {
      ...u,
      ai_months: [
        { month: '2026-10', cost_usd: u.ai_cost_30d_usd, requests: Math.ceil(u.ai_requests / 2) },
        { month: '2026-09', cost_usd: u.ai_cost_usd - u.ai_cost_30d_usd, requests: Math.floor(u.ai_requests / 2) },
      ],
      ai_recent: [
        { kind: 'chat', tokens_in: 3200, tokens_out: 210, cost_usd: 0.0021, created_at: ago(40) },
        { kind: 'images', tokens_in: 9100, tokens_out: 640, cost_usd: 0.0062, created_at: ago(3600 * 26) },
        { kind: 'link', tokens_in: 0, tokens_out: 0, cost_usd: 0, created_at: ago(3600 * 50) },
      ],
      recent_downloads: [
        { artist: 'Кино', title: 'Группа крови', status: 'done', created_at: ago(600) },
        { artist: 'Земфира', title: 'Искала', status: 'failed', created_at: ago(7200) },
      ],
    }), true;
  }
  if (p === '/api/admin/invites' && m === 'GET') return send(res, 200, invites), true;
  if (p === '/api/admin/invites' && m === 'POST') {
    const b = await readJson(req);
    const days = Number(b.days ?? 7);
    const abc = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    const raw = Array.from({ length: 8 }, () => abc[Math.floor(Math.random() * abc.length)]).join('');
    const inv = { id: ++inviteSeq, code: `${raw.slice(0, 4)}-${raw.slice(4)}`, note: String(b.note ?? ''), status: 'active',
      created_at: now().slice(0, 19), expires_at: new Date(Date.now() + days * 86400000).toISOString().slice(0, 19), used_at: null as string | null, used_by: null as string | null };
    invites.unshift(inv);
    return send(res, 200, inv), true;
  }
  const di = p.match(/^\/api\/admin\/invites\/(\d+)$/);
  if (di && m === 'DELETE') {
    invites = invites.filter((i) => i.id !== Number(di[1]));
    return send(res, 200, { ok: true }), true;
  }

  if (p === '/api/auth/me') return send(res, 200, { id: 1, username: 'owner', is_admin: true, public_music_url: 'https://203.0.113.10:8444', public_hub_url: 'https://203.0.113.10:8445' }), true;
  if (p === '/api/auth/logout') {
    res.setHeader('Set-Cookie', 'hm_session=; Path=/; Max-Age=0');
    return send(res, 200, { ok: true }), true;
  }

  if (p === '/api/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
    res.write('retry: 3000\n\n');
    clients.add(res);
    const ping = setInterval(() => res.write('event: ping\ndata: {}\n\n'), 20000);
    req.on('close', () => {
      clearInterval(ping);
      clients.delete(res);
    });
    return true;
  }

  // search
  if (p === '/api/search/history') {
    if (m === 'POST') {
      const b = await readJson(req);
      const q = typeof b.query === 'string' ? b.query.trim() : '';
      if (q.length < 2 || q.length > 512) return send(res, 422, { detail: 'Запрос — от 2 до 512 символов' }), true;
      searchHistory = [q, ...searchHistory.filter((v) => v.toLowerCase() !== q.toLowerCase())].slice(0, 30);
      emit('search_history', {});
      return send(res, 200, { ok: true }), true;
    }
    if (m === 'DELETE') {
      searchHistory = [];
      emit('search_history', {});
      return send(res, 200, { ok: true }), true;
    }
    res.setHeader('Cache-Control', 'no-store');
    return send(res, 200, { queries: searchHistory }), true;
  }
  if (p === '/api/search') {
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const artists = ARTISTS.filter((a) => a.name.toLowerCase().includes(q) || q.length < 3);
    return (
      send(res, 200, {
        library: libTracks.filter((t) => t.title.toLowerCase().includes(q) || t.artist.toLowerCase().includes(q)).map(libTrackOut),
        artists,
        albums: RELEASES.map((_, i) => albumOut(i)).slice(0, 6),
        tracks: searchTracks().slice(0, SEARCH_PAGE),
        tracks_next: SEARCH_PAGE,
        errors: q.includes('err') ? ['Deezer недоступен, показаны результаты hitmoz'] : [],
      }),
      true
    );
  }
  if (p === '/api/search/tracks') {
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const offset = Number(url.searchParams.get('offset') ?? 0);
    await delay(700);
    if (q.includes('err')) return send(res, 502, { detail: 'Каталог Deezer недоступен' }), true;
    const all = searchTracks();
    const end = offset + SEARCH_PAGE;
    return send(res, 200, { tracks: all.slice(offset, end), next_offset: end < all.length ? end : null }), true;
  }
  if (p === '/api/search/providers') {
    const q = url.searchParams.get('q') ?? 'Трек';
    const group = url.searchParams.get('group');
    const title = q.slice(0, 1).toUpperCase() + q.slice(1);
    const sources = group === 'reuploads' ? [] : [0, 1, 2].map((i) => hitmozCand('Три дня дождя', title, i));
    if (group === 'reuploads') await delay(1500); // SoundCloud is slow
    const reuploads = group === 'sources' ? [] : [0, 1, 2].map((i) => reuploadCand('Три дня дождя', title, i));
    for (const c of [...sources, ...reuploads]) shownFiles.set(`file:${c.provider}:${c.provider_id}`, c);
    return send(res, 200, { candidates: [...sources, ...reuploads], errors: [] }), true;
  }

  // catalog
  let mm = p.match(/^\/api\/catalog\/artist\/(.+)$/);
  if (mm) {
    const artist = ARTISTS.find((a) => a.ref === mm![1]) ?? ARTISTS[0];
    const all = RELEASES.map((_, i) => albumOut(i, artist));
    return (
      send(res, 200, {
        artist,
        top_tracks: [trackOut(0, 1), trackOut(2, 2), trackOut(0, 3), trackOut(3, 4), trackOut(5, 1), trackOut(1, 7), trackOut(0, 5)],
        albums: all.filter((a) => a.record_type === 'album'),
        singles: all.filter((a) => a.record_type === 'single' || a.record_type === 'ep'),
        compilations: all.filter((a) => a.record_type === 'compile'),
      }),
      true
    );
  }
  mm = p.match(/^\/api\/catalog\/album\/dz:(\d+)$/);
  if (mm) {
    const i = Math.max(0, Math.min(RELEASES.length - 1, Number(mm[1]) - 100));
    const tracks = Array.from({ length: RELEASES[i].n }, (_, k) => trackOut(i, k + 1));
    return send(res, 200, { ...albumOut(i), tracks }), true;
  }

  // «Микс дня» and the taste onboarding
  if (p === '/api/mixes/today') return send(res, 200, mixToday()), true;
  if (p === '/api/mixes/settings' && m === 'GET') return send(res, 200, mixSettingsOut()), true;
  if (p === '/api/mixes/settings' && m === 'PUT') {
    const b = await readJson(req);
    if (b.mode !== undefined && !(String(b.mode) in MIX_MODES)) return send(res, 422, { detail: 'Неверный режим' }), true;
    if (b.mode !== undefined) mixSettings.mode = String(b.mode);
    if (b.mix_hour !== undefined) mixSettings.mix_hour = Number(b.mix_hour);
    return send(res, 200, mixSettingsOut()), true;
  }
  if (p === '/api/mixes/rebuild' && m === 'POST') {
    if (mixBuildAt) return send(res, 200, { queued: false }), true;
    mixBuildAt = Date.now() + 4000;
    return send(res, 200, { queued: true }), true;
  }
  const mixM = /^\/api\/mixes\/(\d+)$/.exec(p);
  if (mixM) {
    const out = mixOut([curMix, ...pastMixes].find((x) => x?.id === Number(mixM[1])) ?? null);
    return (out ? send(res, 200, out) : send(res, 404, { detail: 'Микс не найден' })), true;
  }
  if (p === '/api/taste/onboarding') {
    const q = (url.searchParams.get('q') ?? '').trim().toLowerCase();
    const suggestions = q ? TASTE_ARTISTS.filter((a) => a.name.toLowerCase().includes(q)) : TASTE_ARTISTS;
    return send(res, 200, { needed: !curMix && !mixSettings.artists.length, selected: mixSettings.artists, suggestions }), true;
  }
  if (p === '/api/taste/artists' && m === 'POST') {
    const refs = ((await readJson(req)).artist_refs as string[] | undefined) ?? [];
    if (!refs.length || refs.length > 20) return send(res, 422, { detail: 'От 1 до 20 исполнителей' }), true;
    const picked = refs.map((r) => TASTE_ARTISTS.find((a) => a.ref === r));
    if (picked.some((a) => !a)) return send(res, 404, { detail: 'Исполнитель не найден' }), true;
    const first = !mixSettings.artists.length;
    mixSettings.artists = picked.map((a) => ({ ref: a!.ref, name: a!.name }));
    if (first && !curMix) mixBuildAt = Date.now() + 4000;
    return send(res, 200, mixSettingsOut()), true;
  }

  // listening before download
  if (p === '/api/listen/prefetch' && m === 'POST') {
    const refs = ((await readJson(req)).refs as string[]) ?? [];
    const todo = refs.filter((r) => (/^dz:\d+$/.test(r) || shownFiles.has(r)) && !listenPicks.has(r));
    for (const r of todo) void listenPick(r); // the "background search": a tap on these starts at once
    return send(res, 200, { queued: todo.length }), true;
  }
  if (p === '/api/listen' || p === '/api/listen/stream') {
    const ref = url.searchParams.get('ref') ?? '';
    if (ref.startsWith('nd:')) {
      // a mix's song only the library has, or a song of the library pages: its file
      const lib = ndSong(ref.slice(3));
      const t = curMix?.tracks.find((x) => x.ref === ref) ?? (lib ? ndSongOut(lib) : undefined);
      if (!t) return send(res, 404, { detail: 'Песни нет в библиотеке' }), true;
      const pick = { ref, kind: 'library', artist: t.artist, title: t.title, duration_s: t.duration_s, provider: null, bitrate_kbps: null, page_url: null, nd_song_id: t.nd_song_id, stream_url: `/api/listen/stream?ref=${encodeURIComponent(ref)}` };
      if (p === '/api/listen') return send(res, 200, pick), true;
      return sendRange(req, res, wav(Number(t.duration_s) || 30, String(t.title)), 'audio/wav'), true;
    }
    if (!/^dz:\d+$/.test(ref) && !ref.startsWith('file:')) return send(res, 400, { detail: 'Неверная ссылка на трек' }), true;
    const pick = await listenPick(ref);
    if (!pick) return send(res, 404, { detail: ref.startsWith('file:') ? 'Файл не найден: повторите поиск' : 'Трек не нашёлся ни в одном источнике' }), true;
    if (p === '/api/listen') return send(res, 200, pick), true;
    let audio = listenAudio.get(ref);
    if (!audio) listenAudio.set(ref, (audio = wav(Number(pick.duration_s) || 30, String(pick.title))));
    return sendRange(req, res, audio, 'audio/wav'), true;
  }

  // downloads
  if (p === '/api/downloads' && m === 'POST') {
    const b = await readJson(req);
    let items: Array<{ artist: string; title: string; album?: string | null; ref?: string }> = [];
    if (Array.isArray(b.items)) items = (b.items as Json[]).map((x) => ({ artist: String(x.artist), title: String(x.title) }));
    if (Array.isArray(b.track_refs))
      items = (b.track_refs as string[]).map((ref) => {
        const f = shownFiles.get(ref);
        if (f) return { artist: String(f.artist), title: String(f.title), ref };
        const n = Number(ref.slice(3)) - 1000;
        const t = trackOut(Math.floor(n / 100), n % 100);
        return { artist: t.artist, title: t.title, album: t.album_title, ref };
      });
    if (typeof b.album_ref === 'string') {
      const i = Number(b.album_ref.slice(3)) - 100;
      items = Array.from({ length: RELEASES[i]?.n ?? 0 }, (_, k) => trackOut(i, k + 1))
        .filter((t) => !t.owned)
        .map((t) => ({ artist: t.artist, title: t.title, album: t.album_title, ref: t.ref }));
    }
    if (typeof b.artist_ref === 'string') items = [trackOut(0, 1), trackOut(0, 2), trackOut(3, 1)].map((t) => ({ artist: t.artist, title: t.title, album: t.album_title, ref: t.ref }));
    const r = enqueue(items, !!b.star);
    let playlist_id = (b.playlist_id as string) ?? null;
    if (!playlist_id && typeof b.new_playlist_name === 'string') {
      playlist_id = `pl${playlists.length + 1}`;
      playlists.unshift({ id: playlist_id, name: b.new_playlist_name, song_count: 0, duration_s: 0 });
    }
    return send(res, 200, { ...r, playlist_id }), true;
  }
  if (p === '/api/downloads' && m === 'GET') return send(res, 200, downloads.map(dlOut)), true;
  if (p === '/api/downloads/page' && m === 'GET') {
    const visible = downloads.map(dlOut).filter((d) => !d.star).sort((a, b) => b.id - a.id);
    const active: Record<string, true> = { queued: true, searching: true, downloading: true, tagging: true, importing: true };
    const counts = {
      all: visible.length,
      active: visible.filter((d) => active[d.status]).length,
      problems: visible.filter((d) => d.status === 'failed' || d.status === 'needs_review').length,
      finished: visible.filter((d) => d.status === 'done' || d.status === 'cancelled').length,
    };
    const filter = url.searchParams.get('filter') || 'all';
    const before = Number(url.searchParams.get('before_id')) || Infinity;
    const limit = Math.max(1, Math.min(200, Number(url.searchParams.get('limit')) || 50));
    const rows = visible.filter((d) => d.id < before && (filter === 'active' ? active[d.status] : filter === 'problems' ? d.status === 'failed' || d.status === 'needs_review' : true));
    const items = rows.slice(0, limit);
    return send(res, 200, { items, next_cursor: rows.length > limit ? items.at(-1)!.id : null, counts }), true;
  }
  if (p === '/api/downloads/clear') {
    const before = downloads.length;
    downloads = downloads.filter((d) => d.status !== 'done' && d.status !== 'cancelled');
    return send(res, 200, { removed: before - downloads.length }), true;
  }
  mm = p.match(/^\/api\/downloads\/(\d+)$/);
  if (mm) {
    const d = downloads.find((x) => x.id === Number(mm![1]));
    return d ? send(res, 200, { ...dlOut(d), candidates: d.candidates ?? [] }) : send(res, 404, { detail: 'Загрузка не найдена' }), true;
  }
  mm = p.match(/^\/api\/downloads\/items\/(\d+)\/(retry|cancel)$/);
  if (mm) {
    const d = downloads.find((x) => x.id === Number(mm![1]));
    if (!d) return send(res, 404, { detail: 'Загрузка не найдена' }), true;
    if (mm[2] === 'cancel') setDl(d, { status: 'cancelled' });
    else {
      setDl(d, { status: 'queued', progress: 0, error: null });
      simulate(d);
    }
    return send(res, 200, dlOut(d)), true;
  }

  // library
  if (p === '/api/library/albums' && m === 'GET') {
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    return send(res, 200, libAlbums.filter((a) => !q || a.title.toLowerCase().includes(q) || a.album_artist.toLowerCase().includes(q)).map((a) => libAlbumOut(a))), true;
  }
  if (p === '/api/library/albums' && m === 'POST') {
    const b = await readJson(req);
    const a = { id: libAlbums.length + 10, uid: `n${Date.now()}`, title: String(b.title), album_artist: String(b.album_artist), year: (b.year as number) ?? null, release_type: (b.release_type as string) ?? 'album', genre: (b.genre as string) ?? null, deezer_album_id: null, track_count: 0, has_cover: false };
    libAlbums.push(a);
    return send(res, 200, libAlbumOut(a)), true;
  }
  if (p === '/api/library/albums/merge') {
    const b = await readJson(req);
    const target = Number(b.target_id);
    for (const t of libTracks) if ((b.source_ids as number[]).includes(t.album_id)) t.album_id = target;
    libAlbums = libAlbums.filter((a) => !(b.source_ids as number[]).includes(a.id));
    return send(res, 200, libAlbumOut(libAlbums.find((a) => a.id === target)!)), true;
  }
  if (p === '/api/library/tracks/move') {
    const b = await readJson(req);
    for (const t of libTracks) if ((b.track_ids as number[]).includes(t.id)) t.album_id = Number(b.album_id);
    return send(res, 200, libAlbumOut(libAlbums.find((a) => a.id === Number(b.album_id))!)), true;
  }
  mm = p.match(/^\/api\/library\/albums\/(\d+)\/cover$/);
  if (mm) {
    const a = libAlbums.find((x) => x.id === Number(mm![1]));
    if (m === 'PUT' && a) {
      await readBody(req);
      a.has_cover = true;
      return send(res, 200, libAlbumOut(a, true)), true;
    }
    return send(res, 200, coverSvg(a?.title ?? 'x'), 'image/svg+xml'), true;
  }
  mm = p.match(/^\/api\/library\/albums\/(\d+)$/);
  if (mm) {
    const a = libAlbums.find((x) => x.id === Number(mm![1]));
    if (!a) return send(res, 404, { detail: 'Альбом не найден' }), true;
    if (m === 'PATCH') Object.assign(a, await readJson(req));
    return send(res, 200, libAlbumOut(a, true)), true;
  }
  mm = p.match(/^\/api\/library\/tracks\/(\d+)$/);
  if (mm) {
    const t = libTracks.find((x) => x.id === Number(mm![1]));
    if (!t) return send(res, 404, { detail: 'Трек не найден' }), true;
    if (m === 'DELETE') {
      libTracks = libTracks.filter((x) => x !== t);
      return send(res, 200, { ok: true }), true;
    }
    const b = await readJson(req);
    for (const k of ['title', 'artist', 'track_no', 'explicit'] as const) if (b[k] !== undefined && b[k] !== null) (t as Json)[k] = b[k];
    return send(res, 200, libTrackOut(t)), true;
  }
  if (p === '/api/star') {
    const b = await readJson(req);
    favoriteCommands.push(b);
    if (favoriteControls.failNext) {
      favoriteControls.failNext = false;
      return send(res, 503, { detail: 'Тестовая ошибка отметки. Повторите попытку.' }), true;
    }
    const targets = b.targets as FavoriteTarget[] | undefined;
    if (targets?.length) {
      if (!favoriteControls.typed) return send(res, 400, { detail: 'Typed favorites unsupported' }), true;
      const eventIds = targets.map((target, i) => {
        const eventId = String(b.event_id ?? `mock:${Date.now()}:${i}`);
        if (!favoriteEventIds.has(eventId)) {
          favoriteEventIds.add(eventId);
          mockFavorite(target, !b.unstar, eventId, String(b.ts ?? now()));
        }
        return eventId;
      });
      emit('library', { kind: b.unstar ? 'unstarred' : 'starred' });
      return send(res, 200, { ok: true, event_ids: eventIds }), true;
    }
    for (const id of (b.song_ids as string[] | undefined) ?? []) {
      if (b.unstar) starred.delete(id);
      else starred.add(id);
    }
    if (b.unstar) for (const ref of (b.refs as string[] | undefined) ?? []) starRefs.delete(ref);
    return send(res, 200, { ok: true }), true;
  }
  // listening data: player events are logged, «Не нравится» kept in memory
  if (p === '/api/plays/events' && m === 'POST') {
    const events = ((await readJson(req)).events as Json[] | undefined) ?? [];
    const duplicates = events.filter((e) => favoriteEventIds.has(String(e.event_id))).length;
    for (const e of events) if (!favoriteEventIds.has(String(e.event_id))) {
      favoriteEventIds.add(String(e.event_id));
      playEvents.push(e);
    }
    console.log('[mock] play events:', events.map((e) => `${e.kind} ${e.ref ?? e.nd_song_id} ${e.position_s ?? ''}/${e.duration_s ?? ''} ${e.context ?? ''} ${e.launch_origin ?? ''}`).join(' | '));
    return send(res, 200, { accepted: events.length - duplicates, duplicates, rejected: [] }), true;
  }
  // «Моя волна»: 5 tracks at a time from a pool of catalog tracks by many artists; an early skip drops the artist,
  // an artist at most once in 8 tracks, the queued tracks come back first
  if (p === '/api/wave/start' && m === 'POST') {
    const b = await readJson(req);
    const seed = (b.seed ?? null) as Json | null;
    console.log('[mock] wave start:', b.origin ?? '-', b.client ?? '-');
    await delay(1200);
    const id = `w${waves.size + 1}`;
    const w = { played: [] as string[], skipped: new Set<string>(), title: 'Моя волна', seed: null as Json | null };
    const ndId = String(seed?.nd_song_id ?? (String(seed?.ref ?? '').startsWith('nd:') ? String(seed?.ref).slice(3) : ''));
    const library = libTracks.find((t) => `nd${t.id}` === ndId);
    const catalog = searchTracks().find((t) => t.ref === seed?.ref || (ndId && t.nd_song_id === ndId));
    const file = shownFiles.has(String(seed?.ref ?? '')) ? waveTrack(String(seed?.ref)) : null;
    const track = library ? ndSongOut(library) : file ?? catalog;
    if (seed && (seed.ref || seed.nd_song_id) && !track) return send(res, 404, { detail: 'Трек не найден' }), true;
    if (seed?.by_artist && file) return send(res, 404, { detail: 'Исполнитель трека не найден' }), true;
    if (seed?.by_artist && track) {
      w.title = `Волна: ${track.artist}`;
      w.seed = library ? { nd_artist_id: ndArtistId(library.artist), artist: track.artist } : { artist_ref: catalog?.artist_ref, artist: track.artist };
    } else if (track) {
      w.title = `Волна по треку «${track.title}»`;
      w.seed = { ref: track.ref, artist: track.artist, title: track.title };
    } else if (seed?.artist_ref || seed?.nd_artist_id) {
      const artist = ARTISTS.find((a) => a.ref === seed.artist_ref);
      if (seed.artist_ref && !artist) return send(res, 404, { detail: 'Исполнитель не найден' }), true;
      const name = artist?.name ?? String(seed.nd_artist_id);
      w.title = `Волна: ${name}`;
      w.seed = seed.artist_ref ? { artist_ref: seed.artist_ref, artist: name } : { nd_artist_id: seed.nd_artist_id, artist: name };
    } else if (seed) return send(res, 404, { detail: 'Трек не найден' }), true;
    waves.set(id, w);
    const first = track && !seed?.by_artist ? [{ ...track, generator: 'seed', generator_title: 'С чего началась волна' }] : [];
    return send(res, 200, { session_id: id, title: w.title, mode: 'normal', seed: w.seed, tracks: wavePick(w, [], first), pending: false }), true;
  }
  const wm = /^\/api\/wave\/([^/]+)\/next$/.exec(p);
  if (wm && m === 'POST') {
    const w = waves.get(wm[1]);
    if (!w) return send(res, 404, { detail: 'Волна закончилась: запустите новую' }), true;
    const b = await readJson(req);
    const events = (b.played as Json[] | undefined) ?? [];
    for (const e of events) {
      const ref = String(e.ref ?? '');
      if (e.kind === 'start' && !w.played.includes(ref)) w.played.push(ref);
      if ((e.kind === 'skip' && Number(e.position_s ?? 0) < 30) || e.kind === 'dislike') w.skipped.add(waveTrack(ref).artist);
    }
    console.log('[mock] wave next:', events.map((e) => `${e.kind} ${e.ref}`).join(' | '), '| queued', ((b.queued as string[]) ?? []).join(','));
    await delay(400);
    return send(res, 200, { session_id: wm[1], title: w.title, mode: 'normal', seed: w.seed, tracks: wavePick(w, (b.queued as string[]) ?? []), pending: false, recorded: { accepted: events.length, duplicates: 0, rejected: [] } }), true;
  }
  // «Умное перемешивание»: the order of the list's own songs + the recommendations between them (hub/API.md)
  if (p === '/api/smart-shuffle/start' && m === 'POST') {
    const b = await readJson(req);
    const context = String(b.context ?? '');
    const songs = [...new Set((b.songs as string[] | undefined) ?? [])];
    console.log('[mock] smart start:', context, songs.length, 'songs, current', b.current ?? '-', b.origin ?? '-');
    if (!/^(liked|playlist:\S+)$/.test(context) || !songs.length) return send(res, 422, { detail: 'context/songs' }), true;
    await delay(900);
    const artistOf = new Map<string, string>();
    for (const id of songs) {
      const lib = libTracks.find((x) => `nd${x.id}` === id);
      artistOf.set(id, lib ? lib.artist : String(trackByRef(id)?.artist ?? ''));
    }
    const rest = songs.filter((x) => x !== b.current);
    for (let i = rest.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [rest[i], rest[j]] = [rest[j], rest[i]];
    }
    const own = [...(songs.includes(String(b.current)) ? [String(b.current)] : []), ...rest];
    const s = { context, own, artistOf, filled: new Map<number, string>(), used: new Set<string>(), skipped: new Set<string>() };
    const id = `s${smartSessions.size + 1}`;
    smartSessions.set(id, s);
    return send(res, 200, { session_id: id, context, title: b.title ?? null, own, recs: smartFill(s, smartSlotsAhead(own.length, 0)), pending: false, own_per_rec: smartOwnPer(own.length) }), true;
  }
  const sm = /^\/api\/smart-shuffle\/([^/]+)\/(next|hide|add)$/.exec(p);
  if (sm && m === 'POST') {
    const s = smartSessions.get(sm[1]);
    if (!s) return send(res, 404, { detail: 'Умное перемешивание закончилось: запустите его заново' }), true;
    const b = await readJson(req);
    if (sm[2] === 'next') {
      const events = (b.played as Json[] | undefined) ?? [];
      for (const e of events) {
        const ref = String(e.ref ?? '');
        if ((e.kind === 'skip' && Number(e.position_s ?? 0) < 30) || e.kind === 'dislike') s.skipped.add(String(waveTrack(ref).artist));
      }
      console.log('[mock] smart next:', events.map((e) => `${e.kind} ${e.ref} ${e.context ?? ''}`).join(' | '), '| at', b.at ?? '-');
      await delay(300);
      const at = Math.max(0, s.own.indexOf(String(b.at ?? '')));
      return send(res, 200, { session_id: sm[1], context: s.context, title: null, own: [], recs: smartFill(s, smartSlotsAhead(s.own.length, at)), pending: false, own_per_rec: smartOwnPer(s.own.length), recorded: { accepted: events.length, duplicates: 0, rejected: [] } }), true;
    }
    const ref = String(b.ref ?? '');
    if (!ref || !s.used.has(ref)) return send(res, 404, { detail: 'Такой рекомендации в этом перемешивании не было' }), true;
    if (sm[2] === 'hide') {
      smartHidden.set(s.context, (smartHidden.get(s.context) ?? new Set<string>()).add(ref));
      console.log('[mock] smart hide:', s.context, ref);
      return send(res, 200, { ok: true }), true;
    }
    console.log('[mock] smart add:', s.context, ref);
    if (s.context === 'liked') return send(res, 200, { ok: true, status: 'liked', playlist_id: null }), true;
    const playlistId = s.context.slice('playlist:'.length);
    const owned = libTracks.find((x) => `nd${x.id}` === String(b.nd_song_id ?? ''));
    if (owned) {
      const songs = playlistSongs.get(playlistId) ?? [];
      songs.push(`nd${owned.id}`);
      playlistSongs.set(playlistId, songs);
      const pl = playlists.find((x) => x.id === playlistId);
      if (pl) pl.song_count = songs.length;
    }
    return send(res, 200, { ok: true, status: owned ? 'added' : 'downloading', playlist_id: playlistId }), true;
  }
  if (p === '/api/dislikes' && m === 'GET') return send(res, 200, dislikes), true;
  if (p === '/api/dislikes' && m === 'POST') {
    const b = await readJson(req);
    const row = { id: dislikes.length + 1, ref: b.ref ?? null, nd_song_id: b.nd_song_id ?? null, artist_ref: b.artist_ref ?? null, artist: b.artist ?? null, title: b.title ?? null, source: 'pwa', created_at: new Date().toISOString() };
    dislikes = [row, ...dislikes];
    return send(res, 200, row), true;
  }
  if (p === '/api/dislikes' && m === 'DELETE') {
    const q = url.searchParams;
    const before = dislikes.length;
    dislikes = dislikes.filter((d) =>
      q.get('artist_ref') ? d.artist_ref !== q.get('artist_ref') : !((q.get('ref') && d.ref === q.get('ref')) || (q.get('nd_song_id') && d.nd_song_id === q.get('nd_song_id'))),
    );
    return send(res, 200, { removed: before - dislikes.length }), true;
  }
  if (p === '/api/starred') {
    // liked in Yandex and waiting for a listen (economy imports)
    const refs = imps.flatMap((imp) =>
      imp.items
        .filter((x) => imp.colls.find((c) => c.id === x.collection_id)?.kind === 'likes' && impItemState(imp, x).status === 'waiting' && x.ref)
        .map((x) => x.ref as string),
    );
    return send(res, 200, { song_ids: [...starred], album_ids: [], artist_ids: [], refs: [...new Set([...refs, ...starRefs])],
      ...(favoriteControls.typed ? { subjects: [...favoriteSubjects.values()] } : {}) }), true;
  }

  // imports
  if (p === '/api/imports' && m === 'GET') return send(res, 200, imps.map(impOut)), true;
  if (p === '/api/imports' && m === 'POST') {
    const b = await readJson(req);
    let account: string | null = null;
    if (b.method === 'profile') {
      const mm = String(b.url ?? '').match(/users\/([\w.-]+)/) ?? String(b.url ?? '').match(/^([\w.-]+)$/);
      if (!mm) return send(res, 400, { detail: 'Нужна ссылка на профиль: music.yandex.ru/users/<логин>/playlists/3' }), true;
      account = mm[1];
    }
    if (b.method === 'token' && !String(b.token ?? '').trim()) return send(res, 400, { detail: 'Вставьте токен' }), true;
    const imp = newImport(String(b.method ?? 'device'), account);
    imps.unshift(imp);
    return send(res, 200, impOut(imp)), true;
  }
  const im = p.match(/^\/api\/imports\/(\d+)(?:\/(items|start|download_all|sync|token|cancel))?$/);
  if (im) {
    const imp = imps.find((x) => x.id === Number(im[1]));
    if (!imp) return send(res, 404, { detail: 'Импорт не найден' }), true;
    const act = im[2];
    if (!act && m === 'DELETE') {
      imps = imps.filter((x) => x !== imp);
      return send(res, 200, { ok: true }), true;
    }
    if (act === 'items') {
      const c = url.searchParams.get('collection_id');
      const match = url.searchParams.get('match')?.split(',');
      const status = url.searchParams.get('status')?.split(',');
      const limit = Number(url.searchParams.get('limit') ?? 200);
      const all = imp.items
        .filter((x) => !c || x.collection_id === Number(c))
        .map((x) => impItemOut(imp, x))
        .filter((o) => (!match || match.includes(String(o.match))) && (!status || status.includes(String(o.status))));
      return send(res, 200, { items: all.slice(0, limit), total: all.length }), true;
    }
    if (act === 'start') {
      const b = await readJson(req);
      const ids = new Set((b.collection_ids as number[]) ?? []);
      for (const col of imp.colls) col.selected = ids.has(col.id);
      Object.assign(imp, { mode: b.mode ?? 'economy', keep_token: !!b.keep_token, download_albums: !!b.download_albums, started: Date.now() });
      emit('import', { id: imp.id, status: 'running' });
      setTimeout(() => emit('import', { id: imp.id, status: impStatus(imp) }), imp.mode === 'full' ? 26500 : 6500);
      return send(res, 200, impOut(imp)), true;
    }
    if (act === 'download_all') imp.mode = 'full';
    if (act === 'token') Object.assign(imp, { has_token: false, keep_token: false });
    if (act === 'cancel') imp.cancelled = true;
    return send(res, 200, impOut(imp)), true;
  }
  const ii = p.match(/^\/api\/imports\/items\/(\d+)\/(pick|skip)$/);
  if (ii) {
    for (const imp of imps) {
      const it = imp.items.find((x) => x.id === Number(ii[1]));
      if (!it) continue;
      if (ii[2] === 'skip') {
        it.skipped = true;
        return send(res, 200, { ok: true }), true;
      }
      enqueue([{ artist: it.artist, title: it.title, album: it.album }]);
      it.picked = downloads[0].id;
      return send(res, 200, { download_item_id: it.picked }), true;
    }
    return send(res, 404, { detail: 'Строка импорта не найдена' }), true;
  }

  // uploads
  if (p === '/api/uploads' && m === 'POST') {
    const files = multipartFiles(await readBody(req)).filter((f) => f.field === 'files');
    const created = files.map((f) => {
      const base = f.filename.replace(/\.[^.]+$/, '');
      const [artist, title] = base.includes(' - ') ? base.split(' - ', 2) : ['', base];
      const u = { id: ++uploadSeq, orig_name: f.filename, size: f.size, status: 'uploaded', tags: { title, artist, album: 'Мой альбом', album_artist: artist, track_no: null, year: 2026, has_cover: uploadSeq % 2 === 0, duration_s: 200 }, created_at: now() };
      uploads.push(u);
      return u;
    });
    return send(res, 200, created), true;
  }
  if (p === '/api/uploads' && m === 'GET') return send(res, 200, uploads.filter((u) => u.status === 'uploaded')), true;
  if (p === '/api/uploads/commit') {
    await readBody(req);
    const n = uploads.length;
    uploads = [];
    emit('library', { kind: 'imported', album_id: 4 });
    return send(res, 200, { album_id: 4, imported: n }), true;
  }
  mm = p.match(/^\/api\/uploads\/(\d+)(\/cover)?$/);
  if (mm) {
    if (mm[2]) return send(res, 200, coverSvg(`upload${mm[1]}`), 'image/svg+xml'), true;
    uploads = uploads.filter((u) => u.id !== Number(mm![1]));
    return send(res, 200, { ok: true }), true;
  }

  // the Navidrome library
  if (p === '/api/nd/home') {
    await delay(300);
    const all = libAlbums.map((a) => ndAlbumOut(a));
    const artists = ndArtists().sort((a, b) => Number(b.starred) - Number(a.starred) || b.album_count - a.album_count);
    return send(res, 200, { recent: [...all].reverse(), frequent: all.slice(0, 3), newest: all, starred: all.filter((a) => a.starred), radio: artists }), true;
  }
  if (p === '/api/nd/liked') {
    await delay(300);
    const subjects = [...favoriteSubjects.values()].filter((s) => s.target_type === 'track');
    const songs: Json[] = libTracks.filter((t) => {
      const own = subjects.find((s) => s.nd_id === `nd${t.id}`);
      return own ? own.liked : likedSongs.has(`nd${t.id}`) || starred.has(`nd${t.id}`);
    }).map(ndSongOut);
    for (const s of subjects) {
      if (!s.liked || !s.ref || songs.some((t) => t.ref === s.ref || (s.nd_id && t.nd_song_id === s.nd_id))) continue;
      const t = trackByRef(s.ref);
      songs.unshift({ ...t, ...s.target_data, artists: [String(s.target_data?.artist ?? t.artist)],
        ref: s.ref, owned: !!s.nd_id, nd_song_id: s.nd_id ?? null });
    }
    return send(res, 200, { songs, albums: libAlbums.filter((a) => a.id === 2).map((a) => ndAlbumOut(a)), artists: ndArtists().filter((a) => a.starred) }), true;
  }
  if (p === '/api/nd/artists') return send(res, 200, ndArtists()), true;
  mm = p.match(/^\/api\/nd\/artists\/([^/]+)$/);
  if (mm) {
    const artist = ndArtists().find((a) => a.id === decodeURIComponent(mm![1]));
    if (!artist) return send(res, 404, { detail: 'Исполнитель не найден' }), true;
    return send(res, 200, { artist, albums: libAlbums.filter((a) => a.album_artist === artist.name).map((a) => ndAlbumOut(a)) }), true;
  }
  if (p === '/api/nd/albums') {
    const offset = Number(url.searchParams.get('offset') ?? 0);
    return send(res, 200, offset ? [] : libAlbums.map((a) => ndAlbumOut(a)).sort((a, b) => a.title.localeCompare(b.title))), true;
  }
  mm = p.match(/^\/api\/nd\/albums\/([^/]+)$/);
  if (mm) {
    const a = libAlbums.find((x) => `ndal${x.id}` === decodeURIComponent(mm![1]));
    return a ? (send(res, 200, ndAlbumOut(a, true)), true) : (send(res, 404, { detail: 'Альбом не найден' }), true);
  }

  // playlists
  if (p === '/api/playlists' && m === 'GET') return send(res, 200, playlists), true;
  if (p === '/api/playlists' && m === 'POST') {
    const b = await readJson(req);
    const pl = { id: `pl${playlists.length + 1}`, name: String(b.name), song_count: 0, duration_s: 0 };
    playlists.unshift(pl);
    return send(res, 200, pl), true;
  }
  mm = p.match(/^\/api\/playlists\/([^/]+)\/(add|remove)$/);
  if (mm) {
    const id = decodeURIComponent(mm[1]);
    const b = await readJson(req);
    const songs = playlistSongs.get(id) ?? [];
    if (mm[2] === 'add') songs.push(...((b.song_ids as string[]) ?? []));
    else for (const i of [...((b.indexes as number[]) ?? [])].sort((x, y) => y - x)) songs.splice(i, 1);
    playlistSongs.set(id, songs);
    const pl = playlists.find((x) => x.id === id);
    if (pl) pl.song_count = songs.length;
    return send(res, 200, { ok: true }), true;
  }
  mm = p.match(/^\/api\/playlists\/([^/]+)$/);
  if (mm) {
    const id = decodeURIComponent(mm[1]);
    const pl = playlists.find((x) => x.id === id);
    if (!pl) return send(res, 404, { detail: 'Плейлист не найден' }), true;
    if (m === 'DELETE') {
      playlists = playlists.filter((x) => x.id !== id);
      return send(res, 200, { ok: true }), true;
    }
    const tracks = (playlistSongs.get(id) ?? []).map(ndSong).filter((t) => !!t).map((t) => ndSongOut(t));
    return send(res, 200, { ...pl, song_count: tracks.length, tracks }), true;
  }

  // assistant
  if (p === '/api/assistant/tasks' && m === 'POST') {
    const ct = req.headers['content-type'] ?? '';
    const body = ct.includes('json') ? await readJson(req) : (await readBody(req), {});
    const task: Task = {
      id: ++taskSeq,
      status: 'queued',
      input_kind: ct.includes('multipart') ? 'images' : body.url ? 'link' : 'text',
      context: {},
      rows: [],
      suggested_playlist_name: null,
      confirm_token: null,
      cost_usd: 0,
      created_at: now(),
    };
    tasks.unshift(task);
    const step = (ms: number, patch: Json) =>
      setTimeout(() => {
        Object.assign(task, patch);
        emit('assistant', { id: task.id, status: task.status });
      }, ms);
    step(600, { status: 'extracting' });
    step(2500, { status: 'resolving', source_app: 'yandex_music', page_type: 'playlist' });
    step(4500, { status: 'awaiting_confirmation', rows: assistantRows(), confirm_token: 'tok' + task.id, suggested_playlist_name: 'Вечерний плейлист', context: { playlist_name: 'Вечерний плейлист' } });
    return send(res, 200, task), true;
  }
  if (p === '/api/assistant/chats' && m === 'GET') {
    const list = [...chats].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return send(res, 200, list.map((c) => ({ ...c, messages: c.messages.length }))), true;
  }
  mm = p.match(/^\/api\/assistant\/chats\/(\d+)$/);
  if (mm) {
    const i = chats.findIndex((c) => c.id === Number(mm![1]));
    if (i < 0) return send(res, 404, { detail: 'Чат не найден' }), true;
    if (m === 'DELETE') return chats.splice(i, 1), send(res, 200, { ok: true }), true;
    return send(res, 200, chats[i]), true;
  }
  if (p === '/api/assistant/chat') {
    const b = await readJson(req);
    const msgs = (b.messages as Array<{ role: string; content: string }>) ?? [];
    const lastRaw = msgs[msgs.length - 1]?.content ?? '';
    const last = lastRaw.toLowerCase();
    let chat = b.chat_id ? chats.find((c) => c.id === b.chat_id) : undefined;
    if (b.chat_id && !chat) return send(res, 404, { detail: 'Чат не найден' }), true;
    if (!chat) {
      chat = { id: ++chatSeq, title: lastRaw.slice(0, 80) || 'Скриншот', messages: [], created_at: now(), updated_at: now() };
      chats.push(chat);
    }
    let out: Record<string, unknown>;
    if (last.includes('удали')) {
      const id = ++taskSeq;
      out = {
        reply: 'Плейлист «Тест» удалю после подтверждения.',
        cost_usd: 0.00031,
        done: [],
        task_id: id,
        confirm_token: `chat-${id}`,
        summary: 'Удалить плейлист «Тест»',
        actions: [{ type: 'delete_playlist', playlist_id: 'pl1', name: 'Тест' }],
      };
    } else if (last.includes('скачай') || last.includes('плейлист') || last.includes('яндекс')) {
      enqueue([{ artist: 'Три дня дождя', title: 'Демоны' }]);
      out = {
        reply: 'Готово: 106 понравившихся из Яндекс Музыки теперь в плейлисте «Яндекс Музыка», ♥ с них снято.',
        cost_usd: 0.00041,
        done: ['Создан плейлист «Яндекс Музыка»: 106 треков', '♥ снято: 106 треков'],
        actions: [],
      };
    } else {
      out = { reply: 'У вас в библиотеке 4 альбома: «Магнит», «Группа крови», «Вендетта» и «Записи с репетиции».', cost_usd: 0.00022, actions: [] };
    }
    const reply: ChatMsg = { role: 'assistant', content: String(out.reply), cost: out.cost_usd as number, done: (out.done as string[]) ?? [] };
    if (out.task_id) reply.proposal = { task_id: out.task_id as number, token: String(out.confirm_token), summary: String(out.summary), actions: out.actions as unknown[], state: 'pending' };
    chat.messages.push({ role: 'user', content: lastRaw }, reply);
    chat.updated_at = now();
    return send(res, 200, { ...out, chat_id: chat.id, title: chat.title }), true;
  }
  mm = p.match(/^\/api\/assistant\/chat\/(\d+)\/(confirm|dismiss)$/);
  if (mm) {
    const b = await readJson(req);
    const chat = chats.find((c) => c.id === b.chat_id);
    for (const msg of chat?.messages ?? []) if (msg.proposal?.task_id === Number(mm[1])) msg.proposal.state = mm[2] === 'confirm' ? 'confirmed' : 'dismissed';
    return send(res, 200, mm[2] === 'confirm' ? { ok: true, playlists_deleted: ['pl1'], tracks_deleted: 0 } : { ok: true }), true;
  }
  if (p === '/api/assistant/tasks' && m === 'GET') return send(res, 200, tasks.map((t) => ({ ...t, rows: [] }))), true;
  mm = p.match(/^\/api\/assistant\/tasks\/(\d+)(\/confirm)?$/);
  if (mm) {
    const t = tasks.find((x) => x.id === Number(mm![1]));
    if (!t) return send(res, 404, { detail: 'Задача не найдена' }), true;
    if (mm[2]) {
      const b = await readJson(req);
      if (b.confirm_token !== t.confirm_token) return send(res, 409, { detail: 'Список устарел, обновите страницу' }), true;
      t.status = 'confirmed';
      t.playlist_id = 'pl9';
      t.batch_id = enqueue([{ artist: 'Три дня дождя', title: 'Мотылёк' }, { artist: 'Кино', title: 'Звезда по имени Солнце' }]).batch_id;
      emit('assistant', { id: t.id, status: t.status });
    }
    return send(res, 200, t), true;
  }

  send(res, 404, { detail: 'Не найдено' });
  return true;
}

export function mockApi(): Plugin {
  return {
    name: 'homemusic-mock-api',
    configureServer(server) {
      // Seed one task that waits for confirmation (for screenshots).
      const seeded: Task = { id: ++taskSeq, status: 'awaiting_confirmation', input_kind: 'images', source_app: 'yandex_music', page_type: 'playlist', context: { playlist_name: 'Вечерний плейлист' }, rows: assistantRows(), suggested_playlist_name: 'Вечерний плейлист', confirm_token: 'tok-seed', cost_usd: 0.0007, created_at: now() };
      tasks.unshift(seeded);
      server.middlewares.use((req, res, next) => {
        handle(req, res)
          .then((done) => !done && next())
          .catch((e) => {
            console.error(e);
            send(res, 500, { detail: 'Ошибка мок-сервера' });
          });
      });
    },
  };
}
