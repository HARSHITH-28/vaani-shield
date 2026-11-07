"""Provider-neutral contracts for assistant reasoning and tool calls."""

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


class LLMProviderError(RuntimeError):
    """Safe provider failure; messages are suitable for API responses."""


class LLMTimeout(LLMProviderError):
    pass


class LLMRequestTooLarge(LLMProviderError):
    """The provider rejected a request for exceeding its size/context limit."""


class LLMRateLimited(LLMProviderError):
    """The provider rejected a request because of a rate limit."""


class LLMAuthenticationError(LLMProviderError):
    """The configured provider credentials or permissions were rejected."""


class LLMConnectionError(LLMProviderError):
    """The provider could not be reached."""


@dataclass(frozen=True)
class LLMTool:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str = ""
    tool_calls: tuple[LLMToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


class LLMProvider(Protocol):
    def generate(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool],
        *,
        response_language: str,
        required_tools: frozenset[str] = frozenset(),
        request_stage: str = "unspecified",
    ) -> LLMResponse: ...
