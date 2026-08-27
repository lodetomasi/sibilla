"""Logging strutturato con redaction dei secret (sez. 52: no private keys in logs)."""
from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|passwd|private[_-]?key|ssoid)"
               r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\",;}]{4,})"),
    re.compile(r"0x[a-fA-F0-9]{60,}"),  # private key esadecimale
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
]

_SENSITIVE_KEYS = {
    "api_key", "apikey", "anthropic_api_key", "app_key", "token", "secret", "secret_key",
    "password", "passwd", "private_key", "session_token", "ssoid", "authorization",
    "newsapi_key", "telegram_bot_token", "slack_webhook_url", "cookie",
}

REDACTED = "***REDACTED***"


def scrub_text(value: str) -> str:
    out = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS else scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(v) for v in value)
    return value


def _redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (REDACTED if k.lower() in _SENSITIVE_KEYS else scrub_value(v))
        for k, v in event_dict.items()
    }


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_processor,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
