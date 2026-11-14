# Hub HTTP API

All JSON. Most models are in `app/api/schemas.py`; player features also define schemas in their route modules and `app/playback_schema.py`. Field names below match those models.
Auth: cookie `hm_session` (HttpOnly, set by login). Protected endpoints return `401` without it; public auth/bootstrap routes, `/healthz` and `/readyz` are marked below.
Errors: `{"detail": "<Russian message>"}` with 4xx/5xx.

## Auth
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/auth/login` | `LoginIn` → `MeOut` (+ cookie). `username` may be the email the account registered with: `MeOut.username` is then its Navidrome name (the player signs in by it) |
| POST | `/api/auth/logout` | → `{ok}` |
| GET | `/api/auth/me` | → `MeOut` |
| GET | `/api/auth/options` | → `AuthOptionsOut` (no auth): `invites`, `email_signup` (registration by email: SMTP is configured), `google_client_id` (null — no Google), `music_url` |
| POST | `/api/auth/register` | `RegisterIn {code, username, password}` → `MeOut` (+ cookie). Creates the Navidrome user for an unused, unexpired invite. `400` bad/used code or name/password, `409` name taken, `429` too many wrong codes |
| POST | `/api/auth/email/code` | `EmailCodeIn {email}` → `EmailCodeOut {resend_in}` (seconds before another letter). Sends a 6-digit code to the address (valid `EMAIL_CODE_MINUTES`, 15); only the latest letter of an address counts. `400` not an address, `409` the address has an account, `429` a letter less than a minute ago or 5 in an hour, `503` `EMAIL_DAILY_LIMIT` letters in 24 h, `502` the letter did not go out, `404` SMTP not configured |
| POST | `/api/auth/email/register` | `EmailRegisterIn {email, code, username, password}` → `MeOut` (+ cookie; `email` set). Creates the Navidrome user like `/register`, no invite. `400` wrong code (5 wrong codes spend the letter), expired/used code, name/password; `409` name or address taken; `429` too many wrong codes; `404` SMTP not configured |
| POST | `/api/auth/google` | `GoogleIn {id_token, code?, username?}` → `GoogleLoginOut` (`MeOut` + `password` of the Navidrome account, for the player) + cookie. Linked Google account → its user; not linked and no `code` → `409`; with `code` → registers as `/register` (random password). `404` Google not configured, `401` token rejected |
| POST | `/api/auth/google/link` | `GoogleLinkIn {id_token}` → `MeOut` — links Google to the signed-in user (`409` linked to someone else) |
| DELETE | `/api/auth/google/link` | → `MeOut` — unlinks |

`MeOut` also has `public_hub_url` and `google_email`. ID tokens are verified against Google's keys
(`app/google_auth.py`): RS256, `aud` = `GOOGLE_CLIENT_ID`, issuer, expiry, verified email.

## Admin (only `is_admin`, otherwise `403`)
| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/admin/users` | → `AdminUsersOut {users: [AdminUserOut], ai_cost_usd, ai_cost_30d_usd, library_tracks}` — every Navidrome user (`id` null, `in_hub` false for those who never opened the Hub): AI spending (all time, 30 days, requests), downloads, tracks added, uploads, ♥ and playlists from Navidrome (null if it did not answer), last seen in the Hub and in the player, invite note |
| GET | `/api/admin/users/{id}` | → `AdminUserDetailOut` (+ `ai_months`, `ai_recent`, `recent_downloads`) |
| GET | `/api/admin/invites` | → `[InviteOut]` (`code` as «ABCD-EFGH», `status` active/used/expired, `used_by`) |
| POST | `/api/admin/invites` | `InviteIn {note, days?}` → `InviteOut` (`days` 1–90, default `INVITE_DAYS`) |
| DELETE | `/api/admin/invites/{id}` | → `{ok}` |

AI spending: every paid call (chat turn, screenshot/link/text recognition) writes an `ai_usage` row with tokens and
`cost_usd` as the provider reported it.

## Admin insights (INSIGHTS.md 7; only `is_admin`, otherwise `403`)

