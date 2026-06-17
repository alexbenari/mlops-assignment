"""Schema-rendering helper (provided complete).

Loads the schema directly from sqlite and renders quoted CREATE TABLE
text suitable for prompt context. Identifiers are always double-quoted
so reserved-word table/column names (e.g. `order`) don't break either
the PRAGMA introspection here or the SQL the model emits later.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "data" / "bird"
DB_DESC_ROOT = DB_DIR / "dev_20240627" / "dev_databases"


def db_path(db_id: str) -> Path:
    return DB_DIR / f"{db_id}.sqlite"


def _q(ident: str) -> str:
    """Double-quote a SQL identifier, escaping any embedded quotes."""
    return '"' + ident.replace('"', '""') + '"'


def _render_foreign_key(fk: tuple[Any, ...]) -> str | None:
    """Render a foreign key clause, tolerating incomplete SQLite metadata."""
    ref_table = fk[2]
    from_col = fk[3]
    to_col = fk[4]
    if ref_table is None or from_col is None:
        return None

    clause = f"  FOREIGN KEY ({_q(from_col)}) REFERENCES {_q(ref_table)}"
    if to_col is not None:
        clause += f"({_q(to_col)})"
    return clause


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@lru_cache(maxsize=256)
def _column_hints(db_id: str, table: str) -> dict[str, str]:
    desc_file = DB_DESC_ROOT / db_id / "database_description" / f"{table}.csv"
    if not desc_file.exists():
        return {}

    hints: dict[str, str] = {}
    raw = desc_file.read_bytes()
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    for row in csv.DictReader(text.splitlines()):
        name = (row.get("original_column_name") or "").strip()
        if not name:
            continue

        column_name = (row.get("column_name") or "").strip()
        description = (row.get("column_description") or "").strip()
        value_description = (row.get("value_description") or "").strip()

        useful_value = bool(value_description and _normalized(value_description) != "not useful")
        opaque_name = bool(re.fullmatch(r"[A-Za-z]\d+", name) or len(name) <= 3)
        if not (useful_value or opaque_name):
            continue

        raw_parts = [column_name, description, value_description]
        seen: set[str] = set()
        parts: list[str] = []
        for part in raw_parts:
            cleaned = part.replace("\n", " ").strip()
            if not cleaned:
                continue
            norm = _normalized(cleaned)
            if not norm or norm == _normalized(name) or norm in seen or norm == "not useful":
                continue
            seen.add(norm)
            parts.append(cleaned)

        hint = "; ".join(parts)
        if hint:
            hints[name] = hint
    return hints


@lru_cache(maxsize=32)
def render_schema(db_id: str) -> str:
    path = db_path(db_id)
    if not path.exists():
        raise FileNotFoundError(f"DB {db_id} not found at {path}. Did you run scripts/load_data.py?")

    parts: list[str] = [f"-- Database: {db_id}"]
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        for t in tables:
            parts.append(f"\nCREATE TABLE {_q(t)} (")
            col_lines: list[str] = []
            column_hints = _column_hints(db_id, t)
            for _cid, name, ctype, notnull, _dflt, pk in conn.execute(f"PRAGMA table_info({_q(t)})"):
                hint = column_hints.get(name)
                if hint:
                    col_lines.append(f"  -- {name}: {hint}")
                line = f"  {_q(name)} {ctype}"
                if pk:
                    line += " PRIMARY KEY"
                if notnull and not pk:
                    line += " NOT NULL"
                col_lines.append(line)
            for fk in conn.execute(f"PRAGMA foreign_key_list({_q(t)})"):
                # (id, seq, ref_table, from, to, on_update, on_delete, match)
                clause = _render_foreign_key(fk)
                if clause:
                    col_lines.append(clause)
            parts.append(",\n".join(col_lines))
            parts.append(");")
    return "\n".join(parts)


def available_dbs() -> list[str]:
    if not DB_DIR.exists():
        return []
    return sorted(p.stem for p in DB_DIR.glob("*.sqlite"))
