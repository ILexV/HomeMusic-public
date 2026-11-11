import base64
import io
import itertools
import json

import httpx
import pytest
import respx
from PIL import Image

import app.ai.llm as llm_mod
from app.ai.extract import (
    REPORT_TRACKS_SCHEMA,
    ExtractedItem,
    Extraction,
    TextIntent,
    _tile_starts,
    dedupe_items,
    extract_from_images,
    merge_extractions,
    norm_key,
    parse_extraction,
    parse_text_request,
    parse_track_list,
    tile_image,
)
from app.ai.llm import (
    LLMClient,
    LLMError,
    LLMUsage,
    completion_limit,
    configured,
    extract_json_object,
    image_part,
)
from app.config import Settings

BASE = "https://openrouter.ai/api/v1"
URL = f"{BASE}/chat/completions"


def png(w: int, h: int, color: tuple[int, int, int] = (200, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def settings(**kw) -> Settings:
    base = {"llm_base_url": BASE, "llm_api_key": "test-key", "llm_model": "anthropic/claude-haiku-5.5",
            "llm_price_in": 0.10, "llm_price_out": 0.50, "llm_timeout_s": 10}
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    llm_mod._mode_cache.clear()
    yield
    llm_mod._mode_cache.clear()


def completion(*, tool_args: dict | None = None, content: str | None = None, tool_name: str = "report_tracks",
               usage: dict | None = None, finish: str = "tool_calls") -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_args is not None:
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)}}]
    return {
        "id": "gen-1", "object": "chat.completion", "created": 1, "model": "anthropic/claude-haiku-5.5",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 3400, "completion_tokens": 200, "total_tokens": 3600},
    }


TRACKS_ARGS = {
    "source_app": "yandex_music", "page_type": "playlist",
    "context": {"playlist_name": " Мой рок ", "album_title": None, "album_artist": None},
    "items": [
        {"artist_raw": "Кино", "title_raw": "Группа крови", "truncated": False, "conf": 0.98, "duration": "4:44"},
        {"artist_raw": "Сплин", "title_raw": "Выхода нет", "truncated": False, "conf": 0.9},
    ],
}


# --------------------------------------------------------------------------- tiling


def test_tile_small_image_unchanged() -> None:
    data = png(1080, 2400)
    assert tile_image(data) == [data]


def test_tile_starts_cover_and_overlap() -> None:
    starts = _tile_starts(5000, 2200, 300)
    assert starts == [0, 1900, 2800]
    for a, b in itertools.pairwise(starts):
        assert a + 2200 - b >= 300  # overlap at least 300 px
    assert starts[-1] + 2200 == 5000  # last tile ends at the bottom


def test_tile_tall_image() -> None:
    tiles = tile_image(png(1080, 5000))
    assert len(tiles) == 3
    sizes = [Image.open(io.BytesIO(t)).size for t in tiles]
    assert sizes == [(1080, 2200)] * 3
    assert all(t.startswith(b"\x89PNG") for t in tiles)


def test_tile_just_over_limit() -> None:
    tiles = tile_image(png(720, 2700))
    assert [Image.open(io.BytesIO(t)).size for t in tiles] == [(720, 2200), (720, 2200)]


def test_tile_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        tile_image(b"not an image")


# --------------------------------------------------------------------------- validation


def test_validation_cleans_and_drops() -> None:
    ex = parse_extraction({
        "source_app": "Yandex Music", "page_type": "weird", "context": None,
        "items": [
            {"artist_raw": "  Кино  ", "title_raw": " Группа   крови ", "conf": 1.7},
            {"artist_raw": "Nobody", "title_raw": "   ", "conf": 0.9},
            {"artist_raw": "X", "conf": 0.9},
            "garbage",
            {"artist": "Сплин", "title": "Романс", "conf": "85", "truncated": "true"},
            {"artist_raw": "Мумий Тролль", "title_raw": "Владивосток 20…", "conf": None, "duration": 185},
        ],
    })
    assert ex.source_app == "yandex_music"
    assert ex.page_type == "other"
    assert ex.context.is_empty()
    assert [(i.artist_raw, i.title_raw) for i in ex.items] == [
        ("Кино", "Группа крови"), ("Сплин", "Романс"), ("Мумий Тролль", "Владивосток 20…"),
    ]
    assert ex.items[0].conf == 1.0
    assert ex.items[1].conf == pytest.approx(0.85) and ex.items[1].truncated is True
    assert ex.items[2].truncated is True and ex.items[2].conf == 0 and ex.items[2].duration == "3:05"