Read-only: no writes to the database, no network (only the embedder's `/v2/profile` for facets, when configured), every
answer `Cache-Control: no-store`. Weights are the production taste code (`load_snapshot` + `calculate_profile`, model
`cfg.mixes.taste_model`); `model=v1` is the **local** v1 baseline (`calculate_baseline`, no catalog enrichment),
labelled in `model_note`. `from`/`to` — local dates (`Settings.timezone`), inclusive, default the last 30 days, at most
3 years; a reversed/too long period or a bad date → `422`. Unknown user/mix/wave/track → `404`. Times are UTC, naive.
Track keys (`nd:<song>`, `dz:…`, `file:…`) go only in the query string (`key=`), URL-encoded. Test traffic (clients
`qa`, `import`, `*-debug`), imported ♥, `favorite:bootstrap/reconcile` and events after `now` are left out, as in the
profile.

| Method | Path | Response |
|---|---|---|
| GET | `/api/admin/insights/overview?from=&to=&include_excluded=` | `InsightsOverviewOut {period, users: [row], totals}` — a row per user: `counted_listens`, `heard_events`, `listened_ms`, `active_days`, `mixes_built`, `mix_launches`, `wave_launches`, `wave_restarts`, `likes`, `dislikes`, `new_tracks`, `new_artists`, `last_activity_at`, `app_version`, `last_seen_at`, `user {id, username, is_admin, excluded}`. Users of `config.yaml` `analytics.exclude_users` (default none) are left out unless `include_excluded=true`, and never in `totals` |
| GET | `/api/admin/users/{id}/listening?from=&to=` | `InsightsListeningOut` (below) |
| GET | `/api/admin/users/{id}/taste?model=` | `InsightsTasteOut`: `model`, `model_note`, `now`, `timezone`, `evidence`, `maturity`, `active_units`, `active_days`, `counts {total, positive (s>0), strong (s≥1), negative, liked, downloaded, album_liked, explicit_dislikes, disliked_artists}`, `facets` (`[{id, weight, artists[2], size, top_members[5]}]`; `null` when the embedder is off/down — `embedder` says `"off"`/`"down"`/`"timeout"`/`"v1"` or `{space_id, model}`), `seeds_today` (`select_profile_seeds`: no cold start, no network — what a mix really took is in the mix), `onboarding_reserve` (`round((1−m)·n)`), `onboarding_artists`, `seeds_note`, `top_artists` (50: `key, name, score` = `A_artist`, `listens, liked, disliked`), `limitations` |
| GET | `/api/admin/users/{id}/taste/tracks?model=&filter=&q=&sort=&limit=&offset=` | `{model, now, embedder, total, offset, rows}`; `filter` `all`/`positive`/`seed`/`negative`/`liked`/`disliked`/`downloaded`, `sort` `s`/`p`/`n`/`last_heard`/`plays`/`title` (descending; `title` ascending), `limit` ≤ 200. Row: `key, ref, nd_song_id, artist, title, owned, s, p` (`positive_raw`), `sat_p, n, liked, disliked, downloaded, album_liked, heard30, familiar, seed_eligible` (in the 30-day seed pool), `last_heard, last_positive, positive_age, plays` (finish / skip ≥ 30 s), `early_skips, facet_id` |
| GET | `/api/admin/users/{id}/taste/track?key=&model=` | `InsightsTasteTrackOut`: the row (any alias of the track as `key`) + `starts, finishes, late_skips`, `contributions` (newest first: `kind` finish/skip/start/like/dislike/dislike_legacy/download/album_like, `ts, event_id, side` P/N/0, `base` (M4 weight before decay), `age` (active units after the event's day), `w, value` (added to P, or to N as a positive amount: Σ P = `p`, Σ N = `n`), `context, client, position_s, duration_s, note` — why a row counts 0: «start — вес 0», «w < 0.01», «старше 3 лет», «импорт — не свой», «♥ снят — вклад 0», «загрузка — вес 0»…), `formula` with the numbers, `artist {key, name, score, positive_sum, negative_sum, tracks_part, likes_part, tracks, this_track, listens, liked, disliked, formula}`, `rest {recent_until, favorites_after, stash_after}` (3/7/60 days after `last_heard`), `facet_cos` (facet id → cosine of the track to the facet center, from the embedder's `/v2/project`; `null` without v2 or the embedder), `mixes` (`mix_id, created_at, state, position, generator, generator_title, score` + outcome fields), `waves` (`session_id, first_handed_at, position, generator, score, value` + outcome fields) |
| GET | `/api/admin/users/{id}/taste/map?model=&basis=global\|user&mix=&wave=&limit=600` | `InsightsMapOut` — the taste map (INSIGHTS.md 8.2). `available` (`false` + `reason`: `model_v1`, `embedder_off`, `embedder_down`, `embedder_timeout`, `embedder_stale_space`; the lists are then empty), `space_id`, `basis` (`global` — the axes of the whole sound space, comparable between users; `user` — fitted on the user's positive tracks, falls back to `global` without any), `explained` (share of the variance of the two axes), `mix_id` / `wave_id` (the lists shown: default the newest active mix and the newest wave session that handed out tracks; a mix/session of the user that does not exist → `404`), `layout_note`, `facets [{id, weight, artists, x, y, fx, fy, color_index}]` (`x, y` the center on the PCA axes; `fx, fy` its anchor of the «Грани» layout; `color_index` the facet's rank by weight), `points [{key, artist, title, layers (profile/mix/wave), x, y, fx, fy, s, p, n, facet_id, facet_cos (in the order of facets), seed, liked, disliked, negative, familiar, generator, generator_title, mix_position, wave_position, outcome}]`, `missing [{key, artist, title, layers}]` (no sound vector: listed, not drawn), `truncated`. Layers: `profile` — songs with `s ≠ 0` or a ♥ / download / 👎, the first `limit` (1…2000) by \|s\| plus today's seeds and every explicit 👎 (`truncated` when more existed); `mix` — the active songs of the mix; `wave` — what the session handed out. `s, p, n` are `null` for a song outside the profile. «Грани» layout (`fx, fy`, x right, y up): the facets sit on the unit circle (heaviest at 12 o'clock, then clockwise), a point is the weighted mean of the anchors with weights `max(0, cos)²`, shrunk to the center by `min(1, max cos / 0.6)`; no positive cosine → `(0, 0)`. `facet_id` — the facet whose `members` hold the song, else the one with the largest cosine. `outcome` as in the mix/wave detail |
| GET | `/api/admin/taste/matrix?by=track\|artist&filter=&q=&sort=&limit=&offset=&model=&include_excluded=` | `{model, by, now, users [{id, username}], rows, total, offset}`. All profiles at one `now`. Track rows join only verified aliases (`ref` ↔ `nd:<song>` that a profile unified), never by «artist\|title»; the key is `nd:<song>`, else the ref. Row: `key, artist, title, owned, cells {"<user id>": {s, p, n, liked, disliked, heard}}, sum_pos, min, max, users_positive, users_negative`. `filter` `all`, `shared_liked` (s > 0 for ≥ 2), `conflict` (s ≥ 1 and s ≤ −1), `single` (one non-zero), `disliked`; `sort` `sum` (default), `spread`, `title`, `user:<id>`. `by=artist`: `A_artist` by `artist_key`, `heard` = the artist's listens, `p`/`n` null |
| GET | `/api/admin/users/{id}/mixes?limit=30` | `[InsightsMixRow]`: `id, created_at, closed_at, status, title, mode, mode_title, taste_model, size, downloaded, launches, started, heard, finished, early_skips, likes, dislikes, top_generators [{generator, title, songs}]` |
| GET | `/api/admin/mixes/{id}` | `InsightsMixOut`: the row + `description, nd_playlist_id`, `funnel {generators [{generator, title, tracks, used, seconds, error, off}], candidates, rejected` (`null` for mixes before 2026-10-31), `kept, sound_scored, active, familiar, new, reserve, failed, launches, started, heard, finished, early_skips, liked, disliked}`, `seeds` (from `stats_json.seed_keys`, with names and `facet_id`), `seed_labels, facets, maturity, embedder, build_seconds`, `items` (active, reserve, failed: name fields + `position, state, familiar, generator, generator_title, gens, score, score_parts` (`null` for old mixes — never recounted), `sim, rank, profile, downloaded_for_mix, cleanup`, outcome fields, `heard_outside` — heard outside this mix while it was in effect), `launch_list`, `artists` |
| GET | `/api/admin/users/{id}/waves?from=&to=&origin=&seed_kind=&limit=&offset=` | `InsightsWavesOut {total, offset, rows, launches, restarts, by_origin, by_seed_kind}` (newest first; the summaries cover the filtered sessions, restarts are not launches). Row: `session_id, created_at, last_activity_at, origin, client, restart_of, restart, seed_kind, seed {title, artist, ref, nd_song_id, artist_ref, nd_artist_id}, mode, taste_model, next_calls, handed, tracks_started, started, heard, finished, early_skips, late_skips, likes, dislikes, listened_ms, downloads, cleared, reconstructed`. `origin=not_recorded` selects sessions without one |
| GET | `/api/admin/waves/{sid}` | `InsightsWaveOut`: the row + `gens` (generator → `{tracks, seconds, error?, off?, title}`), `seeds_used`, `items` (`wave_items` by `position`: name fields + `first_handed_at, last_handed_at, handed_count, generator, generator_title, gens, familiar, score, sim, value` + outcome fields), `events` (`ts, kind, track, position_s, client`) |

Name fields: `key, ref, nd_song_id, artist, title, owned` — local only (library, profile, the latest event metadata,
`catalog_cache`, mix and wave items), else «Неизвестный трек». Outcome fields: `outcome` — `not_played`, `started`,
`heard`, `finished`, `early_skip`, `liked`, `disliked` (the strongest, by that mix's/session's own launches),
`early_skip_at` (seconds), `listened_ms`.

Codes (labels are the PWA's):
- `source` (what a launch played): `mix`, `wave`, `smart` (the smart shuffle's recommendations), `playlist`, `album`, `artist`, `liked`, `search`, `queue`,
  `continuous`, `library`, `other`, `unknown` (no context); in listening blocks also `scrobble` (Navidrome history).
- `origin` (wave) — as `WaveStartIn.origin`; `not_recorded` — an old client sent none. `seed_kind`: `own`, `track`,
  `track_artist`, `artist`, `library_artist`, `file`, `unknown` (a session known only by its events).
- Launch `attribution`: `launch_id`, `wave_session`, `reconstructed` (wave events without a session row), `inferred`
  (old history: a new launch on another context or after 30 minutes on the same device/client). A launch belongs to a
  mix by `context` (`mix:<id>`), `playlist_id` (`playlist:<id>` = the mix's playlist, the mix in effect) or
  `inferred_playlist` (≥ 60 % of an old `playlist` launch's tracks are active songs of the mix in effect).

**`/listening`** — `period {from, to, timezone}`; `totals {counted_listens` (exactly `/api/plays/summary`: measured
`listened_ms` crossing 30 s / half a short track, plus Navidrome scrobbles), `heard_events` (finish or skip ≥ 30 s,
«по событиям»), `starts, finished, early_skips, late_skips` (player attempts started in the period), `listened_ms`
(checkpoint deltas in the period), `unique_tracks, unique_artists, new_tracks, new_artists` (first heard ever in the
period), `active_days, likes, unlikes, dislikes_track, dislikes_artist, downloads}`. A track is «heard» by the counted
rule; an attempt without checkpoints (old history) — by its events. `by_day [{date, heard, listened_ms, by_source}]`,
`by_source [{source, launches, starts, heard, finished, early_skips, listened_ms}]`, `heatmap` (7 local weekdays,
Monday first, × 24 local hours, counted listens), `clients [{client, devices, starts, heard, last_at}]`, `top_tracks`
(20: name fields + `heard, listened_ms`), `top_artists` (20: `artist, heard, tracks`), `discoveries` (50 newest: name
fields + `first_heard_at, source`), `recommendations {mix {built, launched_mixes, launches, started, heard, finished,
early_skips, likes, dislikes, familiar {started, heard}, new {started, heard}, by_generator [{generator, title,
offered, started, heard, finished, early_skips, liked}], adopted}, wave {launches, restarts, by_origin, by_seed_kind,
started, heard, finished, early_skips, likes, dislikes, by_generator, adopted}}` — `offered`: a mix generator's songs
among the active ones of mixes built in the period, a wave generator's handed-out tracks (`wave_items`); `adopted`:
tracks first heard from a mix/the wave in the period that later got an own ♥ or ≥ 2 listens in self-chosen contexts
(`album, playlist, search, artist, liked, library`); `quality {events_without_context, events_without_playback,
scrobbles, legacy_finishes, inferred_launches, inferred_playlist_mixes, reconstructed_waves, first_event_at}`.

## Search and catalog
History belongs to the authenticated Hub account and is shared by browser sessions and the app's embedded PWA.
It keeps the latest 30 distinct queries (case-insensitive Unicode comparison), newest first; repeating a query
moves it to the top and updates its spelling. The PWA records debounced queries independently of cached search
results and the selected search tab. It shows 8 initially; «Показать ещё» below the list expands the rest.
History reads bypass client caches (`Cache-Control: no-store`). Saving and clearing commit before emitting
`search_history` to that user's sessions; clients refetch on that event, reconnect and resume.
The old unscoped `hm.recentSearches` localStorage key is not imported into an account: its owner is unknown.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/search/history` | → `SearchHistoryOut {queries: [string]}` (at most 30) |
| POST | `/api/search/history` | `SearchHistoryIn {query}` → `{ok}`; trimmed, 2–512 characters, otherwise `422` |
| DELETE | `/api/search/history` | → `{ok}`; clears only the current account's recent queries on every device |

| Method | Path | Response |
|---|---|---|
| GET | `/api/search?q=` | `SearchOut`: library tracks + Deezer (fallback hitmoz) artists/albums (12 each) and the first 25 tracks with ownership; `tracks_next` — offset of the next page of tracks, `null` if there is none (the hitmoz fallback has no pages) |
| GET | `/api/search/tracks?q=&offset=&limit=25` | `SearchTracksOut` `{tracks, next_offset}` — further tracks of the same search from `offset` (`tracks_next`, then `next_offset`; `null` — the end). Offsets count Deezer's results, so a page may hold fewer tracks than `limit` (repeats are dropped) and may repeat a track of an earlier page: the client drops those by `ref`. `502` if Deezer fails |
| GET | `/api/search/providers?q=&provider=&group=` | `ProviderSearchOut`: raw downloadable files from hitmoz / hitmos_fm / ytdlp and re-uploads by small channels: `yt_reupload` (plain YouTube without official copies) and `soundcloud` (for a Russian track both are regular sources of downloads and listening, ahead of YouTube Music). `provider` — one source; `group=sources` — only the regular ones, `group=reuploads` — only re-uploads (slower: SoundCloud takes seconds, so the PWA asks for the two groups separately); no `group` — all. A re-upload has `extra.reupload=true`, `extra.channel`, `extra.original_title` (its own title; `artist`/`title` are parsed from it, either order of "Artist - Title"), `extra.uncensored` — the uploader's word: `true` for "(без цензуры)"/"(правильная версия)", `false` for "(без мата)"/clean, absent otherwise. The Hub remembers every file it returns (30 days): it can be listened to (`/api/listen*`) and downloaded (`track_refs`) by its ref `file:<provider>:<provider_id>` |
| GET | `/api/catalog/artist/{ref}` | `ArtistPageOut` — `ref` like `dz:4505880` or `hm:123`; albums/singles/compilations sorted newest first |
| GET | `/api/catalog/album/{ref}` | `AlbumOut` with `tracks: TrackOut[]` (`owned`, `nd_song_id`, `downloading`, `star_pending`) |

`AlbumOut.owned_status`: `full` / `partial` / `none` (badge «в библиотеке» / «частично» / «нет»).
`TrackOut.explicit` → badge `E`. `TrackOut.cover_small_url` — the cover for lists (Deezer `cover_medium`, 250 px, ~10× fewer bytes than `cover_url`, the 1000 px `cover_xl`); null when the source has only one size, then `cover_url` is used. Same for `CatalogAlbum` (`cover_small_url`) and `CatalogArtist` (`picture_small_url`), the PWA shows `cover_url` only on the screen of the track or album itself. `TrackOut.downloading` — a download of the track is under way (not counting ones
queued only for ♥, `DownloadItemOut.star`); `star_pending` — the current user ♥ the track and a download that stars it
is under way: show the ♥ filled.

## Downloads
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/downloads` | `DownloadIn` → `DownloadBatchOut`. One of: `items[]` (artist/title or a server-known `file_ref`), `track_refs[]`, `album_ref`, `artist_ref` (+`record_types`). Optional `playlist_id` or `new_playlist_name`: tracks are added to that Navidrome playlist once imported. `star=true` (♥ on a track that isn't downloaded): the songs are starred in Navidrome for this user once imported (event `library` `{kind:"starred", song_ids}`); an owned track is starred at once; a ♥ request ignores `playlist_id`/`new_playlist_name` (the song is in «Понравившиеся» anyway). A file ref (`file:<provider>:<provider_id>`, a file the provider search showed) downloads that very file; `404` if the Hub doesn't know it. A picked file of a song the library has from another file (an uncensored re-upload of a censored copy) is not «already owned»: it is downloaded next to the old version (`… [v2]`), with `star` the ♥ moves from the old version to it (event `library` `{kind:"unstarred", song_ids}`), and from then on the catalog track (`nd_song_id`, listening) is the newest version. A track ref this user is downloading already (an active item with the same `track_ref`) gets no second download unless `force`: ♥ makes that download star the song, a plain request makes a download queued only for ♥ a visible one (`star` → false; event `download`); such refs count in `queued`. Refs are expanded by a background job: then `expanding=true` and `queued` counts only `items[]` |
| GET | `/api/downloads?status=&limit=100` | `DownloadItemOut[]` (newest first; `status` may be a comma list; `nd_song_id` once scanned, `playlist_id` = the batch's target playlist, `track_ref` = the catalog track it came from, or the file ref of a picked file, `star` = queued only for ♥, by an import (`import_id`) or for a «Микс дня» and not asked for explicitly: the Hub's own business — clients don't show it as a download, don't notify about it, the app doesn't download it to the phone; it turns false if the track is then downloaded explicitly (an import's download then also goes first in the queue), the song is still starred, and when a download queued only for ♥ fails (`failed`/`needs_review`): from then on it is the user's download (listed, notified) that still stars the song once a file is picked; `liked` = queued with ♥, the song is starred once imported) |
| GET | `/api/downloads/page?filter=all&limit=50&before_id=` | `{items: DownloadItemOut[], next_cursor: number \| null, counts: {all, active, problems, finished}}`. `filter`: `all`, `active`, `problems`; `limit` 1–200. Only the current user's visible (`star=false`) history, filter applied before limit; descending ID, exclusive `before_id`. Counts cover the whole visible history independently of filter/cursor. `next_cursor=null` ends pagination |
| GET | `/api/downloads/{id}` | `DownloadItemOut` with `candidates` |
| POST | `/api/downloads/items/{id}/retry` | `RetryIn` (`candidate_index` from `candidates`, or a page `url`) → `DownloadItemOut` |

| POST | `/api/downloads/items/{id}/cancel` | → `DownloadItemOut` |
| POST | `/api/downloads/clear` | removes finished (`done`/`cancelled`) items from the list → `{removed}`. Items still needed as playlist order anchors are kept until earlier pending additions are resolved |

For a single catalog-track heart, a client that sees `catalog_favorite_download_v1` in `/healthz` can send
`star=true`, one `track_refs` entry, `favorite_event_id`, `favorite_ts`, `favorite_client`, and
`favorite_context`. The event id is shared with the listening event; retrying it returns the original batch
without queuing a second download. A later unlike or causal rollback cancels the quiet heart download, while an
independently requested download remains queued. These fields are optional for existing clients.

`GET /api/downloads` also accepts `ref=…`: exact catalog/file reference, filtered within the current account **before** applying `limit` (1–500). This lets the player find the latest download for one track without scanning unrelated jobs.

Capability `server_file_downloads_v1` advertises the safe candidate contract. Clients must use
`track_refs: ["file:<provider>:<id>"]` or `items[].file_ref` after a provider search.
Client-supplied `items[].candidate` is rejected with `422`, not silently trusted or ignored.
Assistant choices of kind `candidate` likewise send `file_ref`, never a Candidate object.
Unknown/expired refs require a fresh search. Older clients sending Candidate JSON must be updated.
External media requests validate public destinations before connecting, pin DNS results and revalidate redirects.
Download filenames are generated by the server, independently of provider IDs.

Playlist additions keep the request order, including songs already in the library. Active downloads wait for earlier
ones, but `needs_review` does not block later ready songs. The pending row and subsequent applied-song anchors are
kept so a reviewed song can be inserted back into its requested place after retry, including across restarts.
Pending additions are also processed at startup and every 30 seconds to recover old blocked batches and retry
Navidrome failures without requiring another download or scan. Separate batches follow submission order.

Statuses: `queued, searching, downloading, tagging, importing, done, failed, needs_review, cancelled`.
`needs_review`: show `candidates` so the user can pick one (retry with `candidate_index`) or paste a URL
(hitmoz / hitmos.fm song page, YouTube or SoundCloud track).

An explicit Russian catalog track takes only a copy known to be uncensored first: a file uploaded before 2026-03-01
that isn't a clean version (not an official YouTube Music copy: labels replace their audio in place), then a
re-upload titled "(без цензуры)" or uploaded before that date (`sources.reuploads`, its last few go to `candidates`).
A possibly censored copy is the last resort and gets the warning «Возможна цензура: копий без цензуры не нашлось».
A picked re-upload (a server-known file ref whose remembered metadata has `extra.reupload`) is looked up in Deezer for its tags.

## Listening before download
| Method | Path | Response |
|---|---|---|
| GET | `/api/listen?ref=` | `ListenOut` — `ref` of a catalog track (`dz:…`, `hm:…`), of a song only the library has (`nd:<song>`, a mix's track: its file, `kind` `library`; `404` if the library has no such song) or of a file the provider search showed (`file:<provider>:<provider_id>`: that file itself, no pick; `404` «повторите поиск» if unknown, `502` if it doesn't open). The remembered pick, or a new one: the way a download picks (Cyrillic original, source order, ranking) with a shorter search (4 results) and no track pages; all sources are searched at once, the first in source order that has the track wins. The file is not opened here: the stream opens it, and a file that doesn't open gives way to the next candidate (then to a new pick whose files are opened first). `404` if nothing at all. Ready picks answer at once |
| POST | `/api/listen/prefetch` | `{refs: [...]}` → `{queued}` — the catalog tracks (and files) on the user's screen, top first (≤ 100); replaces the user's previous list (tracks that scrolled away are dropped). The Hub picks their files in the background (3 at a time, each opened once) and remembers them; this work yields to taps and downloads (hitmoz requests, yt-dlp slots). `queued` — how many were not ready yet |
| GET | `/api/listen/stream?ref=&q=` | audio, `Range` → `206` (unsatisfiable → `416`); the library file, or the server copy of the picked file: the Hub downloads the whole file from the source (googlevideo links are bound to the Hub's IP) to `staging/listen`, resuming a connection the source drops, and serves the request from the copy as it grows (a range more than 2 MB past what it has comes straight from the source); for a source without plain HTTP audio (SoundCloud: HLS) — a copy the Hub downloads first (the prefetch downloads it in the background); copies are kept ~3 h, ≤ 500 MB. A preview is proxied. A request from the start (no `Range` or `bytes=0-`) counts as a listen (an economy import downloads the track) unless it carries `X-HomeMusic-Prefetch: 1` — the app loading the start of the next tracks of its queue ahead; the server copy of such a track downloads only while no copy of a played track does. **`q=mobile`** — the light variant for a metered network (`app/light.py`; without `q`, or with any other value, the stream is as above, so older apps are unaffected): a file above 192 kbps (mp3 320, flac, heavy aac) is answered with its Ogg Opus 160 kbps VBR copy (`audio/ogg`, ~half the bytes; `Content-Length` and `Range` as for any file, the copy is bit-exact so a re-made one has the same bytes), a file at or below 192 kbps (opus ~141, YouTube m4a ~128) as it is. Works for every ref: a catalog track (the copy is made from its server copy), a library song (`nd:<song>`, also an owned catalog track) or a file ref. The copies are made ahead (a mix's songs when it syncs, a wave's tracks when handed out) and kept on disk (`staging/listen-light`, ≤ 2.5 GB, least recently used out, ≤ 3 ffmpeg at a time, background ones at low priority and yielding to users' requests). A request that finds no copy ready waits ~1.5 s (a track loaded ahead, `X-HomeMusic-Prefetch: 1`, up to 25 s), then serves the original while the copy is finished in the background. Header **`X-HomeMusic-Variant`** (only with `q=mobile`): `light` — the Opus copy; `original` — the file as it is because it is light already (always the same for that file); `original-pending` — the copy is not ready (or ffmpeg failed): the original, which a client must not keep in the cache of the light variant. For 10 min after such an answer the same source keeps getting the original (one cache entry never holds bytes of both variants) |

`ListenOut.kind`: `full` (the file a download takes: `provider`, `bitrate_kbps`, `page_url`), `preview` (no source has it —
Deezer's 30 s preview, show «превью 30 сек»), `library` (already owned: the own file; `nd_song_id` once Navidrome has scanned it).
`GET /api/listen/stream?ref=…&full=1` requires a full recording: a remembered/cached preview is re-resolved,
the regular sources are followed by `sources.download_fallback`, and a preview-only result returns `503`
with a Russian explanation instead of preview audio. Without `full`, browser preview behavior is preserved.
Successful stream responses include `X-HomeMusic-Listen-Kind: full|library|preview`, independently of `q`.
After any body bytes have been sent, upstream continuation requires an exact `206` range and unchanged
candidate, source URL and strong ETag. Ignored ranges, missing/changed validators and alternate files terminate
the response instead of splicing incompatible bytes; the player must explicitly restart playback.
`GET /api/listen?ref=…&full=1` applies the same full-recording policy to source metadata; it cannot describe a cached preview as the full-only player's selected source.
`ListenOut.recording_binding` is an opaque, account-owned observation ID; use the returned `stream_url`
unchanged. Metadata/prefetch alone never authorizes a wrong-recording report. If source fallback changes the file,
`GET /api/recording-feedback/current?ref=…` returns the binding of the actually served choice.
Owned-file lookup precedes optional album/name enrichment. Playback bounds optional album lookup to 1.5 s,
original-name lookup to 3 s, each regular search to 6 s, fallback search to 8 s, and file probing to 25 s
(a YouTube file: ~8 s extraction, then its URL answers only after the ad before the video could be skipped —
the Hub waits up to 40 s for that, a longer ad counts as a file that does not open);
these are stage limits, not a guaranteed total startup time. Download matching itself keeps its normal budget.
Source order (`config.yaml`, `sources`): a Russian track (Cyrillic, a CIS release, or `lang=ru`) — hitmoz → hitmos_fm →
SoundCloud → YouTube re-uploads → YouTube Music (official copies are often edited since the 2026 law); other tracks —
YouTube Music → hitmoz. Files a source uploaded on or after its cutoff (`sources.uploaded_before`: SoundCloud — 2026-05-01)
are not taken by listening and downloads; the provider search (Files tab) still shows them.
Picks are remembered per `ref` for 30 days (a preview for a day) and survive restarts; only the upstream link is renewed.
A pick from a source that a changed order now puts behind one not asked at the time is picked again.
A preview picked while a source failed (network, site down) is not remembered and is looked at again after a minute;
with no preview either, `502` instead of `404`.
After the first listen the Hub checks the file on its track page (hitmoz: Explicit/Clean, version); a download of that `ref`
(`track_refs`, `album_ref`…) tries the listened file first if the check passed. A file that fails it (e.g. the clean copy of an
explicit track) is replaced, for the next listen, by a download-grade pick. Importing the track drops the pick (not a file's: it
stays that file).

## Lyrics

`POST /api/lyrics` requires authentication. Supply one canonical `ref` or `nd_song_id`, or local metadata `{title, artist, album?, duration_s?}`. Canonical requests resolve server-side metadata; submitted names cannot override a different canonical recording. Local paths and streaming URLs are not accepted.

The response distinguishes `synced`, `plain`, `instrumental`, and `unavailable`; `lines` contain text and optional `time_s`. Only timed lines support seeking. LRCLIB results must match full title, artist, album when provided, and duration within two seconds. Version qualifiers are retained. Insufficient evidence or unavailable lyrics is not an audio playback error. Provider requests and response sizes are bounded; the process-local cache keeps at most 128 entries (24 hours for successful results, five minutes for unavailable results).

## Smart playlists

All definitions and evaluations belong to the authenticated account; another user's ID returns `404`.

| Method | Path | Body → Response |
|---|---|---|
| GET / POST | `/api/smart-playlists` | List definitions / create `{name, definition}` (`201`) |
| GET / PUT / DELETE | `/api/smart-playlists/{id}` | Read / replace `{name, definition}` / delete `{ok}` |
| POST | `/api/smart-playlists/preview` | `Definition` → `Evaluation` |
| POST | `/api/smart-playlists/{id}/evaluate` | Re-evaluate saved rules → `Evaluation` |

`Definition`: `{match: "all"|"any", rules: [...], sort, descending, limit}`. One to 32 rules; limit 1–2000; sort `title`, `artist`, `year`, `added`, `last_listened`, or `random`. Rules: `liked {value}`, `not_listened {days, include_unheard}`, `unheard`, `genre {value}`, `year {minimum?, maximum?}`, `recently_added {days}`. Unknown fields and invalid ranges return `422`; no SQL expressions are accepted.

`Evaluation`: `{count, tracks: [{nd_song_id, title, artist, album, duration_s}]}`; count precedes the limit. Evaluation uses the user's currently visible Navidrome library, favorites and listening history, not recommendations. “Not listened recently” excludes unheard tracks unless requested. Unknown last-listened time with a positive Navidrome play count is not proof of either “unheard” or “older than N days”. Results are recalculated on preview/play; random order is recalculated too.

## Shared playback and device control

All `/api/playback` routes use the authenticated user's isolated session. `Snapshot` is `{queue, index, occurrence, position, playing, volume, repeat, shuffle}`. Queue ≤1000 canonical tracks: `{ref, nd_song_id, title, artist, album, duration_s}`. URLs, local paths, credentials, and arbitrary extra fields are rejected. `repeat` is `off|all|one`; `shuffle` is boolean. Position is seconds; volume is 0–1. Index and occurrence preserve duplicate entries.

| Method | Path | Request |
|---|---|---|
| POST | `/api/playback/heartbeat` | `{device_id, name, kind: "desktop"|"web", state?, revision?}` |
| POST | `/api/playback/claim` | `{device_id, state, revision}`; only no owner or the same owner |
| POST | `/api/playback/handoff` | `{device_id, target, revision}`; explicit transfer/recovery |
| POST | `/api/playback/command` | `{device_id, target, revision, kind, value?}`; play/pause/next/prev/seek/volume |
| POST | `/api/playback/ack` | `{device_id, command_id, ok, error?, state?}` |

Responses are a `View`: `{device_id, devices, owner, revision, state, handoff, commands, error}`. Commands are bounded, expiring and delivered only to their target; acknowledgment is idempotent. Only the owner publishes state with the current revision. Position-only heartbeats update telemetry without invalidating a controller's revision; queue, mode, volume and playing changes still reject stale actions.

Handoff is prepare-paused → source stop acknowledgment with final position → target start acknowledgment → ownership commit. Failure never automatically resumes the source. Recovery from an owner offline for over 15 seconds is explicit and still requires a ready, online target. Clients heartbeat every two seconds and pause shared playback after 12 seconds without successful contact; reconnect does not automatically resume audio. Explicit local-only mode disconnects from session control and allows offline playback without that shared-session fence. Browser autoplay denial is visible, not a successful transfer.

## Listening data (mixes, «Моя волна»: MIXES.md 3)
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/plays/events` | `{events: PlayEventIn[], expected_user_id?: number}` (≤ 500); mismatch with the authenticated user → `409` → `PlayEventsOut` `{accepted, duplicates, rejected: [{index, event_id, detail}]}`. Always `200` for a well-formed body: a malformed event is rejected alone; clients retain rejected events with diagnostics; an `event_id` the user sent before counts as `duplicates` (send a batch again freely). A `start` with a catalog `ref` is a listen of it (an economy import downloads the track: the app may play it from its precache, which never asked the stream as a listen) |
| GET | `/api/plays/summary?start=YYYY-MM-DD&end=YYYY-MM-DD&timezone=Europe/Moscow` | Inclusive calendar dates in an IANA timezone → measured attempts, cumulative milliseconds, counted listens, scrobble and legacy evidence, track snapshots and coverage. Checkpoint deltas belong to the day they were recorded; `first_observed_at` is only the earliest stored evidence. Invalid period/timezone → `422` |
| GET | `/api/dislikes` | `DislikeOut[]` `{id, ref, nd_song_id, artist_ref, artist, title, source, created_at}`, newest first; `source`: `app`, `pwa`, `yandex_import` |
| POST | `/api/dislikes` | `DislikeIn` `{ref?, nd_song_id?, artist_ref?, artist?, title?, context?, client?}` → `DislikeOut`. A track (`ref` and/or `nd_song_id`) **or** an artist (`artist_ref`), else `422`; the same target again is the same row (missing ids filled in). A track also gets a `dislike` event |
| DELETE | `/api/dislikes?ref=&nd_song_id=&artist_ref=` | → `{removed}`; nothing named → `400`. A track also gets an `undislike` event |
| DELETE | `/api/dislikes/{id}` | → `{removed: 1}`; another user's or unknown → `404` |

`PlayEventIn` — the contract of every player (the app, T3; the PWA's own player):

| Field | |
|---|---|
| `event_id` | string ≤ 64, unique per user (a UUID): the key that makes resending harmless |
| `ts` | when it happened, ISO 8601 with a zone (`2026-10-22T12:00:00Z`; epoch seconds/ms are accepted too). More than 1 h in the future or older than three calendar years → the time it came in. Admitted future events do not affect state/profile before their timestamp |
| `kind` | `start` (sound began), `progress`, `interrupted`, `error`, `skip`, `finish`, `like`, `unlike`, `dislike`, `undislike` |
| `ref` / `nd_song_id` | the track: catalog ref (`dz:…`, `hm:…`, `file:…`) and/or Navidrome song id — at least one, else rejected. The Hub fills in the other where it knows it (the library file's Deezer id, the download or import that brought it) |
| `playback_id`, `device_id`, `listened_ms` | Optional pair of IDs identifies one attempt on one installation; cumulative measured milliseconds are never summed across checkpoints. `progress`/`interrupted`/`error` require all three. `listening_checkpoints_v1` advertises support |
| `track_metadata` | Optional snapshot of title, album, artist credit and explicit artists with IDs, duration, ISRC, year and genre. Hub fills missing values from library or catalog cache and records field-level provenance. `primary_nd_artist_id` preserves the primary Navidrome artist ID independently of a possibly composite credit. Unknown fields remain unknown |
| `position_s`, `duration_s` | seconds (0…86400): where the player was and the track's length (for `skip`/`finish`) |
| `context` | ≤ 128: `mix:<id>`, `wave:<session>`, `smart:<session>` (a recommendation of the smart shuffle), `album`, `playlist`, `search`, `liked`, `artist`, `queue`, `other` |
| `client` | 1…32 chars: `app/<versionName>`, `pwa` (`navidrome` is the Hub's own import) |
| `target_type` | `track` (default), `album`, `artist`; start/skip/finish require track; dislikes support track/artist |
| `nd_album_id`, `nd_artist_id` | optional ND identifier matching `target_type`; `ref` is interpreted within that type. At least ref or the matching ND identifier is required; identifiers of another type are rejected |
| `target_data` | optional metadata snapshot (JSON object ≤8192 characters, e.g. name, artist, title); missing metadata does not discard a decision |
| `launch_id`, `launch_origin` | optional, analytics only (INSIGHTS.md 6.5; the taste profile, mixes and waves never read them): `launch_id` 1…64 — the same on every event of one launched list (a new one for every new list or queue; `wave:<session_id>` for a wave), `launch_origin` ≤ 32, `^[a-z][a-z0-9_]*$` — where it was launched (below); an unknown code is stored as sent, a malformed one rejects the event. Clients send them when `/healthz` has `launch_events_v1` |
| `reverts_event_id` | optional id ≤64 of the exact failed like/unlike decision. A compensation removes that decision, including when delivered first; with `favorite_compensation_chains_v1` it may also retract another compensation, restoring the original decision/date. Only same-user canonical typed targets match. Parallel active retractions all need retraction; unknown targets/cycles are inert. Chains cannot override a newer independent decision |

`launch_origin` codes: `mix_page` («Слушать всё» on a mix's page), `mix_page_track` (a tap on a mix's track),
`mix_card` (▶ of the «Микс дня» card), `nd_playlist` (a Navidrome playlist in the app, «Микс дня» too), `album`,
`artist`, `playlist`, `liked`, `search`, `queue`, `library` (a tap or «Слушать всё» on that screen), `wave` (a wave's
queue). The PWA sends them since 2026-10-31 (its own player and the `playQueue` items of the app bridge).

Favorite state folds the complete journal in `(ts, received_at, id)` order across verified aliases. Repeated likes
keep the start of their continuous series; unlike is neutral. Events duplicated by older players do not add another
contribution. State is permanent; the current v1 profile still uses its calendar scoring window. Artist actions
through all `/api/dislikes` routes, including deletion by id, are journaled too.

`finish` — the track played to its end by itself, or the user left it after ≥ 90 % of its length; `skip` — left
earlier (next, another track, the player closed); a skip «before 30 s» is `skip` with `position_s < 30`. Pause and
seeking are no events. `dislike`/`undislike` events also set/clear the track's row of `/api/dislikes` (`source` `app`
for `app/…` clients). Every hour the Hub adds the user's Navidrome scrobbles (other clients) as `finish` with client
`navidrome`; one within 5 min + the track's length of an app/PWA `start`/`skip`/`finish` of the same song is the same
listen and marked as superseded, whichever came first; the raw scrobble remains stored.

The summary counts an attempt when measured listening reaches 30 seconds, or half the track for tracks shorter
than one minute. A non-superseded Navidrome scrobble is separately labelled threshold evidence and contributes one
count, never measured milliseconds. Old clients and old scrobbles do not supply precise milliseconds; legacy finish
is reported separately. Stored events are not deleted after three years: the three-year rule above is an intake
clock-sanity check, not a retention period. Checkpoints are cumulative, so aggregate the maximum per `(user, device,
playback_id)`, never their sum. On a crash the last unsaved interval can be missing.

### Planned compatible extension: typed favorite events (not implemented)

The current `PlayEventIn`, `/api/star`, `/api/starred` and `/healthz` contracts above/below remain in force.
The taste v2 target contract (MIXES.md, taste v2) plans additive fields: `target_type` (`track` by default,
`album`, `artist`), matching Navidrome ids, bounded `target_data` metadata and `reverts_event_id` for a causal
compensation. `ref` is interpreted by target type; `start`/`skip`/`finish` remain track-only. Existing track
payloads, per-user `event_id` deduplication, batch limits and `accepted`/`duplicates`/`rejected` semantics stay valid;
the same event schema will apply to `WaveNextIn.played`.

The planned `/api/star` extension will carry the decision id, timestamp, client and context; `/api/starred` will
add artist ids. A future `/healthz` capability will advertise support for the new schema. Until that capability
is present, new clients must retain unsupported non-track events for later delivery while continuing to send
compatible track batches. No capability name or new field is available to clients yet; the implementation stage
must publish the exact schema together with server support. This extension does not itself enable v2 scoring.

## «Микс дня» and the taste (MIXES.md 6)
| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/mixes/today` | `MixTodayOut` `{mix: MixOut \| null, building, settings: MixSettingsOut, onboarding}`. `mix` — the user's current mix (lives until the next one); `null` — none yet. `building` — a build is queued or running. `onboarding` — no history, no picked artists and no mix: show «Что вы слушаете?» |
| GET | `/api/mixes/{id}` | `MixOut`; another user's (not admin) or unknown → `404` |
| GET | `/api/mixes/settings` | `MixSettingsOut` `{mode, mix_hour, timezone, next_build_at, nd_playlist_id, artists}` |
| PUT | `/api/mixes/settings` | `MixSettingsIn` `{mode?: normal\|favorites\|discover, mix_hour?: 0…23}` → `MixSettingsOut` (else `422`). A new mode makes the next mix, a new hour — from the next day |
| POST | `/api/mixes/rebuild` | `{user_id?}` → `{queued}` — build the mix again now (admin only, else `403`; `user_id` — another user's). `queued: false` — a build is under way already. The new mix replaces the current one when ready |
| GET | `/api/taste/onboarding?q=` | `TasteOnboardingOut` `{needed, selected: [{ref, name}], suggestions: CatalogArtist[]}` — artists to pick: the library's most present ones and Deezer's chart, or found by `q` |
| POST | `/api/taste/artists` | `{artist_refs: ["dz:…"]}` (1…20) → `MixSettingsOut`; not a Deezer artist → `400`, unknown → `404`. The user's first pick queues the first build at once |

`MixOut`: `{id, title, description, mode, mode_title, created_at, nd_playlist_id, playlist_status, playlist_error, ready, total, tracks, generators}`.
`tracks` — the 50 songs in order, `MixTrackOut` = `TrackOut` (`ref` `dz:…`; a song only the library has: `source`
`nd`, `ref` `nd:<song>`; `owned`/`nd_song_id` — in the library already, play it by `nd_song_id`; `downloading`) +
`{position, generator, generator_title, generators, familiar, score, sim}`: `generator` — the strongest source of the
song (`favorites` «Любимое», `stash` «Тайник», `ytm_radio`, `lastfm_similar`, `deezer_radio`, `yandex_similar`,
`similar_artists`, `new_releases`, `yandex_wave`), `familiar` — familiar to the user or new, `sim` — closeness to
the taste by sound (`null` without the embedder). `ready` — songs in the library (the rest download quietly: not in
`/api/downloads` lists as visible, `star: true`). `generators` — per generator `{tracks, used, seconds, error?, off?}`.
`nd_playlist_id` — the user's Navidrome playlist «Микс дня» (the same id every day; its songs are replaced).
Events of songs played from a mix: `context` `mix:<id>`.
`playlist_status`: `pending`, `error`, or `published`; `playlist_error` is nullable. The local mix survives a
Navidrome outage; publication retries through `mix_sync` without holding the local database write transaction.
An ambiguous playlist creation is reconciled against its temporary remote name, never blindly duplicated.

## «Моя волна» (MIXES.md 7)
An endless stream for the user's taste: the Hub hands out 5 tracks at a time and picks again after every action.
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/wave/start` | `WaveStartIn` `{seed?: {ref? \| nd_song_id? \| artist_ref? \| nd_artist_id?}, mode?: normal\|favorites\|discover, origin?, client?, restart_of?}` → `WaveOut`. No `seed` — the user's own wave from the taste profile (no history and no picked artists → `409`); `seed.ref` (`dz:…` or `nd:<song>`) / `seed.nd_song_id` — «Волна по треку»: that track is `tracks[0]`; `seed.artist_ref` (`dz:…`) — a wave by the artist; `seed.nd_artist_id` — a wave by a Navidrome artist (the app's «Радиостанции»): the artist's library songs (generator `artist_songs`) with the radios of its Deezer artist (found by a song's catalog record or the exact name; none — the radios of its songs). The artist of a wave by an artist plays at most once in 3 tracks. Unknown seed → `404`, none of them → `422`. `mode` — the familiar share as a mix's (default: the user's mix mode). May take up to ~10 s (the sources are asked; the profile is made) |
| POST | `/api/wave/{session_id}/next` | `WaveNextIn` `{played: PlayEventIn[] (≤ 500), queued: [ref] (≤ 50)}` → `WaveOut` with `recorded` (`PlayEventsOut` of `played`). Unknown session, another user's, or not asked for 24 h → `404` «Волна закончилась» (start a new one) |

`origin`, `client`, `restart_of` — optional, analytics only (INSIGHTS.md 6.3; the wave never reads them; clients send
them when `/healthz` has `wave_origin_v1`): `origin` ≤ 32, `^[a-z][a-z0-9_]*$` (else `422`; an unknown code is stored
as sent), `client` 1…32 (`pwa`, `pwa-app` — the PWA inside the app, `app/<versionName>`), `restart_of` ≤ 32 — the
session the app makes again after a `404`. The Hub keeps them with the session, with the body as it came
(`request_json`) and the taste model of the start.

| `origin` | Where it was started |
|---|---|
| `home_card` | the «Моя волна» card of «Главная» (the app's native one, `/home` in a browser) |
| `catalog_card` | the «Моя волна» card of the PWA's catalog (`/search` without a query; the app's «Хаб» tab) |
| `track_menu` | «Волна по треку» in a track's menu (lists, album, mix, search) |
| `artist_menu` | «Волна по исполнителю» in a track's menu |
| `player` | from a player: «Моя волна по треку» on `/player`, the menu of the mini player and the queue, the app's player menu |
| `artist_page` | «Волна по исполнителю» on an artist's page |
| `radio` | the app's «Радиостанции», the PWA's library artists (`nd_artist_id`) |
| `search_file` | the ⋯ menu of a Files search result (`file:<provider>:<id>`) |
| `restart` | the app made the session again after a `404` (with `restart_of`) |

A session's row is kept for the history: one not asked for 7 days has its state cleared to a summary (it answered
`404` after 24 h anyway); what it handed out stays in `wave_items` (MIXES.md 7.1).

`seed.ref` also accepts `file:<provider>:<id>` from the Files search. The Hub reads the candidate it remembered
when returning search results; arbitrary URLs or unknown/expired file refs are rejected with `404`
«Файл не найден: повторите поиск». The exact file is `tracks[0]` (`source: "file"`, `generator: "seed"`), with
its original ref, artist and title; radios use that metadata even when no catalog record exists. The file is not
substituted with a library or catalog version. `by_artist` is supported for `dz:` and `nd:` tracks as before.

`WaveOut`: `{session_id, title, mode, seed, tracks, pending, recorded, preferences, preferences_status, preparation}`.
`title` — «Моя волна», «Волна по треку «<название>»», «Волна: <исполнитель>»; `seed` —
`{ref, artist, title}` or `{artist_ref?, artist, nd_artist_id?}`, `null` for the user's own.
`tracks` — up to 5 `WaveTrackOut` = `TrackOut` + `{generator, generator_title, familiar, sim, explanation: string[]}`.
Owned songs play by `nd_song_id`; other tracks use `/api/listen/stream?ref=…&full=1` (including resolve/preload).
`generator` — as a mix's, plus `seed` and `session_radio`. `pending` includes unfinished source/file preparation;
empty `tracks` with `pending` means continue the **same** session through `next`, not a new `start`.
Empty without `pending` means exhausted. A pending explicit seed is retained across restart, never replaced
with another song; an unavailable full seed returns `409`.

Both start and next accept optional `preferences: {energy: any|calm|energetic, vocals: any|vocal|instrumental,
discovery: balanced|familiar|new}`. Defaults: `any/any/balanced`. Omitted next preferences preserve the session's
values. Applying them replaces upcoming tracks only; clients retain the current occurrence and position.
Energy/vocals contrast actual audio vectors with text prompts; missing model/vectors are neutral and reported
in nullable `preferences_status`. Discovery changes the familiar share; permanent taste settings are untouched.
The lazy text model has a bounded 20-second preparation window. On CPU, background audio inference releases
the model between songs, so interactive text controls do not queue behind a multi-minute audio batch;
GPU audio batching is unchanged.

`preparation: {ready, pending, unavailable}` counts the bounded reserve (up to 10 candidates). Ready means the
complete file exists, has a valid probed duration and matches the expected duration tolerance, not merely that
a provider returned a URL. Preview/truncated files are excluded; transient failures remain pending. Existing
bounded prefetch workers and stream copies are reused. Preparing/rejecting audio does not emit a taste event.

**How a player keeps a wave going** (the PWA's mini player; the app's player, T7):
- Play `tracks` in order with `context` `wave:<session_id>` in their events (the usual `/api/plays/events` events, sent
  as always).
- Call `next` when a wave track starts (after its `start` event), after a ♥ or «Не нравится» of a wave track, and in
  any case when ≤ 2 wave tracks are left ahead. `played` — the player's events of the wave's tracks since the last
  call (the same objects, the same `event_id`, as sent to `/api/plays/events`: the Hub stores an `event_id` once and
  gives the stored event the wave's context); a call that failed sends its events again with the next one.
  `queued` — the `ref`s (as in `tracks`) of the wave's tracks still ahead, not started, in order.
- **The answer replaces `queued`**: drop the wave's tracks after the current one and put `tracks` there. A queued
  track that still fits comes back (a small bonus keeps it from being swapped for a hair better one), so the next
  track rarely changes without a reason. An empty `queued` (none ahead) just appends.
- Nothing ahead and no answer (offline): the queue ends; the session waits up to 24 h.

What the Hub does with `played`: `start` makes the track played (never handed out again in this session; the last
50 played count for «an artist at most once in 8 tracks»); `skip` with `position_s < 30` or `dislike` — the artist goes
down in this session and so does what sounds like the track (the embedder's vectors); `finish` or `like` — its
YouTube Music radio joins the pool (`session_radio`), what sounds like it goes up; `finish` of a track the library
lacks — a quiet download (`query_json.wave`: not shown in «Загрузки», as a mix's). «Не нравится» set anywhere
(`/api/dislikes`) counts at the next call. A ♥ downloads the track the usual way (the client's ♥ download).

## Recommendation quality, controls and releases

All routes require authentication and operate on the current account. Cross-account contexts/IDs return `404`.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/recommendations/quality?days=30` | `days`: 7, 30 or 90. `{days,since,until,attempts,starts,eligible_plays,completed,early_skips,playback_errors,interrupted,new_track_plays,new_track_likes,repeats,completion_rate,early_skip_rate,new_track_like_rate,repeat_rate,sources,limitations,metric_version,provenance,policy_cohorts,comparison,recordings}` |
| GET | `/api/recommendations/explain?context=wave:…&ref=…&nd_song_id=…` | `{reasons: string[]}` from the owned recorded mix/wave/smart decision. Supply ref and/or song ID; both must identify that item. Missing historical snapshot returns an empty list. |
| GET | `/api/recommendations/pauses` | `{items:[{id,action,label,expires_at,ref,nd_song_id,artist}]}`; integer IDs, UTC expiry, active entries only. |
| POST | `/api/recommendations/pauses` | `{action:today|week|less_artist,ref?,nd_song_id?}` → `201`, one item. Identity/artist are resolved server-side; repeated equivalent active action returns the existing item. |
| DELETE | `/api/recommendations/pauses/{id}` | `204`; immediate reversal. |
| GET | `/api/releases?period=week&unseen=false` | `period`: week/month (7/30 calendar days). `{period,generated_at,partial,errors,items:[{ref,title,artist,artist_ref,cover_url,release_date,track_count,seen}]}`. Track count may be null. |
| PUT | `/api/releases/seen` | `{ref,seen}` → same shape. Marking validates the catalog album; unmarking remains available during catalog outages. |
| GET | `/api/recording-feedback/current?ref=…` | `{ref,binding}` for this account's latest actually served full-source recording, within 24 hours. `404` if absent; library files need recoverable provider provenance |
| POST | `/api/recording-feedback` | `{ref,binding}` → `{id,ref,message}`. Excludes that provider file for this user and track, not the musical taste. `404` for another account/ref or unknown binding; `409` for stale, unserved, changed or unidentifiable recordings. Repeating the same report is idempotent |
| DELETE | `/api/recording-feedback/{id}` | `204`, revoke an owned report; `404` otherwise. Does not delete any audio or alter other users' choices |

Rates are fractions, null without a denominator. Only attributable mix/wave/smart playback attempts count,
not recommendations offered. Errors, interruptions and unfinished attempts are excluded from taste outcomes.
Legacy events without `playback_id` use bounded grouping and are explicitly disclosed in `limitations`.
`sources` items: `{id,title,attempts,completed,early_skips,playback_errors,new_track_likes,multiplier,learning_samples}`.
Learning always uses 90 days, at least 20 eligible outcomes per source, shrinkage and multipliers in `[0.75,1.25]`;
insufficient evidence keeps 1.0. Cached per account for at most 60 seconds; applies to new mix/wave/smart choices.
`explanation: string[]` is also included in mix/wave/smart tracks; it reflects saved score components and session
preferences, never a fresh catalog/profile guess.

`comparison.current` and `.previous` are adjacent, non-overlapping windows of the same duration, each with
`since`, `until`, `attempts`, `eligible_plays`, `completed`, `early_skips`, both rates and `policy_cohorts`.
Cohorts use only the policy version saved with the recommendation decision (`unknown` stays unknown).
`comparison.limitations` explicitly warns that this is observational, not a randomized A/B result.
Shadow/replay do not fabricate listening outcomes for an alternative policy.

`recordings` includes `metric_version`, `since`, `until`, `observations`, `known`, `full`, `not_full`, `unknown`,
`rate`, `provenance[]`, `limitations[]`. The rate denominator is only verified full files or confirmed previews;
an HTTP success, metadata or a playback start is not proof of completeness. Repeated binding requests within
five minutes coalesce. These observations cover this account's playback sources, not only recommendations;
old events without observations are not reconstructed. Duration/decoding checks do not prove recording identity.

Wrong-recording feedback is separate from dislike/skip. Clients stop the current attempt as `interrupted`,
offer another recording and expose undo; catalog/library aliases of the rejected source are also excluded.

`today` expires at the next midnight in server `TIMEZONE`, `week` after 7 days, `less_artist` after 30 days.
Track pauses exclude future recommendation selection; artist pauses demote it to 0.25, including negative scores.
Neither changes likes/dislikes or prevents explicit playback.

Release targets come from onboarding and the current favorite ledger. Catalog work is bounded (20 artists,
4 concurrent requests, 15-second overall deadline); caps/outages set `partial` and return available results.
Future/invalid dates are excluded; album versions retain distinct identities. Opening a release, or marking it
seen, never likes/downloads it. Use the existing catalog album, queue and download APIs explicitly.

## «Умное перемешивание» (MIXES.md 8)
A playlist or «Понравившиеся» in the shuffle with recommendations "in the mood of the list" between its songs (Spotify's
Smart Shuffle). The Hub keeps the order and the picks; a client shows them and plays. Capability `smart_shuffle_v1` in
`/healthz`. The playlist itself is never changed.
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/smart-shuffle/start` | `SmartStartIn` `{context, songs, current?, title?, origin?, client?}` → `SmartOut`. `context` — `liked` or `playlist:<Navidrome id>` (else `422`). `songs` (1…5000) — the list's songs in its order (a playlist: new ones at the end; «Понравившиеся»: the newest first): a Navidrome song id, or a catalog ref (it has a colon) for a ♥ that has no file yet. `current` — the song tapped or playing now: it is first in `own`. `title` — the playlist's name (kept for the analytics), `origin` ≤ 32 `^[a-z][a-z0-9_]*$` (`playlist_page`, `liked_page`, `player`), `client` ≤ 32. May take up to ~10 s (the sources are asked) |
| POST | `/api/smart-shuffle/{id}/next` | `SmartNextIn` `{played: PlayEventIn[] (≤ 500), at?}` → `SmartOut` with `recorded` and `own: []`. Unknown session, another user's, or not asked for 24 h → `404` «Умное перемешивание закончилось» |
| POST | `/api/smart-shuffle/{id}/hide` | `{ref? \| nd_song_id?}` → `{ok}`. «−»: the track is not recommended in this context («Понравившиеся» or this playlist) any more — by its «artist — title» too, not only its id; it stays everywhere else (this is not `/api/dislikes`). The track must be one this session recommended (`404`) |
| POST | `/api/smart-shuffle/{id}/add` | `{ref? \| nd_song_id?}` → `SmartAddOut` `{ok, status, playlist_id?}`. «+»: a playlist — `added` (a library song is put in the playlist) or `downloading` (a catalog track is downloaded and put in the playlist after the import: a visible download of the batch `origin` `smart-shuffle`, `POST /api/downloads` with `playlist_id`); «Понравившиеся» — `liked`: the Hub only notes it, the client puts the ♥ itself by its usual way (`/api/star`, the ♥ download for a catalog track). Either way the track counts as taken in the session (what sounds like it goes up) |

`SmartOut`: `{session_id, context, title, own, recs, pending, own_per_rec, recorded}`.
- `own` (start only) — the own songs in the order to play, as the ids the client sent, all of them. «Fewer repeats»: the
  freshest of 20 random, artist-spread orders; the staleness of an order is the sum over its songs of
  `1/(1+days since the user last heard it, in any context)` divided by `1 + position/12`.
- `recs` — `SmartRecOut` = `WaveTrackOut` (`TrackOut` + `generator, generator_title, familiar, sim`; `sim` — closeness by
  sound to the center of the list's songs) + `{after, slot}`: put the track right after the own song `after` (an id from
  `own`); `slot` — the number of its place among the recommendations. Play a track by `nd_song_id` when `owned`, else stream its `ref`
  (`GET /api/listen/stream?ref=`, the Hub has started picking the files, `/api/listen/prefetch`).
  **Places:** one recommendation after every 3 own songs in a list of more than 15 songs, after every 2 in a list of up to
  15; a list of 1–2 songs has one (`own_per_rec` — 3 or 2). `start` fills the first 3 places; `next` fills the places not
  filled yet that are in the next 3 recommendations from `at`. A recommendation handed out is final (the client may have
  preloaded it): `next` brings new ones only.
- `pending` — sources still answer: ask `next` again in a few seconds for the missing places.

**How a player keeps it going** (the PWA's page player; the app's player):
- Play `own` with the `recs` put in; the events of an own song carry the list's usual context (`playlist`, `liked`), the
  events of a recommendation — **`smart:<session_id>`** (the Hub also gives it to an event of a recommendation that the client
  stored under another context). Send them to `/api/plays/events` as always.
- Call `next` when any song of the list starts (after its `start` event) and after a reaction to a recommendation
  (`skip`, `finish`, `like`, `dislike`); `played` — the events of the recommendations since the last call (the same
  objects and `event_id`s as in `/api/plays/events`), `at` — the own song the player is at or passed last. A failed call sends
  its events again with the next one.
- Turning the smart shuffle off drops the recommendations from the queue; turning it on again is a new `start` (a fresh set:
  what the last runs of this context recommended in the last 3 days is worth less).

What the Hub leaves out: the songs of the list, «Не нравится», what the user hid with «−» in this context, what was heard in
the last days (the mix's rule); an artist is recommended at most once in 8 recommendations and never next to a song of its
own artist. What it learns from `played`: as the wave does (MIXES.md 7.2) — an early skip lowers the artist and what sounds
like the track, a play to its end, a ♥ or «+» raises what sounds like it (a finished track is not downloaded for the session,
unlike the wave). The rows are kept for the analytics: `smart_sessions`, `smart_items` (`state` `handed`/`added`/`hidden`),
`smart_hidden`.

## Library (own files, albums)
| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/library/albums?q=&offset=0&limit=50` | `LibAlbumOut[]` |
| POST | `/api/library/albums` | `AlbumCreateIn` → `LibAlbumOut` (empty custom album / compilation) |
| GET | `/api/library/track-source?nd_song_id=` | `TrackSourceOut` `{source, source_url, acquired_at}` — where the file came from (`source`: `hitmoz`, `hitmos_fm`, `ytdlp`, `upload`, `manual`); `404` — not a Hub library track (the app's track info) |
| GET | `/api/library/albums/{id}` | `LibAlbumOut` with `tracks` |
| PATCH | `/api/library/albums/{id}` | `AlbumPatchIn` → `LibAlbumOut` (rename, album artist, year, genre, type) |
| PUT | `/api/library/albums/{id}/cover` | multipart `file` (image) → `LibAlbumOut` |
| GET | `/api/library/albums/{id}/cover?size=300` | image/jpeg |
| POST | `/api/library/albums/merge` | `AlbumMergeIn` → `LibAlbumOut` (target) |
| POST | `/api/library/tracks/move` | `TracksMoveIn` → `LibAlbumOut` (target) |
| PATCH | `/api/library/tracks/{id}` | `TrackPatchIn` → `LibTrackOut` |
| DELETE | `/api/library/tracks/{id}` | → `{ok}` (moved to `.trash`, kept 30 days) |
| POST | `/api/star` | Existing `StarIn` fields remain: song_ids, album_ids, artist_ids, catalog_album_ref, refs, unstar. Adds `targets: [{target_type, ref?, nd_song_id?, nd_album_id?, nd_artist_id?, target_data?}]` (≤500), `event_id?`, `ts?`, `client?` (default pwa), `context?`. Successful objects are journaled; response `{ok:true,event_ids:[…]}`. Single target uses the supplied event_id verbatim; bulk uses stable short hashes. `refs + unstar` also clears own pending ♥; explicit/import/mix/wave downloads survive; refs without unstar →400. Typed album/artist refs work without ND ids and do not download/star their songs |
| GET | `/api/starred` | `StarredOut` `{song_ids, album_ids, artist_ids, refs, subjects}`. ND lists remain visible, refs also include own pending track likes. Own subjects include `{target_type,key,ref,nd_id,liked,liked_since,event_id,aliases,target_data,provenance:"own"}`; known imported objects carry provenance import. Use type plus aliases for button state; an own false state takes precedence over a stale ND list |

An `/api/star` retry with the same event_id does not reapply an old command to Navidrome. Delayed commands older
than the effective decision are journaled without overwriting the mirror. Successful chunks of a bulk assistant/API
operation remain journaled if a later chunk fails. For a wave buffer, reuse the returned per-object event_id and the
same typed target so `/api/plays/events` returns duplicate.

The hourly scrobble loop independently reconciles favorites from a successful complete getStarred2 response.
Failure/incomplete response changes no state; a successful empty snapshot is real absence. It waits 24h after the
latest nonduplicate received decision/compensation, preserves pending refs without an ND song, and retries known
undelivered own mirrors. Import provenance covers all three types; ambiguous legacy album/artist imports are
quarantined from bootstrap until verified or explicitly acted upon. A stale ND star cannot resurrect an own unlike.

## Uploads (FR-5)
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/uploads` | multipart `files` (repeat) → `UploadOut[]` with pre-filled `tags` (title, artist, album, album_artist, track_no, disc_no, year, genre, has_cover, duration_s) |
| GET | `/api/uploads?status=uploaded` | `UploadOut[]` |
| GET | `/api/uploads/{id}/cover` | embedded cover image (404 if none) |
| DELETE | `/api/uploads/{id}` | → `{ok}` |
| POST | `/api/uploads/commit` | multipart: field `payload` = JSON `UploadCommitIn`, optional file `cover` → `UploadCommitOut` |

Authentication precedes multipart parsing. `/api/uploads` accepts at most `uploads.max_files` (default 100),
with per-file `max_file_mb` and aggregate `max_total_mb` limits (both default 500 MiB; configurable downward).
The body may add up to 1 MiB for multipart framing. Caddy caps ingress at 501 MiB; the application also counts
actual streamed bytes, including chunked requests. Oversize returns 413, invalid multipart 400; temporary files
are closed on rejection, disconnect or cancellation. Assistant image ingress permits up to 15 images of 15 MiB
each; upload commit permits one 15 MiB cover and a payload field of at most 1 MiB.

## Playlists
| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/playlists` | `PlaylistOut[]` (current user's Navidrome playlists) |
| POST | `/api/playlists` | `PlaylistCreateIn` → `PlaylistOut` |
| POST | `/api/playlists/{id}/add` | `PlaylistAddIn` → `{ok}` |
| GET | `/api/playlists/{id}` | → `PlaylistPageOut` (`PlaylistOut` + `tracks: NdSongOut[]` in playlist order); `404` unknown or not visible to the user, `502` Navidrome error or no answer |
| POST | `/api/playlists/{id}/remove` | `PlaylistRemoveIn {indexes: int[]}` (1–500 positions from 0; a song may be in a playlist twice) → `{ok}`; `403` not the owner, `404` unknown, `502` Navidrome error or no answer |
| DELETE | `/api/playlists/{id}` | → `{ok}`; `403` not the owner, `404` unknown, `502` Navidrome error or no answer |

## Navidrome library (the PWA in a browser)
The library as the user's player sees it — the app has these as native (Tempus) screens, the PWA in a plain browser
(`STANDALONE`) shows them from here (`app/api/medialib.py`). Everything is asked of Navidrome as the current user, so
the stars and play counts are theirs. Navidrome «not found» (Subsonic code 70) → `404`, «not authorized» (50) → `403`, any other Navidrome error or no answer
from it → `502`.

| Method | Path | Response |
|---|---|---|
| GET | `/api/nd/home` | `NdHomeOut {recent, frequent, newest, starred: NdAlbumOut[] (15 each), radio: NdArtistOut[]}` — the rows of «Главная»; `radio` («Радиостанции», 30): liked artists first, then by album count |
| GET | `/api/nd/albums?type=alphabeticalByName&offset=0&size=50` | `NdAlbumOut[]` — Subsonic `getAlbumList2`; `type` one of `recent`, `frequent`, `newest`, `starred`, `alphabeticalByName`, `alphabeticalByArtist`, `random` (else `422`); `size` 1–500 |
| GET | `/api/nd/albums/{id}` | `NdAlbumOut` with `tracks: NdSongOut[]`; `404` unknown |
| GET | `/api/nd/artists` | `NdArtistOut[]` — every artist (the alphabetical index flattened) |
| GET | `/api/nd/artists/{id}` | `NdArtistPageOut {artist, albums}` — albums by year, newest first, those without a year last, ties by title; `404` unknown |
| GET | `/api/nd/liked` | `NdLikedOut {songs, albums, artists}` — Navidrome stars merged with own track favorites and pending import likes. Own decisions override ND/import state; aliases deduplicate songs. Catalog-only songs have `owned=false`, nullable `nd_song_id` and remain playable by `ref`; listing never starts downloads or catalog requests. Stored subject/import/playback metadata supplies names (ref fallback when unavailable). Songs and albums newest ♥ first; unknown import dates last. Successful `/api/star` commits before publishing a `library` SSE (`starred`/`unstarred`). |
| GET | `/api/nd/cover/{cover_id}?size=` | the Navidrome cover (`getCoverArt`, `size` 32–1500) proxied, so the page needs no Navidrome credentials; `Cache-Control: private, max-age=86400`; `404` no image, `502` no answer from Navidrome |

`NdAlbumOut {id, title, artist, artist_id, year, song_count, duration_s, cover_url, starred, tracks?}`,
`NdArtistOut {id, name, album_count, cover_url, starred}` — Navidrome ids. `NdSongOut` is a `TrackOut` plus
`nd_album_id`, `nd_artist_id`: `ref` is `dz:…` when the Hub's own `Track` record of the file has a Deezer id (mixes,
«Моя волна» and listening data know the song by it), else `nd:<song>` (`source` `dz`/`nd`); always `owned`, `nd_song_id`;
`library_track_id` when the Hub keeps the file. Covers are `/api/nd/cover/…` (`cover_small_url` 300 px, `cover_url`
1000 px; an album's `cover_url` 300 px).

## Assistant (FR-6)

Every message may contain up to **15 photos** (each up to 15 MB) and **5000 Unicode characters** of user text,
including whitespace. These per-message limits apply to admins too. They cover chat JSON/multipart and the
legacy task API (`text`/`url`, including multipart text alongside images). Exceeding either limit returns `400`
with a Russian `detail` before model calls, queuing tasks or consuming the daily allowance.
The current accepted message is sent to the model in full; only older chat context may be compacted.

Non-admin users may submit **5 messages per calendar day**, shared by `POST /api/assistant/chat`
(JSON and multipart) and `POST /api/assistant/tasks` (text, links and images). Each accepted request counts once,
regardless of model/tool calls or attached image count, including requests whose later processing fails.
Invalid input or a foreign/deleted chat is rejected before counting. Admins (`is_admin`) have no daily limit.
The persistent counter resets at midnight in `Settings.timezone` (default `Europe/Moscow`); creating/deleting chats,
signing in again or restarting the Hub does not reset it. Previous history is not charged retroactively.
At the limit these POST routes return `429` with a Russian `detail` and `Retry-After` in seconds until midnight,
before calling the model, saving images or queuing a task. History, confirmation and dismissal remain available
and do not count as messages.

| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/assistant/tasks` | multipart `images` (repeat) **or** JSON `AssistantTextIn` (`url` of a Yandex Music / YouTube playlist or album, or free `text`) → `AssistantTaskOut` |
| GET | `/api/assistant/tasks` | `AssistantTaskOut[]` (own tasks, newest first, rows omitted) |
| GET | `/api/assistant/tasks/{id}` | `AssistantTaskOut` |
| POST | `/api/assistant/tasks/{id}/confirm` | `AssistantConfirmIn` → `AssistantTaskOut` |

Task statuses: `queued, extracting, resolving, awaiting_confirmation, confirmed, done, failed`.
Row statuses: `owned` (already in library), `auto` (confident, pre-selected), `confirm` (pick one of `options`), `not_found` (manual input).

## Imports from music services («Перенести музыку»)
| Method | Path | Body → Response |
|---|---|---|
| GET | `/api/imports` | `ImportOut[]` — the user's imports, newest first |
| POST | `/api/imports` | `ImportIn {source: "yandex", method, token?, url?}` → `ImportOut`. `method`: `device` — sign-in by code: `status="auth"`, `auth {user_code, verification_url, expires_at}` (the user enters the code at ya.ru/device; the Hub waits for it, ~5 min); `token` — a Yandex Music token pasted by hand; `profile` — `url` of an open profile (`music.yandex.ru/users/<login>/…`) or the login, no sign-in. `400` bad link / empty token / Yandex refused the code |
| GET | `/api/imports/{id}` | `ImportOut` |
| GET | `/api/imports/{id}/items?collection_id=&status=&match=&offset=0&limit=200` | `ImportItemsOut {items: [ImportItemOut], total}` — rows in the service's order; `status`, `match` — comma lists (the report: `match=probable`, `match=not_found`, `status=failed`) |
| POST | `/api/imports/{id}/start` | `ImportStartIn {collection_ids, mode, download_albums, keep_token}` → `ImportOut` (`409` unless `preview`) |
| POST | `/api/imports/{id}/download_all` | → `ImportOut`: economy → full, what waits for a listen is downloaded now in the background |
| POST | `/api/imports/{id}/sync` | → `ImportOut`: «Обновить» — reads the collection again (needs the kept sign-in or a profile); new rows go the way the first ones went, removed ones drop out of the playlists. `409` while it runs |
| DELETE | `/api/imports/{id}/token` | → `ImportOut`: the stored sign-in is removed |
| POST | `/api/imports/{id}/cancel` | → `ImportOut`: stops matching; its downloads that have not started are cancelled; done work stays |
| DELETE | `/api/imports/{id}` | → `{ok}`: forgets the import and its sign-in; music, ♥ and playlists stay |
| POST | `/api/imports/items/{id}/pick` | → `ImportPickOut {download_item_id}`: a not found (or wrongly matched) track becomes a visible download by its names without the catalog match; the user picks the file on its page (`/api/downloads/{id}`, `needs_review`) |
| POST | `/api/imports/items/{id}/skip` | → `{ok}`: the row is left out |

`ImportOut.status`: `auth` (waiting for the code) → `fetching` → `preview` (choose what to move; `stats` counts distinct
tracks of the likes and playlists: `owned`, `to_download`, `est_bytes` at 320 kbps, `free_bytes` of the music disk) →
`running` → `done`; `failed` (`error`), `cancelled`. `collections[].kind`: `likes` («Мне нравится»), `playlist`,
`albums`, `artists`, `dislikes` (only kept). Without a sign-in (`profile`) there are no private playlists and no dislikes.

Matching (`ImportItemOut.match`): `owned` — in the library; `exact` — a confident catalog match; `probable` — weaker
(other duration or version; a Cyrillic track Deezer lacks may be matched on hitmoz, `ref` `hm:…`); `not_found`.
What a service id turned out to be is shared by all users (`source_map`); a miss is looked up again after 7 days.
Row `status`: `pending` (not matched yet), `waiting` (economy: matched, downloaded once listened to), `queued`
(`download_item_id`, `download_status`), `done`, `failed`, `not_found`, `skipped`.

Modes: `economy` — ♥ and Navidrome playlists at once from what the library has; the rest plays from the sources
(`ref` → `/api/listen/stream`, the app's `playQueue`), and a track is downloaded once it is listened to (the first
request of its stream); `full` — everything is downloaded in the background. Import downloads are quiet
(`DownloadItemOut.star`, `import_id`) and run below the user's work: queue `priority` < 0, they never take the last
worker and their catalog/source requests yield to the user's. Playlists are rewritten whole in the service's order
(at most every 30 s while downloads come in). ♥ follow the service's order too: Navidrome orders «Понравившиеся» by
the time of the star, so the Hub stars an import's liked songs one by one from the oldest like, and a song that arrives
later is starred together with the likes newer than it again (likes are downloaded oldest first, so mostly it is one
star). Import downloads carry no ♥ of their own; a song the user has unstarred since is left alone. When the import is
over, liked albums and artists the library has get ★. The sign-in token is kept encrypted (Fernet) and removed at the end unless `keep_token`.

## Events (SSE)
`GET /api/events` — `text/event-stream`, named events, `data` is JSON:
- `download` → `DownloadItemOut` (without candidates) on every status/progress change
- `assistant` → `{id, status}`
- `library` → `{kind: "imported"|"changed", album_id, track_id?}`
- `import` → `{id, status}` when an import moves on (sign-in, fetched, a batch matched, done): refetch `/api/imports/{id}`
- `search_history` → `{}` after saving/clearing the account's recent queries: refetch `/api/search/history`
- `ping` → `{}` every 20 s

## Share target / system
| Method | Path | Notes |
|---|---|---|
| POST | `/share-target` | Fallback for Web Share Target if the service worker didn't handle it: images → new assistant task, audio → uploads; redirects to `/#/assistant/{id}` or `/#/upload` (the service worker's path, `/#/share`, opens a new assistant chat with the shared screenshots or text instead) |
| GET | `/healthz` | `HealthOut` (no auth). `capabilities` includes `typed_favorites_v1`: new clients may send typed events/targets and causal rollback only when present. `launch_events_v1` — the Hub stores `launch_id`/`launch_origin` of `PlayEventIn`; `wave_origin_v1` — it keeps `origin`/`client`/`restart_of` of `WaveStartIn` (an older Hub ignores them silently, clients gate on these anyway). `smart_shuffle_v1` — the smart shuffle's endpoints exist (`/api/smart-shuffle/*`). `favorite_compensation_chains_v1` additionally permits retraction of compensations (late accept); clients retain chain events until this separate capability is present. `web_build` — id of the PWA build the Hub serves (`<meta name="hm-build">` of `index.html`, `null` without a build): an open page with another id reloads itself (`web/src/lib/update.ts`) |
| GET | `/readyz` | No auth; `{ok,status,components}`. HTTP503 if Hub DB, Navidrome or required background workers are unavailable. Optional embedder failure yields `status=degraded` with HTTP200. No paths, credentials or internal error text |
| GET | `/api/system/health` | Admin-only `HealthOut` extended with `status`, component states, queue counts/oldest ages, free disk bytes, local/external backup status and last success, background task states. Provider search smoke tests remain daily maintenance rather than a side effect of opening diagnostics |

`/healthz` is liveness only: no upstream requests. `navidrome` is the cached readiness result or `null` before
the first probe, not a fresh availability guarantee. `web_build` and capabilities retain their existing meaning.

### Free chat (stage 3.8)
| Method | Path | Body → Response |
|---|---|---|
| POST | `/api/assistant/chat` | JSON `{messages: [...], chat_id?}` or multipart `messages` (JSON) + `chat_id?` + repeated `images` (PNG/JPEG/WebP/HEIC, up to 15 × 15 MB). The latest user message is limited to 5000 Unicode characters. Images are sent to the model with the latest user message. The turn (the latest user message and the reply) is saved into the chat `chat_id` (a foreign or deleted one → 404 before the model runs) or a new one. Response: `{reply, done: [str], cost_usd, actions: [...], task_id?, confirm_token?, summary?, chat_id, title}` |
| POST | `/api/assistant/chat/{task_id}/confirm` | `{confirm_token, chat_id?}` → `{ok, playlists_deleted: [id], tracks_deleted}`; the proposal in that saved chat becomes `confirmed`. A proposal of the old chat (other action types) or a dismissed one → 409 |
| POST | `/api/assistant/chat/{task_id}/dismiss` | `{chat_id?}` → `{ok}`: the proposal is cancelled (its token no longer works) and becomes `dismissed` in the saved chat |
| GET | `/api/assistant/chats` | the user's saved chats, latest first (up to 100): `[{id, title, messages (count), created_at, updated_at}]` |
| GET | `/api/assistant/chats/{id}` | `{id, title, created_at, updated_at, messages: [{role, content, images? (count), cost?, done?, proposal?: {task_id, token, summary, actions, state: pending/confirmed/dismissed}}]}` |
| DELETE | `/api/assistant/chats/{id}` | `{ok}` |

Saved chats (`assistant_chats`) keep the messages as the PWA shows them; screenshots are not stored, only their
count. The title is the first message (up to 80 characters) or «Скриншот(ы)». The client still sends the whole
visible history in `messages`.

Only deleting needs a confirmation. Action objects: `{type: "delete_playlist", playlist_id, name}` /
`{type: "delete_library_tracks", track_ids, titles}` (files go to the trash, for everyone).

Tools of the model (`app/ai/agent.py`, compact tables `{cols, rows}`): `account_overview` (library size, ♥ counts,
playlists, imports with collections, downloads, uploads, recently played, AI spending), catalog search, artist, album,
library search, `list_starred` (with the import that starred a song), `get_playlist`, `get_import_items`,
`get_downloads`; changes: `create_playlist`, `add_to_playlist`, `remove_from_playlist`, `rename_playlist`, `star`,
`unstar`, `enqueue_download`, `cancel_downloads`, `retry_downloads`. Many songs are named by a set (`songs_from`):
`starred`, `starred_from_imports`, `playlist:<id>`, `import_collection:<id>`. SSE `library` `{kind: "unstarred",
song_ids}` — the assistant took ♥ off.

### Internal embedder geometry v2 (stage E, 2026-10-26)

The compose-only embedder adds authenticated POST routes; these are not public `/api` routes.
The existing `/similarity`, `/vectors` (raw vectors) and `/text` contracts remain unchanged.

| Path | Body → response |
|---|---|
| `/v2/profile` | `{positives: [{key, weight}], negatives?: [key]}` → `{model, space_id, dim, facets: [{id, weight, members, center}], negatives, missing, diagnostics}` |
| `/v2/similarity` | `{space_id, candidates, facets, negatives?: [key]}` → `{model, space_id, dim, results: [{key, pos, neg, score}], missing, diagnostics}` |
| `/v2/vectors` | `{space_id, keys}` → `{model, space_id, dim, vectors: {key: [float]}, missing, diagnostics}` |
| `/v2/shadow` | `{candidates, baseline: [{key, weight}], positives: [{key, weight}], negatives}` → `{model, space_id, dim, v1: {key: score}, v2: {key: score}, missing_v1, missing_v2, missing_baseline, missing_positive, missing_negative}` |

Vectors are normalized after subtracting the mean of all valid done vectors of the current model.
Facet weights are positive-profile weight fractions; `score = max facet cosine − 0.5 * max(0, closest negative cosine)`.
An empty positive/negative set contributes zero. Missing vectors, zero residuals and invalid stored vectors are omitted;
`diagnostics` contains `{available, zero_residual: [key], invalid: [key]}` for the requested keys.
Each list is limited to 5000 keys (1–300 characters), positive weights to finite `[1, 1000000]`, facets to five,
centers to 4096 finite coordinates in `[-1, 1]` with nonzero norm and the snapshot dimension; invalid input → 422.
`space_id` identifies the model and vector snapshot. An expired id → 409
`{detail: {code: "stale_space", space_id, model}}`; rebuild the profile and session coordinates before retrying.
The service retains only its current geometry snapshot. Hub wrappers raise `EmbedderSpaceExpired` (an `EmbedderDown`)
on stale or changed space; old-service 404, HTTP failures and malformed replies use `EmbedderDown`.
`/v2/shadow` reads one atomic copy of already completed vectors and calculates v1 raw-center and v2 centered-facet
scores on that same snapshot. It creates no embedding jobs and does not require model inference. Candidates are limited
to 500; profile lists to 5000, baseline weights finite in `[-1000000, 1000000]`. Missing candidates and profile
vectors are reported separately. Optional `include_vectors: true` additionally returns `raw_vectors` and
`centered_vectors` for candidates from the same snapshot, or null if `dim × candidate_count` exceeds 512000.
Hub uses these transient coordinates only to replay select/order on a fully captured pool of at most 500 candidates;
coordinates are not stored in mix stats. Hub's optional diagnostic uses a five-second timeout and preserves the no-sound report if
the endpoint is absent or unavailable. This is not a complete policy comparison.
See `MIXES.md` 4.2 for deterministic clustering, snapshot invalidation and consumer rollout.


Волна по исполнителю трека: `seed` принимает `ref` или `nd_song_id` вместе с `by_artist: true`. Хаб разрешает исполнителя и сохраняет в ответе `artist_ref` / `nd_artist_id`; если исполнитель не найден, возвращает 404. Личная волна запускается без `seed`.


### Android application updates

`GET /app/latest.json` retains the historical `versionCode`, `versionName`, `notes`, `size`, `sha256`,
`url: /app/latest.apk` contract. Historical manifests are read with UTF-8 BOM support. `GET /app/latest.apk`
serves the current complete APK for first installs and old clients. New manifests can additionally contain `full`
(`url`, `size`, `sha256`) and `patches` entries (`baseVersionCode`, `baseSha256`, `format: bsdiff40-v1`,
`url`, `size`, `sha256`). The full APK and patches are addressed as
`/app/artifacts/apk-<sha256>.apk` and `/app/artifacts/patch-<sha256>.bsdiff`. These files are immutable;
the Hub checks paths and sizes when exposing metadata and verifies the file hash before download. Clients select
only a smaller patch with the exact installed base hash and version, then verify the reconstructed APK against `full`.
The metadata is optional: clients fall back to the historical full APK when absent.
