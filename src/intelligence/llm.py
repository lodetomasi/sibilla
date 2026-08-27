"""Client LLM su OpenRouter (API OpenAI-compatible).

Copre: structured output (json_schema o tool-call forzato per i modelli senza
`structured_outputs`), loop agentico con tool tipizzati (sez. 20/21), budget e
cost tracking (sez. 36/40/41), logging completo in `llm_decisions`.

Iron rule: gli agenti non vedono i secret. Il client riceve la chiave dalla
config e non la espone mai nei payload che passano agli agenti o nei log.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from core.clock import utcnow
from core.config import LLMConfig, LLMRole, get_settings
from core.db import session_scope
from core.errors import LLMBudgetExceeded, LLMError, LLMOutputError, ToolPermissionError
from core.logging import get_logger, scrub_value
from core.repository import Repository

log = get_logger("intelligence.llm")

T = TypeVar("T", bound=BaseModel)
ToolFn = Callable[..., Awaitable[Any] | Any]


@dataclass
class ToolSpec:
    """Tool tipizzato esposto a un agente (sez. 20)."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn
    allowed_roles: tuple[str, ...] = ()  # vuoto = tutti

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class LLMResult:
    content: str | None
    parsed: Any | None
    model: str
    role_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    tools_used: list[str] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 1
    raw: dict[str, Any] = field(default_factory=dict)
    decision_id: int | None = None