def test_validation_non_dict() -> None:
    assert parse_extraction(["x"]).items == []


# --------------------------------------------------------------------------- merge / dedupe


def item(a: str, t: str, **kw) -> ExtractedItem:
    return ExtractedItem(artist_raw=a, title_raw=t, **kw)


def test_norm_key() -> None:
    assert norm_key("Ёлка — «Прованс»!") == "елка прованс"
    assert norm_key("Владивосток 20…") == "владивосток 20"


def test_dedupe_exact_and_normalized() -> None:
    items = [item("Кино", "Группа крови", conf=0.8), item("Сплин", "Романс"),
             item("КИНО", "Группа  крови!", conf=0.95, duration="4:44")]
    out = dedupe_items(items)
    assert [(i.artist_raw, i.title_raw) for i in out] == [("Кино", "Группа крови"), ("Сплин", "Романс")]
    assert out[0].conf == 0.95 and out[0].duration == "4:44"


def test_dedupe_truncated_keeps_full_in_first_position() -> None:
    items = [item("Мумий Тролль", "Владивосток 20…", truncated=True), item("Земфира", "Хочешь?"),
             item("Мумий Тролль", "Владивосток 2000")]
    out = dedupe_items(items)
    assert [i.title_raw for i in out] == ["Владивосток 2000", "Хочешь?"]
    assert out[0].truncated is False


def test_dedupe_truncated_artist_and_e_yo() -> None:
    out = dedupe_items([item("Мияги & Энд…", "Самурай", truncated=True), item("Мияги & Эндшпиль", "Самурай")])
    assert len(out) == 1 and out[0].artist_raw == "Мияги & Эндшпиль"
    out = dedupe_items([item("Ёлка", "Прованс"), item("Елка", "Прованс")])
    assert len(out) == 1


def test_dedupe_does_not_merge_different_tracks() -> None:
    out = dedupe_items([item("Кино", "Звезда по имени Солнце"), item("Кино", "Звезда"), item("ДДТ", "Звезда")])
    assert len(out) == 3
    # a too short truncated prefix (< 3 chars) does not swallow other rows
    out = dedupe_items([item("Кино", "Зв…", truncated=True), item("Кино", "Звезда")])
    assert len(out) == 2


def test_merge_context_from_first_tile_with_it() -> None:
    a = Extraction(items=[item("Кино", "Группа крови")])
    b = Extraction(source_app="vk", page_type="playlist", context={"playlist_name": "Рок"},
                   items=[item("Кино", "Группа крови"), item("Сплин", "Романс")])
    c = Extraction(source_app="spotify", context={"playlist_name": "Другой"}, items=[item("ДДТ", "Осень")])
    m = merge_extractions([a, b, c])
    assert m.source_app == "vk" and m.page_type == "playlist" and m.context.playlist_name == "Рок"
    assert [i.title_raw for i in m.items] == ["Группа крови", "Романс", "Осень"]


# --------------------------------------------------------------------------- LLM client (mocked endpoint)


def test_completion_limit_uses_openai_parameter() -> None:
    assert completion_limit("https://api.openai.com/v1", 2500) == {"max_completion_tokens": 2500}
    assert completion_limit("https://openrouter.ai/api/v1", 2500) == {"max_tokens": 2500}
    assert completion_limit("http://host.docker.internal:11434/v1", 2500) == {"max_tokens": 2500}


