"""OpenAI-compatible LLM client (PLAN.md 3.6, 8.5.1, 8.5.6): OpenRouter by default, any `base_url` + `model` works.

Structured output is obtained by a forced tool call. Providers that ignore tool calling (some local models) get two
fallbacks: `response_format=json_schema`, then a plain "return only JSON" instruction with the first JSON object
extracted from the text. The working mode is remembered per (base_url, model) so later calls skip failed attempts.

The openai SDK talks through a legacy `httpx.AsyncClient` (accepted by the SDK), so the outbound proxy can be mounted
the same way as in `app.net` and tests can mock the endpoint with respx.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

OPENROUTER_HEADERS = {"HTTP-Referer": "https://github.com/homemusic", "X-Title": "HomeMusic"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}
OLLAMA_PORT = 11434

Mode = Literal["tools", "json_schema", "plain"]
_MODES: tuple[Mode, ...] = ("tools", "json_schema", "plain")
# (base_url, model) -> first mode that is known to work; shared by all clients in the process
_mode_cache: dict[tuple[str, str], Mode] = {}


class LLMUsage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
        )


def usage_of(resp: Any, settings: Settings | None = None) -> LLMUsage:
    """Tokens and cost of one completion: OpenRouter reports the real cost (usage.include), else the configured price."""
    u = getattr(resp, "usage", None)
    if u is None:
        return LLMUsage()
    settings = settings or get_settings()
    t_in = int(getattr(u, "prompt_tokens", 0) or 0)
    t_out = int(getattr(u, "completion_tokens", 0) or 0)
    cost = (getattr(u, "model_extra", None) or {}).get("cost")
    if not isinstance(cost, (int, float)):
        cost = t_in * settings.llm_price_in / 1e6 + t_out * settings.llm_price_out / 1e6
    return LLMUsage(tokens_in=t_in, tokens_out=t_out, cost_usd=round(float(cost), 8))


class LLMError(Exception):
    """The model could not be called or did not return usable structured output."""


class _ModeUnsupported(Exception):
    """This structured-output mode does not work with the provider; try the next one."""


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_openrouter(base_url: str) -> bool:
    return _host(base_url).endswith("openrouter.ai")


def completion_limit(base_url: str, limit: int) -> dict[str, int]:
    """Token cap for one chat completion.

    GPT-5 on api.openai.com rejects `max_tokens` and requires `max_completion_tokens`.
    OpenRouter, Ollama and the other OpenAI-compatible servers still take `max_tokens`.
    """
    if _host(base_url) == "api.openai.com":
        return {"max_completion_tokens": limit}
    return {"max_tokens": limit}


def is_local(base_url: str) -> bool:
    """A local OpenAI-compatible server (Ollama and friends) that needs no API key."""
    p = urlparse(base_url)
    return (p.hostname or "").lower() in _LOCAL_HOSTS or p.port == OLLAMA_PORT


def configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.llm_api_key.strip()) or (bool(s.llm_base_url) and is_local(s.llm_base_url))


def image_part(data: bytes, mime: str = "image/png") -> dict:
    """Chat-completions image content part with the image inlined as base64 (full resolution, no downscaling)."""
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def extract_json_object(text: str) -> dict | None:
    """First JSON object found in `text` (handles ```json fences and prose around it)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            return obj
        pos = text.find("{", pos + 1)
    return None


def _parse_arguments(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return extract_json_object(raw)
    return obj if isinstance(obj, dict) else None


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = s = settings or get_settings()
        self.base_url = s.llm_base_url.rstrip("/")
        self.model = s.llm_model
        host = _host(self.base_url)
        proxy = s.outbound_proxy if s.outbound_proxy and host in s.proxy_hosts else None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(s.llm_timeout_s, connect=15),
            proxy=proxy,
            follow_redirects=True,
        )
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=s.llm_api_key.strip() or "not-needed",
            timeout=s.llm_timeout_s,
            max_retries=2,
            http_client=self._http,
            default_headers=OPENROUTER_HEADERS if is_openrouter(self.base_url) else None,
        )

    async def aclose(self) -> None:
        await self._client.close()
        await self._http.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ public API

    async def call_tool(
        self,
        *,
        system: str,
        user_content: list[dict] | str,
        tool_name: str,
        tool_description: str,
        parameters: dict,
        max_tokens: int = 4000,
    ) -> tuple[dict, LLMUsage]:
        """Force the model to "call" `tool_name` and return its parsed JSON arguments plus token usage/cost."""
        if not configured(self.settings):
            raise LLMError("ИИ не настроен: задайте LLM_API_KEY (или локальный LLM_BASE_URL) в .env")
        key = (self.base_url, self.model)
        start = _MODES.index(_mode_cache.get(key, "tools"))
        usage = LLMUsage()
        last_err: Exception | None = None
        only_rejections = True  # remember a downgrade only when the server rejected the request outright
        for mode in _MODES[start:]:
            try:
                result, u = await self._call_mode(mode, system, user_content, tool_name, tool_description,
                                                  parameters, max_tokens)
            except _ModeUnsupported as e:
                soft_usage = getattr(e, "usage", None)
                if soft_usage is not None:  # the model answered, just without usable JSON (may be a one-off)
                    usage = usage + soft_usage
                    only_rejections = False
                log.info("llm %s: mode %s unsupported (%s), trying the next one", self.model, mode, e)
                last_err = e
                continue
            usage = usage + u
            if only_rejections and _mode_cache.get(key) != mode:
                _mode_cache[key] = mode
            log.info(
                "llm %s tool=%s mode=%s tokens_in=%d tokens_out=%d cost=$%.6f",
                self.model, tool_name, mode, usage.tokens_in, usage.tokens_out, usage.cost_usd,
            )
            return result, usage
        raise LLMError(f"Модель не вернула структурированный ответ: {last_err}")

    # ------------------------------------------------------------------ internals

    def _usage(self, resp: Any) -> LLMUsage:
        return usage_of(resp, self.settings)

    async def _create(self, **kwargs: Any) -> Any:
        extra_body = {"usage": {"include": True}} if is_openrouter(self.base_url) else None
        try:
            return await self._client.chat.completions.create(
                model=self.model, temperature=0, extra_body=extra_body, **kwargs
            )
        except (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError) as e:
            # typical answers of servers that do not implement tools / json_schema
            raise _ModeUnsupported(f"HTTP {e.status_code}: {_short(e)}") from e
        except openai.AuthenticationError as e:
            raise LLMError("Неверный LLM_API_KEY или нет доступа к модели") from e
        except openai.PermissionDeniedError as e:
            raise LLMError(f"Провайдер ИИ отказал в доступе: {_short(e)}") from e
        except openai.RateLimitError as e:
            raise LLMError("Провайдер ИИ ограничил частоту запросов, попробуйте позже") from e
        except openai.APITimeoutError as e:
            raise LLMError("Провайдер ИИ не ответил вовремя") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"Нет связи с провайдером ИИ: {e}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"Ошибка провайдера ИИ (HTTP {e.status_code}): {_short(e)}") from e

    async def _call_mode(
        self,
        mode: Mode,
        system: str,
        user_content: list[dict] | str,
        tool_name: str,
        tool_description: str,
        parameters: dict,
        max_tokens: int,
    ) -> tuple[dict, LLMUsage]:
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        kwargs: dict[str, Any] = {"messages": messages, **completion_limit(self.base_url, max_tokens)}
        if mode == "tools":
            kwargs["tools"] = [{
                "type": "function",
                "function": {"name": tool_name, "description": tool_description, "parameters": parameters},
            }]
            kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_name}}
        elif mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": tool_name, "description": tool_description, "schema": parameters,
                                "strict": False},
            }
        else:
            messages[0] = {"role": "system", "content": system + _plain_json_instruction(tool_description, parameters)}

        resp = await self._create(**kwargs)
        usage = self._usage(resp)
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        msg = choice.message if choice else None
        if msg is None:
            raise _with_usage(_ModeUnsupported("empty response"), usage)

        for call in msg.tool_calls or []:
            fn = getattr(call, "function", None)
            if fn is not None and (fn.name == tool_name or len(msg.tool_calls or []) == 1):
                args = _parse_arguments(fn.arguments)
                if args is not None:
                    return args, usage
        # no (usable) tool call: many servers answer with the JSON in the text instead
        args = extract_json_object(msg.content or "")
        if args is not None:
            return args, usage
        if choice is not None and choice.finish_reason == "length":
            raise LLMError("Ответ модели обрезан (лимит токенов)")
        raise _with_usage(_ModeUnsupported(f"no JSON in the answer: {(msg.content or '')[:120]!r}"), usage)


def _with_usage(e: _ModeUnsupported, usage: LLMUsage) -> _ModeUnsupported:
    e.usage = usage  # type: ignore[attr-defined]
    return e


def _short(e: openai.APIStatusError) -> str:
    body = e.body
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(e)[:300]


def _plain_json_instruction(tool_description: str, parameters: dict) -> str:
    return (
        "\n\n# Output format\n"
        f"Task of the answer: {tool_description}\n"
        "Respond with ONLY one JSON object (no prose, no markdown fences) that matches this JSON Schema:\n"
        f"{json.dumps(parameters, ensure_ascii=False)}"
    )