class LLMBudget:
    """Budget giornaliero + rate limit orario (sez. 41)."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._spent_today = 0.0
        self._day = utcnow().date()
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def sync_from_db(self) -> None:
        try:
            start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            async with session_scope() as session:
                repo = Repository(session)
                self._spent_today = await repo.llm_cost_since(start)
                self._calls = [time.time()] * min(
                    self.config.max_calls_per_hour,
                    await repo.llm_calls_since(utcnow() - timedelta(hours=1)),
                )
        except Exception as exc:  # noqa: BLE001 - il budget non deve bloccare per DB assente
            log.warning("llm.budget.sync_failed", error=str(exc)[:120])

    async def check(self, estimated_cost: float = 0.0) -> None:
        async with self._lock:
            today = utcnow().date()
            if today != self._day:
                self._day = today
                self._spent_today = 0.0
            if self._spent_today + estimated_cost > self.config.daily_budget_usd:
                raise LLMBudgetExceeded(
                    f"budget LLM giornaliero esaurito: {self._spent_today:.2f} USD "
                    f"/ {self.config.daily_budget_usd:.2f}"
                )
            now = time.time()
            self._calls = [t for t in self._calls if now - t < 3600]
            if len(self._calls) >= self.config.max_calls_per_hour:
                raise LLMBudgetExceeded("limite chiamate LLM/ora raggiunto")
            self._calls.append(now)

    async def record(self, cost: float) -> None:
        async with self._lock:
            self._spent_today += cost

    @property
    def spent_today(self) -> float:
        return self._spent_today


class LLMClient:
    """Chiamate a OpenRouter con schema, tool loop e logging."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        budget: LLMBudget | None = None,
        persist: bool = True,
    ):
        self.config = config or get_settings().llm
        self.budget = budget or LLMBudget(self.config)
        self.persist = persist
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=self.config.openrouter_base_url,
            timeout=httpx.Timeout(self.config.request_timeout_s, connect=20.0),
        )

    # ------------------------------------------------------------------ utils
    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise LLMError("OpenRouter API key mancante (ATS_OPENROUTER_API_KEY)")
        return {
            "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.config.app_url,
            "X-Title": self.config.app_title,
        }

    def cost_for(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.config.price_for(model)
        return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out

    def role(self, name: str) -> LLMRole:
        roles = self.config.roles()
        if name not in roles:
            raise LLMError(f"ruolo LLM sconosciuto: {name}")
        return roles[name]

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(4):
            try:
                response = await self._http.post(
                    "/chat/completions", json=payload, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if response.status_code in (429, 500, 502, 503, 504, 524):
                last = LLMError(f"OpenRouter HTTP {response.status_code}: {response.text[:200]}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if response.status_code >= 400:
                raise LLMError(f"OpenRouter HTTP {response.status_code}: {response.text[:400]}")
            data = response.json()
            if "error" in data and not data.get("choices"):
                message = str(data["error"].get("message", data["error"]))
                if attempt < 3 and any(k in message.lower() for k in ("rate", "overloaded", "timeout")):
                    last = LLMError(message)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"OpenRouter error: {message[:400]}")
            return data
        raise LLMError(f"OpenRouter non raggiungibile: {last}")

    @staticmethod
    def _response_format(schema_model: type[BaseModel], name: str) -> dict[str, Any]:
        schema = schema_model.model_json_schema()
        _strictify(schema)
        return {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        }

    @staticmethod
    def _schema_tool(schema_model: type[BaseModel], name: str) -> dict[str, Any]:
        schema = schema_model.model_json_schema()
        _strictify(schema)
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": "Restituisci la risposta finale strutturata.",
                "parameters": schema,
            },
        }

    def _reasoning(self, role: LLMRole) -> dict[str, Any] | None:
        if not role.reasoning_effort:
            return None
        effort = role.reasoning_effort
        if effort == "none":
            # filtro ad alto volume: niente thinking, solo classificazione veloce
            return {"enabled": False}
        # OpenRouter accetta low/medium/high; "pro"/"max" mappano su high
        if effort in ("pro", "max"):
            effort = "high"
        return {"effort": effort}

    # ------------------------------------------------------------- chiamate
    async def complete(
        self,
        role_name: str,
        messages: list[dict[str, Any]],
        *,
        schema: type[T] | None = None,
        tools: list[ToolSpec] | None = None,
        signal_id: int | None = None,
        context: dict[str, Any] | None = None,
        model_override: str | None = None,
    ) -> LLMResult:
        """Esegue una conversazione (con eventuale tool loop) e ritorna il risultato.

        Se `schema` e dato, l'output finale viene validato con Pydantic; in caso di
        errore si ritenta chiedendo la correzione (sez. 79: structured-output tests).
        """
        role = self.role(role_name)
        try:
            return await asyncio.wait_for(
                self._complete(role_name, role, messages, schema=schema, tools=tools, signal_id=signal_id, context=context, model_override=model_override),
                timeout=role.timeout_s,
            )
        except TimeoutError as exc:
            log.warning("llm.timeout", role=role_name, model=model_override or role.model, timeout_s=role.timeout_s)
            await self._persist(role_name=role_name, model=model_override or role.model, signal_id=signal_id, inputs={"context": context or {}}, output={}, tools_used=[], latency_ms=role.timeout_s * 1000, input_tokens=0, output_tokens=0, cost_usd=0.0, error=f"timeout {role.timeout_s}s", confidence=None)
            raise LLMError(f"{role_name} ({role.model}) oltre il timeout di {role.timeout_s}s") from exc

    async def _complete(
        self,
        role_name: str,
        role: LLMRole,
        messages: list[dict[str, Any]],
        *,
        schema: type[T] | None,
        tools: list[ToolSpec] | None,
        signal_id: int | None,
        context: dict[str, Any] | None,
        model_override: str | None,
    ) -> LLMResult:
        model = model_override or role.model
        started = time.perf_counter()
        history = list(messages)
        tools = tools or []
        allowed_tools = [t for t in tools if not t.allowed_roles or role_name in t.allowed_roles]
        tool_map = {t.name: t for t in allowed_tools}
        tools_used: list[str] = []
        tool_trace: list[dict[str, Any]] = []
        total_in = total_out = 0
        total_cost = 0.0
        turns = 0
        max_turns = max(1, role.max_tool_turns + 1) if allowed_tools else 1
        schema_name = schema.__name__ if schema else "final_answer"
        use_schema_tool = schema is not None and not role.supports_json_schema
        parsed: Any | None = None
        content: str | None = None
        retries_left = self.config.max_structured_retries
        length_retries = 0
        error: str | None = None
        raw_last: dict[str, Any] = {}

        await self.budget.check()

        try:
            while turns < max_turns + retries_left + 1:
                turns += 1
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": history,
                    "temperature": role.temperature,
                    "max_tokens": role.max_output_tokens,
                    "usage": {"include": True},
                }
                reasoning = self._reasoning(role)
                if reasoning:
                    payload["reasoning"] = reasoning
                request_tools = [t.as_openai() for t in allowed_tools]
                if use_schema_tool:
                    request_tools.append(self._schema_tool(schema, schema_name))  # type: ignore[arg-type]
                if request_tools:
                    payload["tools"] = request_tools
                    # ultimo turno consentito: forza la risposta finale
                    if use_schema_tool and turns >= max_turns:
                        payload["tool_choice"] = {
                            "type": "function",
                            "function": {"name": schema_name},
                        }
                    else:
                        payload["tool_choice"] = "auto"
                if schema is not None and not use_schema_tool:
                    payload["response_format"] = self._response_format(schema, schema_name)

                try:
                    data = await self._post(payload)
                except LLMError as exc:
                    if "Invalid schema" in str(exc) and schema is not None and not use_schema_tool:
                        # provider in strict mode che rifiuta lo schema: si passa al tool call forzato
                        log.warning("llm.schema_rejected_fallback_tool", model=model, error=str(exc)[:160])
                        use_schema_tool = True
                        turns -= 1
                        continue
                    raise
                raw_last = data
                usage = data.get("usage") or {}
                total_in += int(usage.get("prompt_tokens") or 0)
                total_out += int(usage.get("completion_tokens") or 0)
                reported_cost = usage.get("cost")
                total_cost = (
                    float(reported_cost) + total_cost
                    if reported_cost is not None
                    else self.cost_for(model, total_in, total_out)
                )
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                tool_calls = message.get("tool_calls") or []
                if str(choice.get("finish_reason") or "").lower() == "length" and not tool_calls:
                    # output troncato (reasoning + JSON oltre max_tokens): si riprova con budget doppio,
                    # senza consumare i retry di schema
                    length_retries += 1
                    if length_retries <= 2:
                        role = role.model_copy(update={"max_output_tokens": role.max_output_tokens * 2})
                        log.warning("llm.output_truncated_retry", model=model, max_tokens=role.max_output_tokens)
                        turns -= 1
                        continue
                history.append(_strip_message(message))

                if tool_calls:
                    final_call = None
                    for call in tool_calls:
                        fn = call.get("function") or {}
                        name = fn.get("name")
                        args = _parse_args(fn.get("arguments"))
                        if use_schema_tool and name == schema_name:
                            final_call = args
                            continue
                        result = await self._run_tool(role_name, tool_map, name, args)
                        tools_used.append(str(name))
                        tool_trace.append({"tool": name, "args": args, "result": _truncate(result)})
                        history.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "content": json.dumps(result, default=str)[:12000],
                            }
                        )
                    if final_call is not None:
                        try:
                            parsed = schema.model_validate(final_call)  # type: ignore[union-attr]
                            content = json.dumps(final_call, default=str)
                            break
                        except ValidationError as exc:
                            retries_left -= 1
                            if retries_left < 0:
                                raise LLMOutputError(f"output non valido: {exc}") from exc
                            history.append(
                                {
                                    "role": "user",
                                    "content": f"Output non valido rispetto allo schema: {exc}. "
                                    "Richiama il tool finale con dati corretti.",
                                }
                            )
                    continue

                content = message.get("content")
                if schema is None:
                    break
                if use_schema_tool:
                    # il modello ha risposto in testo invece di chiamare il tool finale:
                    # se il testo e' gia JSON valido per lo schema lo accettiamo, altrimenti retry
                    try:
                        parsed = schema.model_validate(_extract_json(content))
                        break
                    except (ValidationError, ValueError, json.JSONDecodeError):
                        pass
                    retries_left -= 1
                    if retries_left < 0:
                        raise LLMOutputError("il modello non ha prodotto l'output strutturato")
                    history.append(
                        {
                            "role": "user",
                            "content": f"Devi rispondere chiamando il tool `{schema_name}` con l'output finale.",
                        }
                    )
                    continue
                try:
                    parsed = schema.model_validate(_extract_json(content))
                    break
                except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                    retries_left -= 1
                    if retries_left < 0:
                        raise LLMOutputError(f"output non valido: {exc}") from exc
                    history.append(
                        {
                            "role": "user",
                            "content": f"Il JSON non rispetta lo schema: {exc}. Rispondi solo con JSON valido.",
                        }
                    )
            else:
                if schema is not None and parsed is None:
                    raise LLMOutputError("turni esauriti senza output strutturato")
        except LLMError as exc:
            error = str(exc)
            raise
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            await self.budget.record(total_cost)
            decision_id = await self._persist(
                role_name=role_name,
                model=model,
                signal_id=signal_id,
                inputs={"messages": _truncate(messages, 6000), "context": context or {}},
                output=parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else {"content": content},
                tools_used=tools_used,
                latency_ms=latency_ms,
                input_tokens=total_in,
                output_tokens=total_out,
                cost_usd=total_cost,
                error=error,
                confidence=_confidence_of(parsed),
            )
        return LLMResult(
            content=content,
            parsed=parsed,
            model=model,
            role_name=role_name,
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            tools_used=tools_used,
            tool_trace=tool_trace,
            turns=turns,
            raw={"id": raw_last.get("id"), "provider": raw_last.get("provider")},
            decision_id=decision_id,
        )

    async def _run_tool(
        self, role_name: str, tool_map: dict[str, ToolSpec], name: str | None, args: dict[str, Any]
    ) -> Any:
        if not name or name not in tool_map:
            # Sez. 21: nessuna esecuzione free-form; tool sconosciuto = errore esplicito
            raise ToolPermissionError(f"tool non autorizzato o inesistente: {name}")
        spec = tool_map[name]
        if spec.allowed_roles and role_name not in spec.allowed_roles:
            raise ToolPermissionError(f"tool {name} non consentito al ruolo {role_name}")
        try:
            result = spec.fn(**args)
            if inspect.isawaitable(result):
                result = await result
            return scrub_value(result)
        except TypeError as exc:
            return {"error": f"argomenti non validi per {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - l'agente deve vedere l'errore, non morire
            log.warning("llm.tool.error", tool=name, error=str(exc)[:200])
            return {"error": str(exc)[:300]}

    async def _persist(self, **kwargs: Any) -> int | None:
        if not self.persist:
            return None
        try:
            async with session_scope() as session:
                repo = Repository(session)
                decision = await repo.add_llm_decision(
                    signal_id=kwargs["signal_id"],
                    agent=kwargs["role_name"],
                    model=kwargs["model"],
                    model_version=kwargs["model"],
                    prompt_version=self.config.prompt_version,
                    tools_used=kwargs["tools_used"],
                    inputs=scrub_value(kwargs["inputs"]),
                    structured_output=scrub_value(kwargs["output"] or {}),
                    confidence=kwargs["confidence"],
                    latency_ms=kwargs["latency_ms"],
                    input_tokens=kwargs["input_tokens"],
                    output_tokens=kwargs["output_tokens"],
                    cost_usd=kwargs["cost_usd"],
                    error=kwargs["error"],
                )
                await repo.add_cost(
                    kind="llm",
                    provider=kwargs["model"],
                    amount_usd=kwargs["cost_usd"],
                    units=kwargs["input_tokens"] + kwargs["output_tokens"],
                    details={"agent": kwargs["role_name"]},
                )
                return decision.id
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.persist_failed", error=str(exc)[:160])
            return None

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


# --------------------------------------------------------------------- helpers
def _strictify(schema: dict[str, Any]) -> None:
    """Rende lo schema compatibile con strict mode: additionalProperties=false ovunque,
    tutte le proprieta required (gli opzionali restano nullable via anyOf)."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False
        props = schema.get("properties", {})
        schema["required"] = list(props.keys())
        for prop in props.values():
            _strictify(prop)
            prop.pop("default", None)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []) or []:
            _strictify(sub)
    if "items" in schema and isinstance(schema["items"], dict):
        _strictify(schema["items"])
    for definition in (schema.get("$defs") or {}).values():
        _strictify(definition)
    schema.pop("default", None)
    schema.pop("title", None)


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {"_raw": str(raw)}


def _extract_json(content: str | None) -> Any:
    if not content:
        raise ValueError("risposta vuota")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1])


def _strip_message(message: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.get("role", "assistant")}
    if message.get("content") is not None:
        out["content"] = message["content"]
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    if "content" not in out and "tool_calls" not in out:
        out["content"] = ""
    return out


def _truncate(value: Any, limit: int = 4000) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {"_truncated": text[:limit]}


def _confidence_of(parsed: Any) -> float | None:
    if parsed is None:
        return None
    for attr in ("confidence", "critic_score", "relevance"):
        value = getattr(parsed, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    global _client
    _client = client
