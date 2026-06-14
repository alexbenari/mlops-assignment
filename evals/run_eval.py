"""Eval runner using execution accuracy.

Reads evals/eval_set.jsonl, calls the agent at AGENT_URL on each question,
then compares the agent's SQL output to the gold SQL by *executed rows*
(canonicalized: sorted, stringified, None-coerced to empty).

Helpers (run_sql / canonicalize / matches) are provided. You implement
eval_one() and summarize().

Run:
    uv run python evals/run_eval.py --out results/eval_baseline.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_FILE = ROOT / "evals" / "eval_set.jsonl"
DEFAULT_OUT_FILE = ROOT / "results" / "eval_baseline.json"
DB_DIR = ROOT / "data" / "bird"
AGENT_URL_DEFAULT = "http://localhost:8001/answer"


# ---------- Helpers (provided) -----------------------------------------

def run_sql(db_id: str, sql: str, timeout: float = 5.0) -> tuple[bool, list[tuple] | None, str | None]:
    """Run sql against db_id in read-only mode. Returns (ok, rows, error)."""
    path = DB_DIR / f"{db_id}.sqlite"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout) as conn:
            cur = conn.execute(sql)
            rows = cur.fetchall()
            return True, rows, None
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"


def canonicalize(rows: list[tuple] | None) -> list[tuple] | None:
    """Sort rows; coerce cells to str; None -> ''."""
    if rows is None:
        return None
    return sorted(tuple("" if c is None else str(c) for c in row) for row in rows)


def matches(gold_rows: list[tuple] | None, pred_rows: list[tuple] | None) -> bool:
    if gold_rows is None or pred_rows is None:
        return False
    return canonicalize(gold_rows) == canonicalize(pred_rows)


# ---------- Implement these (Phase 5) ----------------------------------

def _parse_metadata_tag(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"metadata tag must be KEY=VALUE, got: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError(f"metadata tag key cannot be empty: {raw}")
    return key, value


def _attempts_from_history(history: list[dict[str, Any]], final_sql: str) -> list[str]:
    attempts = [
        str(step["sql"]).strip()
        for step in history
        if step.get("node") in {"generate_sql", "revise"} and step.get("sql")
    ]
    final_sql = final_sql.strip()
    if final_sql and (not attempts or attempts[-1] != final_sql):
        attempts.append(final_sql)
    return attempts


def eval_one(
    question: dict,
    agent_url: str,
    metadata_tags: dict[str, str] | None = None,
    langfuse_tags: list[str] | None = None,
) -> dict:
    """Score one question. Return a dict capturing per-iteration correctness."""
    gold_ok, gold_rows, gold_error = run_sql(question["db_id"], question["gold_sql"])

    request_tags: dict[str, Any] = dict(metadata_tags or {})
    if langfuse_tags:
        request_tags["langfuse_tags"] = list(langfuse_tags)

    payload: dict[str, Any] = {
        "question": question["question"],
        "db": question["db_id"],
    }
    if request_tags:
        payload["tags"] = request_tags

    t0 = time.monotonic()
    response = httpx.post(agent_url, json=payload, timeout=180.0)
    elapsed = time.monotonic() - t0
    response.raise_for_status()
    body = response.json()

    history = body.get("history", [])
    attempts: list[dict[str, Any]] = []
    for idx, sql in enumerate(_attempts_from_history(history, body.get("sql", ""))):
        pred_ok, pred_rows, pred_error = run_sql(question["db_id"], sql)
        attempts.append({
            "iteration": idx,
            "sql": sql,
            "ok": pred_ok,
            "error": pred_error,
            "correct": bool(gold_ok and pred_ok and matches(gold_rows, pred_rows)),
        })

    final_correct = attempts[-1]["correct"] if attempts else False
    return {
        "question": question["question"],
        "db_id": question["db_id"],
        "gold_sql": question["gold_sql"],
        "gold_ok": gold_ok,
        "gold_error": gold_error,
        "final_sql": body.get("sql", ""),
        "final_ok": body.get("ok", False),
        "final_error": body.get("error"),
        "iterations": body.get("iterations", 0),
        "latency_seconds": elapsed,
        "final_correct": final_correct,
        "attempts": attempts,
    }


def summarize(results: list[dict]) -> dict:
    """Aggregate per-question results.

    Per-iteration carry-forward: if the agent terminated at iteration j < k
    (verify said ok at j, or it hit MAX_ITERATIONS at j < k), treat the
    question's iteration-k result as identical to its iteration-j result.
    The agent stopped emitting; whatever it had at termination is what
    would have been served had we polled at iteration k.
    """
    total = len(results)
    max_attempts = max((len(r.get("attempts", [])) for r in results), default=0)

    per_iteration_accuracy: dict[str, float] = {}
    for idx in range(max_attempts):
        correct = 0
        for result in results:
            attempts = result.get("attempts", [])
            if not attempts:
                continue
            carried = attempts[idx] if idx < len(attempts) else attempts[-1]
            correct += int(bool(carried.get("correct", False)))
        per_iteration_accuracy[f"iter_{idx}"] = (correct / total) if total else 0.0

    return {
        "questions": total,
        "final_accuracy": (
            sum(int(bool(r.get("final_correct", False))) for r in results) / total
            if total
            else 0.0
        ),
        "per_iteration_accuracy": per_iteration_accuracy,
        "avg_iterations": (
            sum(int(r.get("iterations", 0)) for r in results) / total if total else 0.0
        ),
        "revised_questions": sum(int(int(r.get("iterations", 0)) > 1) for r in results),
    }


# ---------- Main (provided) --------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_FILE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument("--agent-url", default=AGENT_URL_DEFAULT)
    parser.add_argument(
        "--langfuse-tag",
        action="append",
        default=[],
        help="Repeatable explicit Langfuse tag to attach to each trace.",
    )
    parser.add_argument(
        "--metadata-tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Repeatable metadata tag to attach to each agent request.",
    )
    args = parser.parse_args()

    questions = [json.loads(line) for line in args.eval_set.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(questions)} eval questions from {args.eval_set}")
    metadata_tags = dict(_parse_metadata_tag(item) for item in args.metadata_tag)
    langfuse_tags = list(args.langfuse_tag)

    results: list[dict] = []
    t0 = time.monotonic()
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['db_id']}: {q['question'][:60]}...", flush=True)
        results.append(eval_one(q, args.agent_url, metadata_tags, langfuse_tags))
    elapsed = time.monotonic() - t0

    summary = summarize(results)
    out = {
        "summary": summary,
        "wall_clock_seconds": elapsed,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"Wrote {args.out}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
