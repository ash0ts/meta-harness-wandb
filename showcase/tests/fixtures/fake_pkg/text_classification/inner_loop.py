# SPDX-License-Identifier: MIT
"""Stdlib-only stand-in for upstream ``inner_loop.py`` (L1-L825 at f805ad3).

Same argparse flags, same skip rule (``exit(0)`` when every requested output exists), the loaders bound inside
``__main__`` from ``.data`` (L519) and ``LLM`` imported after loading (L637), the same evaluator call shape
``check_answer(pred, ex["target"], **_get_eval_kwargs(ex))``, the same normalisation, and the same output files:
``val.json`` / ``test.json`` (``_build_output``), ``memory.json`` and ``log.jsonl``. Offline mode learns with
the labels visible, evaluates validation after the epoch (pass 1) and again at the end (pass 2); with
``--load-memory`` it skips learning and makes one test pass. The configuration is a constant (no YAML).
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory_system import MemorySystem

CONFIG = {
    "dataset": {"num_train": 4, "num_val": 5, "num_test": 6, "overrides": {}},
    "inner_loop": {"mode": "offline", "num_epochs": 1, "eval_interval": 0, "batch_size": 1, "seed": 42},
    "models": [{"model": "openrouter/openai/gpt-oss-120b"}],
}


class JSONLLogger:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        self.start_time = time.time()
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")

    def log(self, type: str, **data):
        if not self.path:
            return
        entry = {"type": type, "t": round(time.time() - self.start_time, 2), **data}
        with self._lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")


def _get_eval_kwargs(ex: dict[str, Any]) -> dict[str, Any]:
    kwargs = {k: v for k, v in ex.items() if k not in ("input", "target")}
    if "raw_input" in ex:
        kwargs["input_nums"] = ex["raw_input"]
    return kwargs


def _unpack_eval_result(raw) -> tuple[bool, dict]:
    if isinstance(raw, dict):
        return raw["was_correct"], raw.get("metrics", {})
    return bool(raw), {}


def make_result(preds: list[dict]) -> dict:
    correct = sum(1 for p in preds if p["was_correct"])
    result = {"accuracy": correct / len(preds) if preds else 0.0, "correct": correct, "total": len(preds)}
    f1_values = [p["metrics"]["f1"] for p in preds if p.get("metrics", {}).get("f1") is not None]
    result["avg_f1"] = sum(f1_values) / len(f1_values) if f1_values else None
    return result


def evaluate_memory(memory: MemorySystem, examples: list[dict], check_answer, max_workers: int = 32) -> dict:
    if not examples:
        return {"accuracy": 0.0, "correct": 0, "total": 0, "predictions": [], "avg_context_len": 0}

    def predict_one(idx: int, ex: dict) -> tuple:
        pred, _ = memory.predict(ex["input"])
        prompt_len = memory.get_last_prompt_info().get("prompt_len") or 0
        raw = check_answer(pred, ex["target"], **_get_eval_kwargs(ex))
        ok, metrics = _unpack_eval_result(raw)
        result = {"prediction": pred, "target": ex["target"], "was_correct": ok,
                  "context_len": max(0, prompt_len - len(ex["input"])) if prompt_len else 0}
        if metrics:
            result["metrics"] = metrics
        return idx, result

    results = [None] * len(examples)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(examples))) as exe:
        futures = {exe.submit(predict_one, i, ex): i for i, ex in enumerate(examples)}
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result
    correct = sum(1 for r in results if r["was_correct"])
    lens = [r["context_len"] for r in results]
    return {"accuracy": correct / len(examples), "correct": correct, "total": len(examples),
            "predictions": results, "avg_context_len": int(sum(lens) / len(lens))}


def run_inner_loop(memory, examples, check_answer, *, mode, logger, val_examples=None) -> dict:
    """Offline: learn with labels visible, then validation pass 1. Online: predict, score, learn per example."""
    correct = 0
    for step, ex in enumerate(examples):
        if mode == "online":
            pred, meta = memory.predict(ex["input"])
            ok, metrics = _unpack_eval_result(check_answer(pred, ex["target"], **_get_eval_kwargs(ex)))
            correct += int(ok)
            result = {"input": ex["input"], "prediction": pred, "ground_truth": ex["target"], "was_correct": ok,
                      "metadata": meta}
        else:
            result = {"input": ex["input"], "prediction": ex["target"], "ground_truth": ex["target"],
                      "was_correct": True}
        for k, v in ex.items():
            if k not in ("input", "target") and k not in result:
                result[k] = v
        memory.learn_from_batch([result])
        logger.log("train_batch" if mode == "offline" else "learn_batch", step=step, batch_size=1)
    if mode == "offline" and val_examples:
        val_result = evaluate_memory(memory, val_examples, check_answer)
        logger.log("val_epoch", epoch=0, val_acc=round(val_result["accuracy"], 4),
                   val_correct=val_result["correct"], val_total=val_result["total"])
        print(f"  epoch 0: val={val_result['accuracy']:.1%} ({val_result['correct']}/{val_result['total']})",
              flush=True)
    return {"correct": correct, "total": len(examples)}


def load_memory_system(path: str, llm) -> MemorySystem:
    """Upstream's loader rule: the first class (alphabetical) that subclasses MemorySystem (L479-L504)."""
    import importlib
    import inspect

    if "/" not in path and not path.endswith(".py"):
        try:
            return load_memory_system(f"agents/{path}.py", llm)
        except (ModuleNotFoundError, ValueError):
            raise ValueError(f"Memory system '{path}' not found in agents") from None
    module_path = path.replace("/", ".").replace(".py", "")
    module = importlib.import_module(f".{module_path}", package="text_classification")
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, MemorySystem) and obj is not MemorySystem:
            return obj(llm=llm)
    raise ValueError(f"No MemorySystem subclass found in {path}")


