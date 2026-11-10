# SPDX-License-Identifier: MIT
"""A candidate whose import fails: upstream's import check must report it."""

import mhf_fake_module_that_does_not_exist  # noqa: F401

from ..memory_system import MemorySystem


class BadImport(MemorySystem):
    def predict(self, input):
        return "", {}

    def learn_from_batch(self, batch_results):
        pass

    def get_state(self):
        return "{}"

    def set_state(self, state):
        pass
