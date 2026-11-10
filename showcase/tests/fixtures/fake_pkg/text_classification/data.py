# SPDX-License-Identifier: MIT
"""Stdlib-only stand-in for upstream ``data/`` (``data/api.py`` L76-L153 at f805ad3), as one module.

A module, not a directory: the repository's root ``.gitignore`` ignores directories named ``data``. Examples
are synthetic and made to the requested size (no upstream file is read). Each question ends with
``hint: <answer>``; a test completion server can answer with the hint, which equals the target for two
examples in three. Two shapes the capture must handle are built in:

- ``Symptom2Disease``: test example 1 is val example 0 (as one real question is in both splits);
- ``DupKeys``: val example 2 repeats val example 0 (same question and target).

The module has no relative import, so a test may load it standalone from its file.
"""

import json
import re
from collections.abc import Callable

ALL_TASKS = ["USPTO", "Symptom2Disease", "LawBench", "DupKeys"]
_DIAGNOSES = ("common cold", "flu", "migraine")
_CHARGES = ("盗窃", "诈骗", "抢劫")
_ASK = {
    "USPTO": "Give the reactants of the product as SMILES.",
    "Symptom2Disease": "Which diagnosis fits these symptoms?",
    "LawBench": "请给出罪名。",
    "DupKeys": "Name the answer.",
}


def _target(task: str, i: int) -> str:
    if task == "USPTO":
        return "CC%sO.C%dN" % ("C" * (i % 4), i)
    if task == "Symptom2Disease":
        return _DIAGNOSES[i % 3]
    if task == "LawBench":
        return "[罪名]%s<eoa>" % _CHARGES[i % 3]
    return "answer-%d" % i


def make_example(task: str, split: str, i: int) -> dict:
    """The ``i``-th synthetic example of ``split``, before the built-in sharing and duplication."""
    if task == "Symptom2Disease" and split == "test" and i == 1:
        return make_example(task, "val", 0)
    if task == "DupKeys" and split == "val" and i == 2:
        return make_example(task, "val", 0)
    target = _target(task, i + {"train": 0, "val": 1000, "test": 2000}[split])
    hint = target if i % 3 else "WRONG"
    question = "SYNTHETIC %s %s item %04d. %s\nhint: %s" % (task, split, i, _ASK[task], hint)
    return {"input": question, "target": target, "raw_question": question, "context": ""}


def _split(task: str, split: str, n: int) -> list[dict]:
    if task not in ALL_TASKS:
        raise ValueError(f"Unknown task: {task}")
    return [make_example(task, split, i) for i in range(n)]


def extract_final_answer(response_text: str) -> str:
    if not response_text:
        return ""
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, dict) and "final_answer" in parsed:
            return str(parsed["final_answer"])
    except json.JSONDecodeError:
        pass
    match = re.search(r'"final_answer"\s*:\s*"([^"]*)"', response_text)
    return match.group(1) if match else response_text


def _eval_uspto(pred: str, target: str, **kwargs) -> dict:
    def parts(text: str) -> set:
        return {p.strip().lower() for p in text.strip().split(".") if p.strip()}

    got, want = parts(extract_final_answer(pred)), parts(target)
    jaccard = 1.0 if not got and not want else (len(got & want) / len(got | want) if got and want else 0.0)
    return {"was_correct": got == want, "metrics": {"jaccard_similarity": jaccard}}


def _eval_symptom(pred: str, target: str, **kwargs) -> bool:
    text = extract_final_answer(pred)
    match = re.search(r"\[DIAGNOSIS\](.*?)\[/DIAGNOSIS\]", text, re.I | re.S)
    if match:
        text = match.group(1)
    return text.strip().lower() == target.strip().lower()


def _eval_lawbench(pred: str, target: str, **kwargs) -> dict:
    def charges(text: str) -> set:
        text = extract_final_answer(text).strip()
        match = re.search(r"\[罪名\](.*?)(?:<eoa>|$)", text)
        text = (match.group(1) if match else text).strip()
        return {text} if text else set()

    got, want = charges(pred), charges(target)
    tp, fp, fn = len(got & want), len(got - want), len(want - got)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"was_correct": got == want,
            "metrics": {"f1": f1, "precision": precision, "recall": recall, "tp": tp, "fp": fp, "fn": fn}}


def _eval_exact(pred: str, target: str, **kwargs) -> bool:
    return extract_final_answer(pred).strip() == target.strip()


def get_evaluator(task: str) -> Callable:
    evaluators = {"USPTO": _eval_uspto, "Symptom2Disease": _eval_symptom, "LawBench": _eval_lawbench,
                  "DupKeys": _eval_exact}
    if task not in evaluators:
        raise ValueError(f"Unknown task: {task}")
    return evaluators[task]


def load_dataset_splits(task: str, num_train: int, num_test: int = 20, shuffle_seed: int = 42):
    """Upstream's two-way loader shape: ``(train, test, evaluator)``. ``shuffle_seed`` is accepted and unused."""
    evaluator = get_evaluator(task)
    return _split(task, "train", num_train), _split(task, "test", num_test), evaluator


def load_dataset_splits_3way(task: str, num_train: int = 30, num_val: int = 35, num_test: int = 35,
                             shuffle_seed: int = 42):
    """Upstream's three-way loader shape: ``(train, val, test, evaluator)``. ``shuffle_seed`` is unused."""
    evaluator = get_evaluator(task)
    return (_split(task, "train", num_train), _split(task, "val", num_val), _split(task, "test", num_test),
            evaluator)
