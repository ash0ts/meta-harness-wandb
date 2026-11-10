# SPDX-License-Identifier: MIT
"""Stdlib-only stand-in for upstream ``memory_system.py``: the base class the loader looks for."""

import hashlib
import json
import re
import threading
from abc import ABC, abstractmethod
from typing import Any


def extract_json_field(text: str, field: str, default: str = "") -> str:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return str(data.get(field, default))
    except json.JSONDecodeError:
        pass
    match = re.findall(rf'"{field}"\s*:\s*"([^"]*)"', text)
    return match[-1] if match else default


class MemorySystem(ABC):
    """Same interface as upstream: thread-local last-prompt info, abstract predict/learn/state."""

    def __init__(self, llm):
        self._llm = llm
        self._prompt_local = threading.local()

    def call_llm(self, prompt: str) -> str:
        self._prompt_local.last_prompt_len = len(prompt)
        self._prompt_local.last_prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
        self._prompt_local.last_prompt_text = prompt
        return self._llm(prompt)

    def get_last_prompt_info(self) -> dict[str, Any]:
        return {
            "prompt_len": getattr(self._prompt_local, "last_prompt_len", None),
            "prompt_hash": getattr(self._prompt_local, "last_prompt_hash", None),
            "prompt_text": getattr(self._prompt_local, "last_prompt_text", None),
        }

    @abstractmethod
    def predict(self, input: str) -> tuple[str, dict[str, Any]]: ...

    @abstractmethod
    def learn_from_batch(self, batch_results: list[dict[str, Any]]) -> None: ...

    def get_context_length(self) -> int:
        return len(self.get_state())

    @abstractmethod
    def get_state(self) -> str: ...

    @abstractmethod
    def set_state(self, state: str) -> None: ...