def test_image_part_and_configured() -> None:
    part = image_part(b"\x89PNGdata")
    assert part["type"] == "image_url"
    assert part["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"\x89PNGdata").decode()
    assert configured(settings()) is True
    assert configured(settings(llm_api_key="")) is False
    assert configured(settings(llm_api_key="", llm_base_url="http://host.docker.internal:11434/v1")) is True


def test_extract_json_object() -> None:
    assert extract_json_object('Sure! ```json\n{"a": {"b": [1, "}"]}}\n``` done') == {"a": {"b": [1, "}"]}}
    assert extract_json_object("no json {here") is None


def test_usage_add() -> None:
    u = LLMUsage(tokens_in=1, tokens_out=2, cost_usd=0.1) + LLMUsage(tokens_in=3, tokens_out=4, cost_usd=0.2)
    assert (u.tokens_in, u.tokens_out) == (4, 6) and u.cost_usd == pytest.approx(0.3)


@respx.mock
async def test_extract_from_images_request_shape_and_merge() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion(tool_args=TRACKS_ARGS)))
    async with LLMClient(settings()) as llm:
        ex, usage = await extract_from_images([png(1080, 2000), png(1080, 2000)], llm)

    assert route.call_count == 2
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["model"] == "anthropic/claude-haiku-5.5"
    assert body["temperature"] == 0
    assert body["tool_choice"] == {"type": "function", "function": {"name": "report_tracks"}}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "report_tracks"
    assert body["tools"][0]["function"]["parameters"] == REPORT_TRACKS_SCHEMA
    assert body["usage"] == {"include": True}  # OpenRouter: report cost
    sys_msg, user_msg = body["messages"]
    assert sys_msg["role"] == "system" and "DATA" in sys_msg["content"]
    assert user_msg["content"][0]["type"] == "text"
    img = user_msg["content"][1]
    assert img["type"] == "image_url" and img["image_url"]["url"].startswith("data:image/png;base64,")
    assert req.headers["authorization"] == "Bearer test-key"
    assert req.headers["http-referer"] == "https://github.com/homemusic"
    assert req.headers["x-title"] == "HomeMusic"

    # the same two rows on both screenshots are deduplicated
    assert [i.title_raw for i in ex.items] == ["Группа крови", "Выхода нет"]
    assert ex.source_app == "yandex_music" and ex.context.playlist_name == "Мой рок"
    assert usage.tokens_in == 6800 and usage.tokens_out == 400
    assert usage.cost_usd == pytest.approx(2 * (3400 * 0.10 + 200 * 0.50) / 1e6)


@respx.mock
async def test_tall_screenshot_is_tiled() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion(tool_args=TRACKS_ARGS)))
    async with LLMClient(settings()) as llm:
        ex, _ = await extract_from_images([png(1080, 5000)], llm)
    assert route.call_count == 3
    assert len(ex.items) == 2


@respx.mock
async def test_openrouter_cost_is_preferred() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "cost": 0.00042}
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion(tool_args=TRACKS_ARGS, usage=usage)))
    async with LLMClient(settings()) as llm:
        _, u = await extract_from_images([png(100, 100)], llm)
    assert u.cost_usd == pytest.approx(0.00042)


@respx.mock
async def test_fallback_to_json_schema_then_cached() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "this model does not support tools"}})
        return httpx.Response(200, json=completion(content=json.dumps(TRACKS_ARGS), finish="stop"))

    respx.post(URL).mock(side_effect=handler)
    async with LLMClient(settings()) as llm:
        ex, _ = await extract_from_images([png(100, 100)], llm)
        assert len(ex.items) == 2
        assert calls[1]["response_format"]["type"] == "json_schema"
        assert calls[1]["response_format"]["json_schema"]["schema"] == REPORT_TRACKS_SCHEMA
        await extract_from_images([png(100, 100)], llm)
    assert len(calls) == 3  # second run starts directly with json_schema
    assert "tools" not in calls[2]


@respx.mock
async def test_fallback_to_plain_json_text() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body or "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported"}})
        text = "Here you go:\n```json\n" + json.dumps(TRACKS_ARGS, ensure_ascii=False) + "\n```"
        return httpx.Response(200, json=completion(content=text, finish="stop"))

    respx.post(URL).mock(side_effect=handler)
    async with LLMClient(settings()) as llm:
        ex, _ = await extract_from_images([png(100, 100)], llm)
    assert len(calls) == 3
    assert "Respond with ONLY one JSON object" in calls[2]["messages"][0]["content"]
    assert [i.artist_raw for i in ex.items] == ["Кино", "Сплин"]


