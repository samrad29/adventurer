"""Execute a SQL script inside an Alembic revision.

Statements are split on semicolons, with dollar-quoting and string literals
left intact. Scripts avoid ``%`` so the database driver does not treat them
as parameter placeholders.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


def execute_script(path: Path) -> None:
    connection = op.get_bind()
    for statement in split_sql(path.read_text(encoding="utf-8")):
        connection.exec_driver_sql(statement)


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(script)
    dollar: str | None = None
    while i < length:
        if dollar is not None:
            if script.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                buf.append(script[i])
                i += 1
            continue
        if script.startswith("--", i):
            while i < length and script[i] != "\n":
                buf.append(script[i])
                i += 1
            continue
        if script[i] == "'":
            buf.append("'")
            i += 1
            while i < length:
                if script[i] == "'":
                    if i + 1 < length and script[i + 1] == "'":
                        buf.append("''")
                        i += 2
                        continue
                    buf.append("'")
                    i += 1
                    break
                buf.append(script[i])
                i += 1
            continue
        if script[i] == "$":
            tag_end = i + 1
            while tag_end < length and (script[tag_end].isalnum() or script[tag_end] == "_"):
                tag_end += 1
            if tag_end < length and script[tag_end] == "$":
                dollar = script[i : tag_end + 1]
                buf.append(dollar)
                i = tag_end + 1
                continue
        if script[i] == ";":
            statement = "".join(buf).strip()
            if statement and not _comments_only(statement):
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(script[i])
        i += 1
    tail = "".join(buf).strip()
    if tail and not _comments_only(tail):
        statements.append(tail)
    return statements


def _comments_only(statement: str) -> bool:
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True
