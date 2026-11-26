"""The assistant's chat with tools through the HTTP API: the model (a scripted OpenAI-compatible server) reads the
account and changes ♥ and playlists itself; deleting waits for the user's confirmation. Navidrome is a mock that keeps
playlists and stars.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from tests.support import app_client

LLM = "https://llm.test/v1"


def tool_call(tool: str, **args) -> dict:
    return {"id": f"call_{tool}_{uuid.uuid4().hex[:6]}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}


def completion(content: str = "", calls: list[dict] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": msg}]}


SONGS = {"ndA": ("Кино", "Группа крови"), "ndB": ("ДДТ", "Осень"), "ndC": ("Сплин", "Выхода нет")}


@pytest.fixture
async def env(hub_env, monkeypatch):
    from app import config
    from app.main import create_app

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", LLM)
    config.get_settings.cache_clear()
    state: dict = {"playlists": {"pl1": {"name": "Тест", "songs": ["ndA", "ndB"]}}, "stars": ["ndA", "ndB", "ndC"],
                   "deleted": [], "script": [], "llm_requests": []}

    def song(i: str) -> dict:
        return {"id": i, "artist": SONGS[i][0], "title": SONGS[i][1]}

    def nd_handler(request: httpx.Request) -> httpx.Response:
        ep = request.url.path.rsplit("/", 1)[-1]
        p = request.url.params

        def reply(**body) -> httpx.Response:
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **body}})

        if ep == "getUser":
            return reply(user={"adminRole": True})
        if ep == "getPlaylists":
            return reply(playlists={"playlist": [{"id": k, "name": v["name"], "songCount": len(v["songs"]),
                                                  "owner": "admin"} for k, v in state["playlists"].items()]})
        if ep == "getPlaylist":
            pl = state["playlists"][p["id"]]
            return reply(playlist={"id": p["id"], "name": pl["name"], "entry": [song(i) for i in pl["songs"]]})
        if ep == "createPlaylist":
            pid = p.get("playlistId") or f"pl{len(state['playlists']) + 1}"
            name = p.get("name") or state["playlists"][pid]["name"]
            state["playlists"][pid] = {"name": name, "songs": p.get_list("songId")}
            return reply(playlist={"id": pid, "name": name})
        if ep == "updatePlaylist":
            pl = state["playlists"][p["playlistId"]]
            for i in sorted(map(int, p.get_list("songIndexToRemove")), reverse=True):
                del pl["songs"][i]
            pl["songs"] += p.get_list("songIdToAdd")
            pl["name"] = p.get("name") or pl["name"]
            return reply()
        if ep == "deletePlaylist":
            state["deleted"].append(p["id"])
            state["playlists"].pop(p["id"])
            return reply()
        if ep in ("star", "unstar"):
            ids = p.get_list("id")
            state["stars"] = [x for x in state["stars"] if x not in ids] + (ids if ep == "star" else [])
            return reply()
        if ep == "getStarred2":
            return reply(starred2={"song": [song(i) for i in state["stars"]]})
        if ep == "getAlbumList2":
            return reply(albumList2={"album": []})
        return reply()

    def llm_handler(request: httpx.Request) -> httpx.Response:
        state["llm_requests"].append(json.loads(request.content))
        if not state["script"]:  # the model stopped answering (400: the client does not retry)
            return httpx.Response(400, json={"error": {"message": "no more"}})
        return httpx.Response(200, json=state["script"].pop(0))

    app = create_app()
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as mock:
            mock.route(host="navidrome.test").mock(side_effect=nd_handler)
            mock.post(f"{LLM}/chat/completions").mock(side_effect=llm_handler)
            async with app_client(app) as client:
                yield {"client": client, "state": state, "yandex": await yandex_import()}


async def yandex_import() -> dict:
    """A done Yandex import whose «Мне нравится» (newest first: Осень, Группа крови) starred ndA and ndB."""
    from app.db import session_scope
    from app.matching.normalize import fingerprint
    from app.models import Import, ImportCollection, ImportItem, Track, User

    async with session_scope() as s:
        user = (await s.execute(User.__table__.select())).first()
        imp = Import(user_id=user.id, source="yandex", method="token", status="done", account="ivan",
                     options_json={"liked": ["ndA", "ndB"]})
        s.add(imp)
        await s.flush()
        likes = ImportCollection(import_id=imp.id, kind="likes", external_id="likes", title="Мне нравится")
        s.add(likes)
        await s.flush()
        for pos, nd_id in enumerate(["ndB", "ndA"]):
            artist, title = SONGS[nd_id]
            t = Track(uid=str(uuid.uuid4()), path=f"{artist}/{title}.mp3", nd_song_id=nd_id, artist=artist,
                      title=title, fingerprint=fingerprint(artist, title))
            s.add(t)
            await s.flush()
            s.add(ImportItem(import_id=imp.id, collection_id=likes.id, position=pos, external_id=str(pos),
                             raw_json={"artist": artist, "title": title}, status="done", match="exact",
                             track_id=t.id))
        return {"import_id": imp.id, "likes_id": likes.id}


async def chat(env, text: str, *script: dict) -> dict:
    env["state"]["script"] = list(script)
    r = await env["client"].post("/api/assistant/chat", json={"messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    assert env["state"]["script"] == [], "the model was not asked every scripted step"
    return r.json()


async def test_found_files_are_playable_saved_and_download_only_on_request(env, monkeypatch):
    from app.services import svc
    from tests.support import FakeDeezer, FakeProvider, cand

    live = cand("live", "https://cdn.test/live.mp3", title="Группа крови (Live)")
    offline = FakeProvider("unavailable")
    offline.down = True
    monkeypatch.setattr(svc, "providers", {"hitmoz": FakeProvider(cands=[live, live]), "unavailable": offline})
    monkeypatch.setattr(svc, "deezer", FakeDeezer())
    out = await chat(env, "Найди студийную и концертную версии Группы крови",
                     completion(calls=[tool_call("search_catalog", q="Группа крови", kind="track"),
                                       tool_call("search_files", q="Группа крови")]), completion("Нашёл две версии."))
    c = env["client"]
    tracks = out["tracks"]
    assert [(t["ref"], t["title"]) for t in tracks] == [
        ("dz:100", "Группа крови"), ("file:hitmoz:live", live.title)]
    assert live.download_url not in json.dumps(tracks) and out["done"] == []
    assert (await c.get("/api/downloads")).json() == []
    history = (await c.get(f"/api/assistant/chats/{out['chat_id']}")).json()
    assert history["messages"][-1]["tracks"] == tracks
    ref = tracks[1]["ref"]
    playback = await c.get("/api/listen", params={"ref": ref})
    assert playback.status_code == 200 and playback.json()["page_url"] == live.page_url
    queued = await c.post("/api/downloads", json={"track_refs": [ref]})
    assert queued.status_code == 200 and queued.json()["queued"] == 1
    item, = (await c.get("/api/downloads", params={"ref": ref})).json()
    assert not item["star"] and item["title"] == live.title


async def test_daily_limit_covers_all_chats_images_and_legacy_tasks(env):
    # One shared app for the full quota lifecycle avoids rebuilding it for each HTTP boundary.
    from sqlalchemy import update

    from app.db import session_scope
    from app.models import User

    async with session_scope() as s:
        await s.execute(update(User).values(is_admin=False))
    c = env["client"]
    # Rejected input and missing chats do not consume messages.
    assert (await c.post("/api/assistant/chat", json={"messages": []})).status_code == 400
    assert (await c.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": " "}]})).status_code == 400
    assert (await c.post("/api/assistant/tasks", json={"text": " "})).status_code == 400
    missing = await c.post("/api/assistant/chat", json={"chat_id": 999, "messages": [{"role": "user", "content": "x"}]})
    assert missing.status_code == 404
    first = await chat(env, "Привет", completion("Привет"))  # 1: new chat
    env["state"]["script"] = [completion("Да")]
    response = await c.post("/api/assistant/chat", data={
        "chat_id": str(first["chat_id"]), "messages": json.dumps([{"role": "user", "content": "Скриншот"}]),
    }, files={"images": ("screen.png", b"image", "image/png")})  # 2: existing chat + image
    assert response.status_code == 200, response.text
    response = await c.post("/api/assistant/tasks", json={"url": "https://music.yandex.ru/album/10100"})  # 3
    assert response.status_code == 200, response.text
    response = await c.post("/api/assistant/tasks", files={"images": ("screen.png", b"image", "image/png")})  # 4
    assert response.status_code == 200, response.text
    last = await chat(env, "Удали плейлист", completion(calls=[tool_call("delete_playlist", playlist_ids=["pl1"])]),
                      completion("Подтвердите удаление"))  # 5: another chat, multiple model calls
    requests = len(env["state"]["llm_requests"])
    assert (await c.delete(f"/api/assistant/chats/{first['chat_id']}")).status_code == 200
    for path, kwargs in [
        ("/api/assistant/chat", {"json": {"messages": [{"role": "user", "content": "Ещё"}]}}),
        ("/api/assistant/chat", {"data": {"messages": '[{"role":"user","content":""}]'},
                                 "files": {"images": ("s.png", b"image", "image/png")}}),
        ("/api/assistant/tasks", {"json": {"text": "Ещё"}}),
        ("/api/assistant/tasks", {"files": {"images": ("s.png", b"image", "image/png")}}),
    ]:
        denied = await c.post(path, **kwargs)
        assert denied.status_code == 429, denied.text
        assert "5 сообщений" in denied.json()["detail"]
        assert int(denied.headers["Retry-After"]) > 0
    assert len(env["state"]["llm_requests"]) == requests
    # Completing an already paid-for proposal still works at the limit.
    confirmed = await c.post(f"/api/assistant/chat/{last['task_id']}/confirm", json={
        "confirm_token": last["confirm_token"], "chat_id": last["chat_id"],
    })
    assert confirmed.status_code == 200, confirmed.text
    assert env["state"]["deleted"] == ["pl1"]
    assert (await c.get("/api/assistant/chats")).status_code == 200


async def test_admin_can_send_more_than_five_messages(env):
    # Seven real API turns verify the admin exemption with the production model-call path.
    for _ in range(7):
        await chat(env, "Привет", completion("Привет"))
    assert len(env["state"]["llm_requests"]) == 7


async def test_model_failure_does_not_refund_an_accepted_message(env):
    from sqlalchemy import select

    from app.ai.limits import reserve_message
    from app.db import session_scope
    from app.models import User

    async with session_scope() as s:
        user = (await s.execute(select(User))).scalar_one()
        user.is_admin = False
        for _ in range(4):
            await reserve_message(s, user)
    # Empty model script returns an immediate, non-retried error from the fake provider.
    body = {"messages": [{"role": "user", "content": "Привет"}]}
    failed = await env["client"].post("/api/assistant/chat", json=body)
    assert failed.status_code == 502, failed.text
    denied = await env["client"].post("/api/assistant/chat", json=body)
    assert denied.status_code == 429, denied.text
    assert len(env["state"]["llm_requests"]) == 1


async def test_chat_sends_attached_screenshot_to_model(env):
    env["state"]["script"] = [completion("На скриншоте вижу экран музыки.")]
    response = await env["client"].post(
        "/api/assistant/chat",
        data={"messages": json.dumps([{"role": "user", "content": "Что на этом скриншоте?"}], ensure_ascii=False)},
        files={"images": ("screen.png", b"screenshot-bytes", "image/png")},
    )
    assert response.status_code == 200, response.text
    sent = env["state"]["llm_requests"][-1]["messages"][-1]
    assert sent["role"] == "user"
    assert sent["content"][0]["text"] == "Что на этом скриншоте?"
    assert sent["content"][1]["type"] == "image_url"
    assert "c2NyZWVuc2hvdC1ieXRlcw==" in sent["content"][1]["image_url"]["url"]


@pytest.mark.parametrize("path", ["chat", "tasks"])
@pytest.mark.parametrize("count", [15, 16], ids=["at-limit", "over-limit"])
@pytest.mark.parametrize("admin", [True, False], ids=["admin", "ordinary"])
async def test_photos_per_message_limit(env, path, count, admin):
    from sqlalchemy import func, select, update

    from app.db import session_scope
    from app.models import AssistantDailyUsage, AssistantTask, User

    async with session_scope() as s:
        await s.execute(update(User).values(is_admin=admin))
    env["state"]["script"] = [completion("Вижу фотографии")]
    files = [("images", (f"image-{i}.png", b"image", "image/png")) for i in range(count)]
    response = await env["client"].post(f"/api/assistant/{path}",
        data={"messages": '[{"role":"user","content":""}]'} if path == "chat" else {}, files=files)
    if count > 15:
        assert response.status_code == 400, response.text
        assert "15 фотографий" in response.json()["detail"]
        assert env["state"]["llm_requests"] == []
    else:
        assert response.status_code == 200, response.text
        if path == "chat":
            sent = env["state"]["llm_requests"][0]["messages"][-1]["content"]
            assert len([part for part in sent if part["type"] == "image_url"]) == 15
        else:
            async with session_scope() as s:
                task = await s.get(AssistantTask, response.json()["id"])
                assert task.input_ref["count"] == 15
    async with session_scope() as s:
        charged = await s.scalar(select(func.sum(AssistantDailyUsage.messages)))
        assert (charged or 0) == (1 if count == 15 and not admin else 0)
        if count > 15:
            assert await s.scalar(select(func.count()).select_from(AssistantTask)) == 0


@pytest.mark.parametrize("kind", ["chat-json", "chat-multipart", "task-json", "task-multipart", "task-url"])
@pytest.mark.parametrize("size", [5000, 5001], ids=["at-limit", "over-limit"])
async def test_message_text_limit(env, kind, size):
    from sqlalchemy import func, select, update

    from app.db import session_scope
    from app.models import AssistantDailyUsage, AssistantTask, User

    async with session_scope() as s:
        await s.execute(update(User).values(is_admin=False))
    # Astral characters count as one character, matching the browser's Array.from count.
    text = "🎵" * (size - 3) + "END"
    env["state"]["script"] = [completion("Принято")]
    is_chat = kind.startswith("chat-")
    path = "chat" if is_chat else "tasks"
    body = {"messages": [{"role": "user", "content": text}]} if is_chat else {"url" if kind == "task-url" else "text": text}
    if kind.endswith("multipart"):
        data = {"messages": json.dumps(body["messages"])} if is_chat else body
        response = await env["client"].post(f"/api/assistant/{path}", data=data,
            files={"images": ("screen.png", b"image", "image/png")} if is_chat else {"unused": (None, "")})
    else:
        response = await env["client"].post(f"/api/assistant/{path}", json=body)
    if size > 5000:
        assert response.status_code == 400, response.text
        assert "5000 символов" in response.json()["detail"]
        assert env["state"]["llm_requests"] == []
    else:
        assert response.status_code == 200, response.text
        if is_chat:
            sent = env["state"]["llm_requests"][0]["messages"][-1]["content"]
            assert (sent[0]["text"] if isinstance(sent, list) else sent) == text
        else:
            async with session_scope() as s:
                task = await s.get(AssistantTask, response.json()["id"])
                assert task.input_ref["url" if kind == "task-url" else "text"] == text
    async with session_scope() as s:
        charged = await s.scalar(select(func.sum(AssistantDailyUsage.messages)))
        assert (charged or 0) == (1 if size == 5000 else 0)
        if size > 5000:
            assert await s.scalar(select(func.count()).select_from(AssistantTask)) == 0


def tool_results(env) -> list[dict]:
    return [json.loads(m["content"]) for m in env["state"]["llm_requests"][-1]["messages"] if m["role"] == "tool"]


async def test_moves_yandex_likes_into_a_playlist_without_asking(env):
    likes = f"import_collection:{env['yandex']['likes_id']}"
    out = await chat(
        env, "То что было добавлено импортом из яндекс музыки в понравившиеся убрать в отдельный плейлист",
        completion(calls=[tool_call("account_overview")]),
        completion(calls=[tool_call("create_playlist", name="Яндекс Музыка", songs_from=likes),
                          tool_call("unstar", songs_from=likes)]),
        completion("Готово: 2 трека в плейлисте «Яндекс Музыка», ♥ снято."),
    )
    state = env["state"]
    new = next(v for v in state["playlists"].values() if v["name"] == "Яндекс Музыка")
    assert new["songs"] == ["ndB", "ndA"]  # Yandex's order: the newest like first
    assert state["stars"] == ["ndC"]  # the user's own ♥ stays
    assert out["done"] == ["Создан плейлист «Яндекс Музыка»: 2 трека", "♥ снято: 2 трека"]
    assert out["actions"] == [] and "task_id" not in out
    overview = tool_results(env)[0]
    assert overview["starred"] == {"songs": 3, "songs_from_imports": 2, "albums": 0, "artists": 0}
    coll = overview["imports"][0]["collections"]
    assert coll["cols"][:5] == ["collection_id", "kind", "title", "selected", "items"]
    assert coll["rows"][0][:5] == [env["yandex"]["likes_id"], "likes", "Мне нравится", True, "done:2"]


async def test_starred_songs_name_the_import_that_starred_them(env):
    await chat(env, "что из яндекса?", completion(calls=[tool_call("list_starred", only="imports")]),
               completion("Два трека."))
    res = tool_results(env)[0]
    assert res["cols"] == ["song_id", "song", "album", "import_id"]
    assert [r[:2] + r[3:] for r in res["rows"]] == [["ndA", "Кино — Группа крови", env["yandex"]["import_id"]],
                                                    ["ndB", "ДДТ — Осень", env["yandex"]["import_id"]]]


async def test_removes_songs_from_a_playlist_and_renames_it(env):
    out = await chat(env, "убери Осень из Тест и назови его Рок",
                     completion(calls=[tool_call("remove_from_playlist", playlist_id="pl1", song_ids=["ndB"]),
                                       tool_call("rename_playlist", playlist_id="pl1", name="Рок")]),
                     completion("Готово."))
    assert env["state"]["playlists"]["pl1"] == {"name": "Рок", "songs": ["ndA"]}
    assert out["done"] == ["Из плейлиста «Тест» убрано 1 трек", "Плейлист «Тест» переименован в «Рок»"]


async def test_deleting_a_playlist_waits_for_confirmation(env):
    out = await chat(env, "удали плейлист Тест", completion(calls=[tool_call("delete_playlist", playlist_ids=["pl1"])]),
                     completion("Удалю после подтверждения."))
    assert env["state"]["deleted"] == []
    assert out["actions"] == [{"type": "delete_playlist", "playlist_id": "pl1", "name": "Тест"}]
    assert out["summary"] == "Удалить плейлист «Тест»"
    c = env["client"]
    r = await c.post(f"/api/assistant/chat/{out['task_id']}/confirm", json={"confirm_token": "wrong"})
    assert r.status_code == 403, r.text
    r = await c.post(f"/api/assistant/chat/{out['task_id']}/confirm", json={"confirm_token": out["confirm_token"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "playlists_deleted": ["pl1"], "tracks_deleted": 0}
    assert env["state"]["deleted"] == ["pl1"]


@pytest.mark.parametrize("args", [{"songs_from": "everything"}, {"songs_from": "import_collection:999"}],
                         ids=["unknown-source", "foreign-collection"])
async def test_a_bad_song_set_is_an_error_for_the_model(env, args):
    out = await chat(env, "поставь ♥", completion(calls=[tool_call("star", **args)]), completion("Не вышло."))
    assert "error" in tool_results(env)[0]
    assert out["done"] == [] and env["state"]["stars"] == ["ndA", "ndB", "ndC"]


async def test_changes_made_stay_listed_when_the_model_stops_answering(env):
    env["state"]["script"] = [completion(calls=[tool_call("star", song_ids=["ndA"])])]  # then the server fails
    r = await env["client"].post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "♥"}]})
    assert r.status_code == 200, r.text
    assert r.json()["done"] == ["♥ поставлено: 1 трек"]


async def test_chat_turns_are_saved_and_continue_the_same_chat(env):
    c = env["client"]
    first = await chat(env, "поставь ♥ Группе крови", completion(calls=[tool_call("star", song_ids=["ndA"])]),
                       completion("Поставил."))
    chat_id = first["chat_id"]
    assert first["title"] == "поставь ♥ Группе крови"
    env["state"]["script"] = [completion("Пожалуйста.")]
    r = await c.post("/api/assistant/chat", json={"chat_id": chat_id, "messages": [
        {"role": "user", "content": "поставь ♥ Группе крови"}, {"role": "assistant", "content": "Поставил."},
        {"role": "user", "content": "спасибо"}]})
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == chat_id

    r = await c.get("/api/assistant/chats")
    assert [(x["id"], x["title"], x["messages"]) for x in r.json()] == [(chat_id, "поставь ♥ Группе крови", 4)]
    saved = (await c.get(f"/api/assistant/chats/{chat_id}")).json()["messages"]
    assert [(m["role"], m["content"]) for m in saved] == [
        ("user", "поставь ♥ Группе крови"), ("assistant", "Поставил."), ("user", "спасибо"), ("assistant", "Пожалуйста.")]
    assert saved[1]["done"] == ["♥ поставлено: 1 трек"]

    assert (await c.delete(f"/api/assistant/chats/{chat_id}")).status_code == 200
    assert (await c.get("/api/assistant/chats")).json() == []
    r = await c.post("/api/assistant/chat", json={"chat_id": chat_id, "messages": [{"role": "user", "content": "ещё"}]})
    assert r.status_code == 404, r.text


async def test_saved_chat_remembers_screenshots_and_the_answer_to_a_proposal(env):
    c = env["client"]
    env["state"]["script"] = [completion(calls=[tool_call("delete_playlist", playlist_ids=["pl1"])]),
                              completion("Удалю после подтверждения.")]
    r = await c.post("/api/assistant/chat", data={"messages": json.dumps([{"role": "user", "content": ""}])},
                     files={"images": ("screen.png", b"png", "image/png")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["title"] == "Скриншот"
    r = await c.post(f"/api/assistant/chat/{out['task_id']}/dismiss", json={"chat_id": out["chat_id"]})
    assert r.status_code == 200, r.text
    saved = (await c.get(f"/api/assistant/chats/{out['chat_id']}")).json()["messages"]
    assert saved[0] == {"role": "user", "content": "", "images": 1}
    assert saved[1]["proposal"]["state"] == "dismissed"
    assert saved[1]["proposal"]["actions"] == [{"type": "delete_playlist", "playlist_id": "pl1", "name": "Тест"}]
    r = await c.post(f"/api/assistant/chat/{out['task_id']}/confirm", json={"confirm_token": out["confirm_token"]})
    assert r.status_code == 409, r.text  # a dismissed proposal can no longer delete anything
    assert env["state"]["deleted"] == []