@respx.mock
async def test_tool_ignored_but_json_in_content() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion(content=json.dumps(TRACKS_ARGS))))
    async with LLMClient(settings()) as llm:
        ex, _ = await extract_from_images([png(100, 100)], llm)
    assert len(ex.items) == 2


@respx.mock
async def test_auth_error_is_llm_error() -> None:
    respx.post(URL).mock(return_value=httpx.Response(401, json={"error": {"message": "No auth"}}))
    async with LLMClient(settings()) as llm:
        with pytest.raises(LLMError):
            await extract_from_images([png(100, 100)], llm)


async def test_not_configured() -> None:
    async with LLMClient(settings(llm_api_key="")) as llm:
        with pytest.raises(LLMError):
            await extract_from_images([png(100, 100)], llm)


# --------------------------------------------------------------------------- text requests


def test_track_list_fast_path() -> None:
    rows = parse_track_list("Кино - Группа крови\n2. Сплин — Выхода нет 4:03\n\n• Земфира – Хочешь?\n")
    assert rows is not None
    assert [(r.artist_raw, r.title_raw, r.duration) for r in rows] == [
        ("Кино", "Группа крови", None), ("Сплин", "Выхода нет", "4:03"), ("Земфира", "Хочешь?", None),
    ]
    assert parse_track_list("скачай Кино - Группа крови") is None
    assert parse_track_list("привет\nкак дела\nKino - Blood type") is None


@respx.mock
async def test_parse_text_fast_path_no_llm() -> None:
    route = respx.post(URL)
    async with LLMClient(settings()) as llm:
        intent, usage = await parse_text_request("Кино - Группа крови\nСплин - Романс", llm)
    assert route.call_count == 0
    assert intent.kind == "tracks" and len(intent.tracks) == 2
    assert usage == LLMUsage()


@respx.mock
async def test_parse_text_latest_album() -> None:
    args = {"kind": "latest_album", "artist": "«Скриптонит»", "album": None, "tracks": [], "playlist_name": None,
            "record_types": ["album", "bogus"]}
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json=completion(tool_args=args, tool_name="report_intent")))
    async with LLMClient(settings()) as llm:
        intent, usage = await parse_text_request("скачай последний альбом Скриптонита", llm)
    body = json.loads(route.calls[0].request.content)
    assert body["tool_choice"]["function"]["name"] == "report_intent"
    assert "скачай последний альбом Скриптонита" in body["messages"][1]["content"]
    assert intent == TextIntent(kind="latest_album", artist="Скриптонит", record_types=["album"])
    assert usage.tokens_in == 3400


@respx.mock
async def test_parse_text_sends_all_5000_characters_to_model() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(
        200, json=completion(tool_args={"kind": "unknown"}, tool_name="report_intent")))
    text = "Ж" * 4997 + "END"
    async with LLMClient(settings()) as llm:
        await parse_text_request(text, llm)
    body = json.loads(route.calls[0].request.content)
    assert body["messages"][1]["content"] == f"User request:\n<<<\n{text}\n>>>"


@respx.mock
async def test_parse_text_tracks_and_playlist() -> None:
    args = {"kind": "tracks", "tracks": [{"artist_raw": "Кино", "title_raw": "Группа крови"},
                                         {"artist_raw": "Кино", "title_raw": "Пачка сигарет"}],
            "playlist_name": "Рок"}
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion(tool_args=args, tool_name="report_intent")))
    async with LLMClient(settings()) as llm:
        intent, _ = await parse_text_request("добавь Кино — Группа крови и Пачка сигарет в плейлист Рок", llm)
    assert intent.kind == "tracks" and intent.playlist_name == "Рок"
    assert [(t.artist_raw, t.title_raw, t.conf) for t in intent.tracks] == [
        ("Кино", "Группа крови", 1.0), ("Кино", "Пачка сигарет", 1.0),
    ]


@respx.mock
async def test_parse_text_garbage_kind() -> None:
    respx.post(URL).mock(return_value=httpx.Response(
        200, json=completion(tool_args={"kind": "hack_the_planet"}, tool_name="report_intent")))
    async with LLMClient(settings()) as llm:
        intent, _ = await parse_text_request("что-то непонятное", llm)
    assert intent.kind == "unknown"
