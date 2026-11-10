# SPDX-License-Identifier: MIT
"""A working memory system: one model call per prediction, learns a count and the labels it saw (no call)."""

import json
from typing import Any

from ..memory_system import MemorySystem, extract_json_field

PROMPT = """Answer the following question.

{input}

Answer in JSON with the keys "reasoning" and "final_answer".
"""


class OkAgent(MemorySystem):
    def __init__(self, llm):
        super().__init__(llm)
        self._seen = 0
        self._labels: list[str] = []

    def predict(self, input: str) -> tuple[str, dict[str, Any]]:
        response = self.call_llm(PROMPT.format(input=input))
        answer = extract_json_field(response, "final_answer") or response.strip()
        return answer, {"full_response": response}

    def learn_from_batch(self, batch_results: list[dict[str, Any]]) -> None:
        for result in batch_results:
            self._seen += 1
            label = str(result.get("ground_truth", ""))
            if label not in self._labels:
                self._labels.append(label)

    def get_state(self) -> str:
        return json.dumps({"seen": self._seen, "labels": self._labels[:20]})

    def set_state(self, state: str) -> None:
        data = json.loads(state or "{}")
        self._seen = int(data.get("seen", 0))
        self._labels = list(data.get("labels", []))
