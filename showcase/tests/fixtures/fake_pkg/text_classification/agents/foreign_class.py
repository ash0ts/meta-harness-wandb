# SPDX-License-Identifier: MIT
"""A candidate whose first MemorySystem subclass (alphabetically) comes from another module.

``AnImportedAgent`` sorts before ``ForeignClass``, so upstream's loader (``inspect.getmembers`` order,
``inner_loop.py`` L500-L502) would load ``ok_agent.OkAgent`` under this candidate's name.
"""

from ..memory_system import MemorySystem
from .ok_agent import OkAgent as AnImportedAgent  # noqa: F401


class ForeignClass(MemorySystem):
    def predict(self, input):
        return "", {}

    def learn_from_batch(self, batch_results):
        pass

    def get_state(self):
        return "{}"

    def set_state(self, state):
        pass
