from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.config import Settings
from intelligence.etoro_judge import CatalystVerdict, judge_catalyst
from strategies.etoro_momentum import MomentumCandidate


def _candidate() -> MomentumCandidate:
    return MomentumCandidate(instrument_id=1, name="PennyCo", price=3.55, gap_pct=0.18, relative_volume=9.0)


def test_llm_config_has_etoro_catalyst_judge_role() -> None:
    roles = Settings().llm.roles()
    assert "etoro_catalyst_judge" in roles
    assert roles["etoro_catalyst_judge"].model == "openai/gpt-5.6-sol-pro"


@pytest.mark.asyncio
async def test_judge_catalyst_returns_verdict_from_llm() -> None:
    llm = AsyncMock()
    llm.complete.return_value = type("R", (), {"parsed": CatalystVerdict(has_catalyst=True, direction="BUY", confidence=0.7, rationale="FDA approval headline")})()

    verdict = await judge_catalyst(_candidate(), news_brief="FDA approved PennyCo's drug trial extension.", llm=llm)

    assert verdict.has_catalyst is True
    assert verdict.direction == "BUY"
    llm.complete.assert_awaited_once()
    call_args = llm.complete.await_args
    assert call_args.args[0] == "etoro_catalyst_judge"
    assert call_args.kwargs["schema"] is CatalystVerdict


@pytest.mark.asyncio
async def test_judge_catalyst_llm_failure_returns_no_catalyst() -> None:
    llm = AsyncMock()
    llm.complete.side_effect = Exception("timeout")

    verdict = await judge_catalyst(_candidate(), news_brief="", llm=llm)

    assert verdict.has_catalyst is False


@pytest.mark.asyncio
async def test_judge_catalyst_unparsed_returns_no_catalyst() -> None:
    llm = AsyncMock()
    llm.complete.return_value = type("R", (), {"parsed": None})()

    verdict = await judge_catalyst(_candidate(), news_brief="noise", llm=llm)

    assert verdict.has_catalyst is False
