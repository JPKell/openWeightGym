"""Small DB-API adapters that keep repository SQL independent of parameter style."""

from __future__ import annotations

from typing import Any, Iterable, Iterator


def _qmark(sql: str, *, parameters: bool = False) -> str:
    """Adapt qmarks and percent literals while preserving native psycopg bind markers."""
    out: list[str] = []
    quote = ""
    line_comment = block_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if parameters and char == "%":
            if not (quote or line_comment or block_comment) and following in ("s", "b", "t"):
                out.extend((char, following))
                index += 2
            else:
                out.append("%%")
                index += 1
            continue
        if line_comment:
            out.append(char)
            if char == "\n":
                line_comment = False
        elif block_comment:
            out.append(char)
            if char == "*" and following == "/":
                out.append(following)
                index += 1
                block_comment = False
        elif quote:
            out.append(char)
            if char == quote:
                if following == quote:
                    out.append(following)
                    index += 1
                else:
                    quote = ""
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "-" and following == "-":
            out.extend((char, following))
            index += 1
            line_comment = True
        elif char == "/" and following == "*":
            out.extend((char, following))
            index += 1
            block_comment = True
        elif char == "?":
            out.append("%s")
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            word = sql[index:end]
            out.append('"user"' if word.lower() == "user" else word)
            index = end - 1
        else:
            out.append(char)
        index += 1
    return "".join(out)


def split_script(script: str) -> list[str]:
    """Split a migration script on unquoted semicolons."""
    statements: list[str] = []
    start = 0
    quote = ""
    line_comment = block_comment = False
    index = 0
    while index < len(script):
        char = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and following == "/":
                index += 1
                block_comment = False
        elif quote:
            if char == quote:
                if following == quote:
                    index += 1
                else:
                    quote = ""
        elif char in "'\"":
            quote = char
        elif char == "-" and following == "-":
            index += 1
            line_comment = True
        elif char == "/" and following == "*":
            index += 1
            block_comment = True
        elif char == ";":
            if statement := script[start:index].strip():
                statements.append(statement)
            start = index + 1
        index += 1
    if statement := script[start:].strip():
        statements.append(statement)
    return statements


class Cursor:
    """Cursor proxy retaining the ordinary tuple-row behavior openWeightGym repositories expect."""

    def __init__(self, cursor) -> None:
        """Wrap an engine cursor without changing its tuple-row behavior."""
        self._cursor = cursor

    def __iter__(self) -> Iterator[tuple]:
        """Iterate over the underlying records in their defined order."""
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        """Delegate unsupported attributes to the wrapped object."""
        return getattr(self._cursor, name)

    def fetchone(self):
        """Return the next result row, or None after the cursor is exhausted."""
        return self._cursor.fetchone()

    def fetchall(self):
        """Return all remaining result rows."""
        return self._cursor.fetchall()


class Connection:
    """A narrow DB-API connection proxy shared by SQLite and PostgreSQL repositories."""

    def __init__(self, database, raw) -> None:
        """Bind a raw DB-API connection to its portable database definition."""
        object.__setattr__(self, "database", database)
        object.__setattr__(self, "raw", raw)

    @property
    def engine(self) -> str:
        """Return the configured database engine name."""
        return self.database.engine

    def sql(self, statement: str, *, parameters: bool = False) -> str:
        """Translate portable SQL, escaping percent literals only for bound parameters."""
        if self.engine == "sqlite":
            return statement
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            return "BEGIN"
        # psycopg parses percent markers inside strings/comments too. Native bind
        # markers from driver-generated SQL must remain parameters, not literals.
        return _qmark(statement, parameters=parameters)

    def execute(self, statement: str, parameters=()) -> Cursor:
        """Execute one parameterized statement and wrap its result cursor."""
        parameters = tuple(int(value) if isinstance(value, bool) else value for value in parameters) if self.engine == "postgresql" else parameters
        sql = self.sql(statement, parameters=bool(parameters))
        return Cursor(self.raw.execute(sql) if not parameters else self.raw.execute(sql, parameters))

    def executemany(self, statement: str, parameters: Iterable) -> Cursor:
        """Execute one statement for each parameter row on the active engine."""
        if self.engine == "postgresql":
            parameters = [tuple(int(value) if isinstance(value, bool) else value for value in row) for row in parameters]
            cursor = self.raw.cursor()
            cursor.executemany(self.sql(statement, parameters=True), parameters)
            return Cursor(cursor)
        return Cursor(self.raw.executemany(statement, parameters))

    def executescript(self, script: str) -> None:
        """Execute a migration script using the active engine's statement rules."""
        if self.engine == "sqlite":
            self.raw.executescript(script)
            return
        for statement in split_script(script):
            self.execute(statement)

    def insert_id(self, statement: str, parameters=(), column: str = "id") -> int:
        """Execute an insert and return its generated integer identity on either engine."""
        if self.engine == "postgresql":
            row = self.execute(f"{statement.rstrip().rstrip(';')} RETURNING {self.database.quote_identifier(column)}", parameters).fetchone()
            return int(row[0])
        return int(self.execute(statement, parameters).lastrowid)

    def greatest(self, *expressions: str) -> str:
        """Return a scalar greatest-value expression for trusted SQL fragments."""
        if not expressions:
            raise ValueError("greatest requires at least one expression")
        function = "MAX" if self.engine == "sqlite" else "GREATEST"
        return f"{function}({', '.join(expressions)})"

    def backup(self, target, *args, **kwargs):
        """Run SQLite's online backup API while accepting another openWeightGym connection proxy."""
        return self.raw.backup(getattr(target, "raw", target), *args, **kwargs)

    def __enter__(self):
        """Enter the connection transaction context."""
        return self

    def __exit__(self, kind, value, traceback):
        """Commit a successful transaction or roll it back after an exception."""
        if kind is None:
            try:
                self.raw.commit()
            except BaseException:
                self.raw.rollback()
                raise
        else:
            self.raw.rollback()
        return False

    def __getattr__(self, name: str) -> Any:
        """Delegate unsupported attributes to the wrapped object."""
        return getattr(self.raw, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep wrapper state locally and delegate other attribute assignments."""
        if name in {"database", "raw"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.raw, name, value)
