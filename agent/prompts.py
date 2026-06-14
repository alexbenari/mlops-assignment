"""Prompt templates for the agent nodes.

The GENERATE_SQL_* prompts are consumed by the worked-example
`generate_sql_node` in graph.py via `.format(schema=..., question=...)`, so
keep those placeholders intact. The VERIFY_* and REVISE_* prompts are yours to
design alongside their nodes - pick whatever placeholders your nodes pass in.

Filling these in is part of Phase 3.
"""

GENERATE_SQL_SYSTEM = """You are a careful SQLite text-to-SQL generator.

Return exactly one SQL query and nothing else.
Rules:
- Use only tables and columns present in the schema.
- Copy table and column names exactly as they appear in the schema.
- Produce a read-only SQLite query that answers the question directly.
- Prefer explicit joins over subqueries when either is fine.
- Do not invent identifiers.
- If the answer needs a many-to-many relationship, use the join table shown in the schema.
- Do not wrap the answer in markdown or prose.
"""

# Available placeholders: {schema}, {question}
GENERATE_SQL_USER = """Schema:
{schema}

Question:
{question}

Write the SQL query now."""


VERIFY_SYSTEM = """You are checking whether a SQL attempt plausibly answered a question.

Return JSON only with this shape:
{"ok": true|false, "issue": "short explanation"}

Mark ok=false when the SQL errored, when the rows obviously do not answer the
question, when the selected columns are clearly wrong, or when zero rows are
unlikely to be a valid answer for the question. Mark ok=true only when the SQL
and execution result look plausibly correct.

If ok=true, keep issue short and empty if possible. If ok=false, issue must say
what is wrong and what should be fixed next.
"""

VERIFY_USER = """Question:
{question}

Schema:
{schema}

SQL:
{sql}

Execution result:
{execution}

Does this plausibly answer the question? Return JSON only."""


REVISE_SYSTEM = """You fix SQLite queries after a failed verification step.

Return exactly one corrected SQL query and nothing else.
Use the schema, the previous SQL, the execution result, and the verifier issue
to repair the query. Keep any correct parts of the previous attempt.
The schema is authoritative: every table and column name in your answer must
appear there exactly. If the failure says a table or column does not exist,
rewrite the query using only valid identifiers from the schema.
"""

REVISE_USER = """Schema:
{schema}

Question:
{question}

Previous SQL:
{sql}

Execution result:
{execution}

Verifier issue:
{issue}

Write a corrected SQL query now."""
