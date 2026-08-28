from __future__ import annotations

import os

import pytest

from core.config import Settings, load_settings
from core.enums import ExecutionMode


def _settings(**env: str) -> Settings:
    # load_settings() (non Settings() diretto) applica il mapping flat->nested
    # (_FLAT_MAP): ATS_ETORO_API_KEY -> etoro.api_key. Settings() da solo richiederebbe
    # ATS_ETORO__API_KEY (doppio underscore, env_nested_delimiter nativo pydantic-settings).
    for k, v in env.items():
        os.environ[k] = v
    try:
        return load_settings()
    finally:
        for k in env:
            os.environ.pop(k, None)


def test_etoro_config_defaults() -> None:
    s = _settings()
    assert s.etoro.demo_base_url == "https://public-api.etoro.com"
    assert s.etoro.live_base_url == "https://public-api.etoro.com"
    assert s.etoro.max_penny_price_usd == 50.0
    assert s.etoro.read_rate_limit_per_min == 55
    assert s.etoro.write_rate_limit_per_min == 18


def test_etoro_config_reads_env_credentials() -> None:
    s = _settings(ATS_ETORO_API_KEY="pub-key-123", ATS_ETORO_USER_KEY="user-key-456")
    assert s.etoro.api_key is not None
    assert s.etoro.api_key.get_secret_value() == "pub-key-123"
    assert s.etoro.user_key.get_secret_value() == "user-key-456"
    assert s.etoro.configured is True


def test_etoro_config_not_configured_without_keys() -> None:
    s = _settings()
    assert s.etoro.configured is False


def test_etoro_endpoint_path_by_execution_mode() -> None:
    demo = _settings(ATS_EXECUTION_MODE="DEMO")
    assert demo.etoro.orders_path(demo.execution_mode) == "/api/v2/trading/execution/demo/orders"
    live = _settings(ATS_EXECUTION_MODE="LIVE", ATS_ETORO_API_KEY="k", ATS_ETORO_USER_KEY="u")
    assert live.etoro.orders_path(live.execution_mode) == "/api/v2/trading/execution/orders"
    paper = _settings(ATS_EXECUTION_MODE="PAPER")
    assert paper.etoro.orders_path(paper.execution_mode) == "/api/v2/trading/execution/demo/orders"


def test_ig_polymarket_limitless_removed_from_settings() -> None:
    s = _settings()
    assert not hasattr(s, "ig")
    assert not hasattr(s, "polymarket")
    assert not hasattr(s, "limitless")
    assert not hasattr(s, "ig_enabled")
    assert not hasattr(s, "ig_environment")


def test_live_mode_requires_etoro_credentials_not_ig() -> None:
    with pytest.raises(ValueError, match="(?i)etoro"):
        _settings(ATS_EXECUTION_MODE="LIVE")


def test_live_mode_succeeds_with_etoro_credentials_only() -> None:
    s = _settings(ATS_EXECUTION_MODE="LIVE", ATS_ETORO_API_KEY="k", ATS_ETORO_USER_KEY="u")
    assert s.execution_mode == ExecutionMode.LIVE


def test_live_small_also_requires_etoro_credentials() -> None:
    with pytest.raises(ValueError, match="(?i)etoro"):
        _settings(ATS_EXECUTION_MODE="LIVE_SMALL")
