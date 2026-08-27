"""Gerarchia errori del sistema."""
from __future__ import annotations


class ATSError(Exception):
    """Errore base."""


class ConfigError(ATSError):
    pass


class DataError(ATSError):
    """Dati corrotti/incompleti (-> kill switch CORRUPTED_DATA)."""


class StaleDataError(DataError):
    pass


class UpstreamError(ATSError):
    """Errore da API esterna."""

    def __init__(self, message: str, *, status_code: int | None = None, provider: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider


class RateLimitError(UpstreamError):
    pass


class RiskViolation(ATSError):
    """Il Risk Engine ha rifiutato l'operazione. Non catturabile dagli agenti LLM."""

    def __init__(self, message: str, *, code: str = "RISK_VIOLATION"):
        super().__init__(message)
        self.code = code


class KillSwitchActive(ATSError):
    pass


class ExecutionError(ATSError):
    pass


class LLMError(ATSError):
    pass


class LLMOutputError(LLMError):
    """Output strutturato non valido (sez. 79: LLM structured-output tests)."""


class LLMBudgetExceeded(LLMError):
    pass


class ToolPermissionError(ATSError):
    """Un agente ha tentato di usare un tool non autorizzato (sez. 20/21)."""


class JurisdictionError(ATSError):
    """Hard rule 9: mercato non legalmente accessibile."""