if __name__ == "__main__":
    import argparse

    from .data import ALL_TASKS, load_dataset_splits, load_dataset_splits_3way

    cfg = CONFIG
    parser = argparse.ArgumentParser(description="Run inner loop with memory system")
    parser.add_argument("--memory", default="agents/ok_agent.py")
    parser.add_argument("--dataset", required=True, help=f"Options: {ALL_TASKS}")
    parser.add_argument("--seed", type=int, default=cfg["inner_loop"]["seed"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--mode", default=cfg["inner_loop"]["mode"], choices=["online", "offline"])
    parser.add_argument("--num-epochs", type=int, default=cfg["inner_loop"]["num_epochs"])
    parser.add_argument("--num-train", type=int, default=None)
    parser.add_argument("--num-val", type=int, default=None)
    parser.add_argument("--num-test", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--save-memory", default=None)
    parser.add_argument("--load-memory", default=None)
    parser.add_argument("--val-output", default=None)
    parser.add_argument("--test-output", default=None)
    parser.add_argument("--log", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    outputs_to_check = [p for p in [args.val_output, args.test_output] if p]
    if outputs_to_check and all(Path(p).exists() for p in outputs_to_check) and not args.force:
        print(f"Already complete, skipping: {outputs_to_check}")
        exit(0)

    ds = cfg["dataset"]
    num_train = args.num_train if args.num_train is not None else ds["num_train"]
    num_val = args.num_val if args.num_val is not None else ds["num_val"]
    num_test = args.num_test if args.num_test is not None else ds["num_test"]
    eval_val, eval_test = args.val_output is not None, args.test_output is not None

    print(f"Loading dataset: {args.dataset}", flush=True)
    if num_val > 0:
        train_examples, val_examples, test_examples, evaluator = load_dataset_splits_3way(
            args.dataset, num_train=num_train, num_val=num_val, num_test=num_test, shuffle_seed=args.seed)
    else:
        train_examples, test_examples, evaluator = load_dataset_splits(
            args.dataset, num_train=num_train, num_test=num_test, shuffle_seed=args.seed)
        val_examples = []

    from .llm import LLM

    model = args.model or cfg["models"][0]["model"]
    api_base = args.api_base or cfg["models"][0].get("api_base")
    llm = LLM(model=model, api_base=api_base, temperature=args.temperature)
    memory = load_memory_system(path=args.memory, llm=llm)
    logger = JSONLLogger(args.log)
    logger.log("meta", dataset=args.dataset, memory=Path(args.memory).stem, model=model, seed=args.seed,
               mode=args.mode, num_epochs=args.num_epochs if args.mode == "offline" else None,
               start_time=datetime.now().isoformat())
    run_start = time.time()
    train_correct = 0
    if args.load_memory:
        memory.set_state(Path(args.load_memory).read_text())
        print(f"Loaded memory state from {args.load_memory}", flush=True)
    else:
        loop = run_inner_loop(memory, train_examples, evaluator, mode=args.mode, logger=logger,
                              val_examples=val_examples)
        train_correct = loop["correct"]
        if args.save_memory:
            Path(args.save_memory).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save_memory).write_text(memory.get_state())
            print(f"Saved memory state to {args.save_memory}", flush=True)

    val_preds, test_preds, avg_context_len = [], [], 0
    if eval_val and eval_test:
        combined = evaluate_memory(memory, val_examples + test_examples, evaluator)
        avg_context_len = combined["avg_context_len"]
        val_preds = combined["predictions"][: len(val_examples)]
        test_preds = combined["predictions"][len(val_examples):]
    elif eval_val:
        result = evaluate_memory(memory, val_examples, evaluator)
        avg_context_len, val_preds = result["avg_context_len"], result["predictions"]
    elif eval_test:
        result = evaluate_memory(memory, test_examples, evaluator)
        avg_context_len, test_preds = result["avg_context_len"], result["predictions"]
    val_result = make_result(val_preds) if val_preds else None
    test_result = make_result(test_preds) if test_preds else None
    runtime = time.time() - run_start
    usage = llm.get_usage()
    logger.log("done", train_correct=train_correct, train_total=len(train_examples),
               val_acc=round(val_result["accuracy"], 4) if val_result else None,
               test_acc=round(test_result["accuracy"], 4) if test_result else None,
               runtime_seconds=round(runtime, 2), llm_calls=usage["calls"])
    print(f"Done: time={runtime:.1f}s", flush=True)

    def _build_output(result_dict: dict) -> dict:
        return {"accuracy": result_dict["accuracy"], "correct": result_dict["correct"],
                "total": result_dict["total"], "dataset": args.dataset, "memory": args.memory, "model": model,
                "seed": args.seed, "mode": args.mode,
                "num_epochs": args.num_epochs if args.mode == "offline" else None,
                "timestamp": datetime.now().isoformat(), "runtime_seconds": round(runtime, 2),
                "memory_context_chars": avg_context_len, "llm_calls": usage["calls"],
                "llm_input_tokens": usage["input_tokens"], "llm_output_tokens": usage["output_tokens"],
                "llm_total_tokens": usage["total_tokens"]}

    for path, result in ((args.val_output, val_result), (args.test_output, test_result)):
        if path and result:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(_build_output(result), f, indent=2)
            print(f"Saved results to {path}", flush=True)
