from __future__ import annotations

from abc import ABC, abstractmethod

from .config import ModelConfig


class ModelError(RuntimeError):
    pass


class ModelClient(ABC):
    @abstractmethod
    def complete(self, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError


class SameSessionRequired(ModelClient):
    """Fail-closed model client.

    Codex skills run inside the already-loaded agent session. This package must not
    create a fresh Claude Code or Codex subprocess to act as the mutation model.
    Tests may inject an in-process deterministic ModelClient, but production use
    must follow SKILL.md's same-session loop.
    """

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def complete(self, messages: list[dict[str, str]]) -> str:
        raise ModelError(
            "recursive-self-improvement is a same-session Codex skill. "
            "Do not launch child Claude/Codex sessions; load SKILL.md and run the loop "
            "with the current agent's exposed tools."
        )


def create_model_client(config: ModelConfig) -> ModelClient:
    return SameSessionRequired(config)
