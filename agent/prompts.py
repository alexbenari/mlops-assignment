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
- Read the inline schema comments carefully; they contain column semantics and allowed value hints.
- Copy table and column names exactly as they appear in the schema.
- Produce a read-only SQLite query that answers the question directly.
- Prefer explicit joins over subqueries when either is fine.
- Interpret temporal phrases precisely: for example, "starting from 1997" means year >= 1997, not only the year 1997.
- Use DISTINCT when joins could duplicate the requested entity or attribute rows.
- For coded categorical columns, copy the canonical values shown in the schema comments exactly, including capitalization, punctuation, and spacing.
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

Mark ok=true only when the SQL and execution result look plausibly correct for
both content and answer shape.

Mark ok=false whenever there is strong evidence that the query answered a different question, returned the wrong shape
of answer, or used the wrong semantics even if it executed successfully.

Typical reasons for ok=false:
- the SQL errored
- zero rows are unlikely to be a valid answer
- duplicate rows suggest the join is too loose
- the selected columns are clearly wrong, for example they are not the ones
  requested or do not appear in the schema
- the specified columns are a near match to schema columns but do not exactly
  match the schema because of capitalization, wording, or normalization
  differences, which indicates the SQL is not using the schema correctly
- the answer shape does not match what the question asks for: for example it returns COUNT when the question asks
  to list rows or returns an ID when the question asks for an attribute

If ok=true, keep issue short and empty if possible.
If ok=false, issue should contain a concrete and actionable explanation, specific enough to guide the next
revision.
- describe exactly what is wrong
- if the SQL errored, include the error message
- name the specific column / literal / aggregation / answer-shape problem
- say what should be changed next

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
to repair the query.

Revision rules:
- Make the smallest substantive fix that addresses the verifier issue.
- Keep correct parts of the previous SQL. Do not rewrite the whole query unless
  the structure is clearly wrong.
- Do not repeat the same SQL
- avoid sql query anti-patterns which may cause long-running queries

The schema is authoritative: every table and column name in your answer must
appear there exactly. If the failure says a table or column does not exist,
rewrite the query using only valid identifiers from the schema.
Use schema comments to choose the right coded values and semantically correct columns.
Copy coded values from the schema exactly, including capitalization, punctuation, and spacing.
Preserve exact timestamp/text literals when the data appears to store them in a
specific format.
Keep temporal conditions faithful to the wording: "starting from" means
inclusive lower bound, not exact equality.
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
