"""Run a fixed batch of historically slow agent queries and record results.

This is a Phase 6 regression harness for the "long-tail" questions found in
Langfuse. It runs them sequentially against the agent endpoint, records
latency/status/history, and writes a JSON artifact that can be compared after
each tuning change.

Run:
    uv run python scripts/run_long_query_batch.py \
      --out results/long_query_batch_baseline.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "results" / "long_query_batch_baseline.json"
AGENT_URL_DEFAULT = "http://localhost:8001/answer"

LONG_QUERY_CASES: list[dict[str, Any]] = [
    {
        "case_id": "codebase_community_single_history_1000_views",
        "db_id": "codebase_community",
        "question": "Which user have only one post history per post and having at least 1000 views?",
        "source_trace_latency_seconds": 1865.677,
        "suspected_cause": "pathological execute step",
    },
    {
        "case_id": "european_football_attacking_moves",
        "db_id": "european_football_2",
        "question": "Who are the players that tend to be attacking when their mates were doing attack moves? List down their name.",
        "source_trace_latency_seconds": 427.025,
        "suspected_cause": "pathological execute step after revise",
    },
    {
        "case_id": "toxicology_tr000_1_2_molecule",
        "db_id": "toxicology",
        "question": "Indicate the molecule id is belonging to the TR000_1_2 bond that has the first atom named TR000_1.",
        "source_trace_latency_seconds": 137.247,
        "suspected_cause": "verify call grows to token cap",
    },
    {
        "case_id": "california_schools_magnet_k_8",
        "db_id": "california_schools",
        "question": "Of the schools that offers a magnet program serving a grade span of Kindergarten to 8th grade, how many offers Multiple Provision Types? List down the full school names and DOCType.",
        "source_trace_latency_seconds": 102.288,
        "suspected_cause": "verify call grows to token cap",
    },
    {
        "case_id": "card_games_highest_converted_mana_artist",
        "db_id": "card_games",
        "question": "Among the cards that doesn't have multiple faces on the same card, who is the illustrator of the card art that has the highest cost of converted mana?",
        "source_trace_latency_seconds": 88.735,
        "suspected_cause": "verify call grows to token cap",
    },
    {
        "case_id": "card_games_creature_legal_status",
        "db_id": "card_games",
        "question": "Lists by ID all Creature-type cards with legal status.",
        "source_trace_latency_seconds": 80.670,
        "suspected_cause": "verify call grows to token cap",
    },
    {
        "case_id": "thrombosis_prediction_ldh_above_normal",
        "db_id": "thrombosis_prediction",
        "question": "Provide the ID and age of patient with lactate dehydrogenase (LDH) between 100-300 index above the normal range.",
        "source_trace_latency_seconds": 38.486,
        "suspected_cause": "multi-iteration loop plus verbose verify",
    },
    {
        "case_id": "debit_card_eur_consumption_segment_growth",
        "db_id": "debit_card_specializing",
        "question": "Which of the three segments—SME, LAM and KAM—has the biggest and lowest percentage increases in consumption paid in EUR between 2012 and 2013?",
        "source_trace_latency_seconds": 19.702,
        "suspected_cause": "semantic revise loop",
    },
    {
        "case_id": "european_football_average_height_italy",
        "db_id": "european_football_2",
        "question": "What is the average heights of Italy players?",
        "source_trace_latency_seconds": 14.939,
        "suspected_cause": "semantic revise loop",
    },
]


def run_case(
    client: httpx.Client,
    agent_url: str,
    case: dict[str, Any],
    timeout_seconds: float,
    langfuse_tags: list[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "question": case["question"],
        "db": case["db_id"],
    }
    if langfuse_tags:
        payload["tags"] = {
            "langfuse_tags": list(langfuse_tags) + [f"case:{case['case_id']}"],
        }

    started_at = time.time()
    t0 = time.monotonic()
    status = "ok"
    err: str | None = None
    body: dict[str, Any] | None = None

    try:
        response = client.post(agent_url, json=payload, timeout=timeout_seconds)
        elapsed = time.monotonic() - t0
        if response.status_code != 200:
            status = "http_error"
            err = f"HTTP {response.status_code}: {response.text[:500]}"
        else:
            body = response.json()
    except httpx.TimeoutException:
        elapsed = time.monotonic() - t0
        status = "timeout"
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        status = "client_error"
        err = f"{type(e).__name__}: {e}"

    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "db_id": case["db_id"],
        "question": case["question"],
        "started_at_unix": started_at,
        "latency_seconds": elapsed,
        "status": status,
        "error": err,
        "source_trace_latency_seconds": case["source_trace_latency_seconds"],
        "suspected_cause": case["suspected_cause"],
    }

    if body is not None:
        execution_rows = body.get("rows")
        result.update({
            "agent_ok": body.get("ok"),
            "agent_error": body.get("error"),
            "sql": body.get("sql"),
            "iterations": body.get("iterations"),
            "history_nodes": [step.get("node") for step in body.get("history", [])],
            "history": body.get("history"),
            "row_count": len(execution_rows) if isinstance(execution_rows, list) else None,
        })

    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [r["latency_seconds"] for r in results]
    return {
        "cases": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "timeouts": sum(1 for r in results if r["status"] == "timeout"),
        "http_errors": sum(1 for r in results if r["status"] == "http_error"),
        "client_errors": sum(1 for r in results if r["status"] == "client_error"),
        "latency_max": max(latencies, default=0.0),
        "latency_min": min(latencies, default=0.0),
        "latency_avg": (sum(latencies) / len(latencies)) if latencies else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default=AGENT_URL_DEFAULT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
        help="Per-query client timeout when calling the agent.",
    )
    parser.add_argument(
        "--langfuse-tag",
        action="append",
        default=[],
        help="Repeatable Langfuse tag attached to all traces in this batch.",
    )
    args = parser.parse_args()

    langfuse_tags = list(args.langfuse_tag) or [
        "phase6",
        "long-query-batch",
        "baseline",
    ]

    results: list[dict[str, Any]] = []
    batch_start = time.monotonic()
    with httpx.Client() as client:
        for idx, case in enumerate(LONG_QUERY_CASES, 1):
            print(
                f"[{idx}/{len(LONG_QUERY_CASES)}] {case['case_id']} "
                f"({case['db_id']})",
                flush=True,
            )
            results.append(
                run_case(
                    client=client,
                    agent_url=args.agent_url,
                    case=case,
                    timeout_seconds=args.timeout_seconds,
                    langfuse_tags=langfuse_tags,
                )
            )

    out = {
        "summary": summarize(results),
        "batch_timeout_seconds": args.timeout_seconds,
        "langfuse_tags": langfuse_tags,
        "wall_clock_seconds": time.monotonic() - batch_start,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"Wrote {args.out}")
    print(json.dumps(out["summary"], indent=2))


if __name__ == "__main__":
    main()
