"""LangGraph agent: text-to-SQL with verify+revise loop.

Graph shape:

    START -> attach_schema -> generate_sql -> execute -> verify
                                                          |
                                              ok=true ----+----> END
                                                          |
                                              ok=false ---+----> revise -> execute -> verify (loop)

Loop is capped at MAX_ITERATIONS total generate/revise calls.

The execute node and the graph wiring are provided. `generate_sql_node` is
filled in as a worked example; you implement `verify`, `revise`, and the
conditional router following the same shape.
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from agent import prompts
from agent.execution import ExecutionResult, execute_sql
from agent.schema import render_schema

# Total generate + revise calls before the loop is forced to stop.
# 3-5 is a reasonable range; tune it as part of Phase 3.
MAX_ITERATIONS = 3

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")
# vLLM ignores the key, but a hosted OpenAI-compatible provider needs a real one.
# Lets you point the agent at e.g. OpenAI while iterating without a running vLLM.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "not-needed")


@dataclass
class AgentState:
    """State threaded through the graph. Extend with fields you need."""

    question: str
    db_id: str
    schema: str = ""
    sql: str = ""
    execution: ExecutionResult | None = None
    verify_ok: bool = False
    verify_issue: str = ""
    iteration: int = 0
    stalled_revision: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)


def llm() -> ChatOpenAI:
    """Chat client pointed at VLLM_BASE_URL (your local vLLM by default)."""
    kwargs: dict[str, Any] = {
        "model": VLLM_MODEL,
        "base_url": VLLM_BASE_URL,
        "api_key": LLM_API_KEY,
        "temperature": 0.0,
    }
    return ChatOpenAI(
        **kwargs,
    )


# ---- Nodes ------------------------------------------------------------

def _attach_schema(state: AgentState) -> dict:
    """Provided. Render the DB schema once at the start of the run."""
    return {"schema": render_schema(state.db_id)}


def _extract_sql(text: str) -> str:
    """Pull a SQL statement out of an LLM reply, stripping markdown fences/prose.

    Intentionally simple: take the first ```sql ... ``` block if there is one,
    otherwise the whole reply. You may need to harden this for your prompts.
    """
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    candidate = re.sub(r"<think>.*?</think>", "", candidate, flags=re.DOTALL | re.IGNORECASE).strip()

    statements = re.findall(r"(?is)\b(?:with|select)\b.*?(?:;|$)", candidate)
    if statements:
        return statements[-1].strip()
    return candidate.strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON-like object out of a model reply."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = match.group(0) if match else text.strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        data = ast.literal_eval(candidate)
    if not isinstance(data, dict):
        raise ValueError("verifier reply did not contain an object")
    return data


def _execution_issue_hint(error: str) -> str:
    """Turn raw sqlite errors into a more actionable revise hint."""
    table_match = re.search(r"no such table: ([^\s]+)", error, re.IGNORECASE)
    if table_match:
        return (
            f"The query references a table named '{table_match.group(1)}' that does not exist. "
            "Use only exact table names from the schema and rewrite the joins accordingly."
        )
    column_match = re.search(r"no such column: ([^\s]+)", error, re.IGNORECASE)
    if column_match:
        return (
            f"The query references a column named '{column_match.group(1)}' that does not exist. "
            "Use only exact column names from the schema and fix the select, filter, or join clauses."
        )
    return error


def _normalize_sql(sql: str) -> str:
    """Collapse superficial formatting differences before comparing SQL."""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


def _question_entities(question: str) -> list[str]:
    entities: list[str] = []
    for m in re.finditer(r"'([^']+)'|\"([^\"]+)\"", question):
        entity = (m.group(1) or m.group(2) or "").strip()
        if entity:
            entities.append(entity)
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9_.-]+)'s\b", question):
        entities.append(m.group(1))
    return entities


def _heuristic_verify_issue(state: AgentState) -> str | None:
    """Catch the obvious failure modes the tiny stand-in model misses."""
    execution = state.execution
    if execution is None:
        return "No execution result was produced."
    if not execution.ok:
        return _execution_issue_hint(execution.error or "SQL execution failed.")

    sql_lower = state.sql.lower()
    columns_lower = " ".join((execution.columns or [])).lower()
    observed_text = f"{sql_lower} {columns_lower}"
    entities = [e for e in _question_entities(state.question) if len(e) >= 3]
    for entity in entities:
        if entity.lower() not in sql_lower:
            return f"The query does not filter for the requested entity '{entity}'."

    aggregate_intent = (
        "how many" in state.question.lower()
        or "average" in state.question.lower()
        or "percentage" in state.question.lower()
        or "percent" in state.question.lower()
        or "difference" in state.question.lower()
        or "total" in state.question.lower()
    )
    if aggregate_intent and not re.search(r"\b(count|avg|sum|min|max)\s*\(", sql_lower):
        return "The question asks for an aggregate, but the SQL does not compute one."

    if (
        not aggregate_intent
        and execution.row_count > 1
        and execution.rows is not None
        and "distinct" not in sql_lower
        and len({tuple(row) for row in execution.rows}) < execution.row_count
    ):
        return "The query returned duplicate rows; tighten the join or use DISTINCT."

    if execution.row_count == 0 and not aggregate_intent:
        return "The query returned zero rows and likely did not answer the question."

    question_lower = state.question.lower()
    if ("superpower" in question_lower or "super power" in question_lower) and "power" not in observed_text:
        return "The question asks about superpowers, but the query does not retrieve any power-related fields."
    if "coordinate" in question_lower and not ("lat" in observed_text and "lng" in observed_text):
        return "The question asks for coordinates, but the query does not retrieve latitude and longitude."
    if "location" in question_lower and not any(token in observed_text for token in ("location", "lat", "lng")):
        return "The question asks for a location, but the query does not retrieve location-related fields."

    return None


def generate_sql_node(state: AgentState) -> dict:
    """Worked example - the other LLM nodes follow this same shape.

    Build messages from the prompts, call the shared llm(), extract the SQL,
    and return only the state fields you changed. `iteration` is bumped here
    (and in revise) so route_after_verify can enforce MAX_ITERATIONS.

    This node is wired and ready; fill in GENERATE_SQL_SYSTEM / GENERATE_SQL_USER
    in prompts.py to make it produce real queries.
    """
    response = llm().invoke([
        ("system", prompts.GENERATE_SQL_SYSTEM),
        ("user", prompts.GENERATE_SQL_USER.format(
            schema=state.schema,
            question=state.question,
        )),
    ])
    sql = _extract_sql(response.content)
    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "history": state.history + [{"node": "generate_sql", "sql": sql}],
    }


def execute_node(state: AgentState) -> dict:
    """Provided. Runs the SQL and stores the result."""
    return {"execution": execute_sql(state.db_id, state.sql)}


def verify_node(state: AgentState) -> dict:
    """Decide whether state.execution plausibly answers state.question.

    Follow the generate_sql_node pattern: build messages from the VERIFY_*
    prompts, call llm(), parse the reply. Ask the model for a small JSON object
    like {"ok": bool, "issue": str} and parse it defensively - the model may
    wrap it in prose or fences. state.execution.render() gives you a compact
    view of the rows or error to feed into the prompt.

    Return: {"verify_ok": <bool>, "verify_issue": <str>}.
    What counts as "not plausible" is yours to define - see the Phase 3 targets
    in the README.
    """
    response = llm().invoke([
        ("system", prompts.VERIFY_SYSTEM),
        ("user", prompts.VERIFY_USER.format(
            question=state.question,
            schema=state.schema,
            sql=state.sql,
            execution=state.execution.render() if state.execution else "No execution result.",
        )),
    ])

    try:
        payload = _extract_json_object(response.content)
        ok = bool(payload.get("ok", False))
        issue = str(payload.get("issue", "")).strip()
    except Exception:  # noqa: BLE001
        ok = False
        if state.execution is None:
            issue = "No execution result was produced."
        elif not state.execution.ok:
            issue = state.execution.error or "SQL execution failed."
        elif state.execution.row_count == 0:
            issue = "Query returned zero rows and likely did not answer the question."
        else:
            issue = "Verifier response was not parseable; revise the SQL conservatively."

    heuristic_issue = _heuristic_verify_issue(state)
    if heuristic_issue is not None:
        ok = False
        issue = heuristic_issue

    return {
        "verify_ok": ok,
        "verify_issue": issue,
        "history": state.history + [{
            "node": "verify",
            "ok": ok,
            "issue": issue,
        }],
    }


def revise_node(state: AgentState) -> dict:
    """Produce a revised SQL query given state.verify_issue and the prior attempt.

    Same shape as generate_sql_node, but the prompt should include the failing
    SQL, its execution result, and the verifier's complaint so the model can fix
    it. Bump the iteration counter the same way generate_sql_node does so the
    loop terminates.

    Return: {"sql": <str>, "iteration": state.iteration + 1, ...}.
    """
    response = llm().invoke([
        ("system", prompts.REVISE_SYSTEM),
        ("user", prompts.REVISE_USER.format(
            schema=state.schema,
            question=state.question,
            sql=state.sql,
            execution=state.execution.render() if state.execution else "No execution result.",
            issue=state.verify_issue,
        )),
    ])
    sql = _extract_sql(response.content)
    stalled_revision = _normalize_sql(sql) == _normalize_sql(state.sql)
    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "stalled_revision": stalled_revision,
        "history": state.history + [{
            "node": "revise",
            "issue": state.verify_issue,
            "sql": sql,
            "stalled_revision": stalled_revision,
        }],
    }


def route_after_verify(state: AgentState) -> str:
    """Conditional router: return "revise" to loop, "end" to terminate.

    Two reasons to end: the verifier was happy (state.verify_ok), or you've hit
    the iteration cap (state.iteration >= MAX_ITERATIONS). Otherwise, revise.
    """
    if state.verify_ok or state.iteration >= MAX_ITERATIONS:
        return "end"
    return "revise"


def route_after_revise(state: AgentState) -> str:
    """End early when revise produced no substantive SQL change."""
    if state.stalled_revision:
        return "end"
    return "execute"


# ---- Graph wiring -----------------------------------------------------

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("attach_schema", _attach_schema)
    g.add_node("generate_sql", generate_sql_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("revise", revise_node)

    g.add_edge(START, "attach_schema")
    g.add_edge("attach_schema", "generate_sql")
    g.add_edge("generate_sql", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"revise": "revise", "end": END},
    )
    g.add_conditional_edges(
        "revise",
        route_after_revise,
        {"execute": "execute", "end": END},
    )
    return g.compile()


graph = build_graph()
