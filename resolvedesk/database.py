"""Repository connections with PostgreSQL pooling and explicit SQLite test mode.

Legacy repositories use qmark parameters and sqlite.Row's keys/index interface.
The adapter binds parameters through SQLAlchemy; it never interpolates values.
Only the existing portable CREATE TABLE/INDEX DDL subset is translated.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def bindings(sql, values):
    if isinstance(values, dict):
        return sql, values
    values = tuple(values or ())
    output, parameters, index, quote, line_comment, block_comment = [], {}, 0, None, False, False
    pos = 0
    while pos < len(sql):
        char = sql[pos]
        next_char = sql[pos+1] if pos+1 < len(sql) else ""
        if line_comment:
            output.append(char)
            if char == "\n": line_comment = False
        elif block_comment:
            output.append(char)
            if char == "*" and next_char == "/":
                output.append(next_char); pos += 1; block_comment = False
        elif quote:
            output.append(char)
            if char == quote:
                if next_char == quote:
                    output.append(next_char); pos += 1
                else: quote = None
        elif char in {"'", '"'}:
            quote = char; output.append(char)
        elif char == "-" and next_char == "-":
            output += [char, next_char]; pos += 1; line_comment = True
        elif char == "/" and next_char == "*":
            output += [char, next_char]; pos += 1; block_comment = True
        elif char == "?":
            if index >= len(values): raise ValueError("Not enough SQL parameters")
            name = "p"+str(index)
            output.append(":"+name); parameters[name] = values[index]; index += 1
        else: output.append(char)
        pos += 1
    if index != len(values): raise ValueError("Too many SQL parameters")
    return "".join(output), parameters


class Row:
    """The small keys/index/iteration contract already used by repositories."""
    def __init__(self, row):
        self._mapping, self._values = dict(row._mapping), tuple(row)

    def keys(self): return self._mapping.keys()
    def __getitem__(self, key): return self._values[key] if isinstance(key, (int, slice)) else self._mapping[key]
    def __iter__(self): return iter(self._values)
    def __len__(self): return len(self._values)


class Cursor:
    def __init__(self, result): self.result = result
    @property
    def rowcount(self): return self.result.rowcount
    def fetchone(self):
        row = self.result.fetchone()
        return Row(row) if row is not None else None
    def fetchall(self): return [Row(row) for row in self.result.fetchall()]
    def __iter__(self): return (Row(row) for row in self.result)


class Connection:
    def __init__(self, connection, backend, write, schema):
        self.connection, self.backend, self.write, self.schema = connection, backend, write, schema

    def locked(self, sql):
        return sql+" FOR UPDATE" if self.backend == "postgresql" and self.write else sql

    def now(self):
        # SQL owns lease expiry across worker hosts with different wall clocks.
        return float(self.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0]) if self.backend=="postgresql" else time.time()

    def execute(self, sql, values=()):
        if self.backend=="postgresql" and re.match(r"^\s*CREATE TABLE IF NOT EXISTS\b",sql,re.I):
            sql=sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT","BIGSERIAL PRIMARY KEY")
            sql=re.sub(r"\bREAL\b","DOUBLE PRECISION",sql)
        statement, parameters = bindings(sql, values)
        return Cursor(self.connection.execute(text(statement), parameters))

    def has_column(self, table, column):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table): raise ValueError("Invalid table name")
        if self.backend == "postgresql":
            return self.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=? AND table_name=? AND column_name=?", (self.schema,table,column)).fetchone() is not None
        return column in {r[1] for r in self.execute('PRAGMA table_info("'+table+'")')}

    def executescript(self, ddl):
        # This API only accepts repository-owned table/index declarations, no SQL
        # functions, triggers, user data, or scripts with quoted semicolons.
        for statement in ddl.split(";"):
            statement = statement.strip()
            if not statement: continue
            if not re.match(r"^CREATE (?:TABLE|INDEX) IF NOT EXISTS\b", statement, re.I):
                raise ValueError("Unsupported repository DDL")
            if self.backend == "postgresql":
                statement = statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                statement = re.sub(r"\bREAL\b", "DOUBLE PRECISION", statement)
            self.execute(statement)


class Database:
    def __init__(self, location, schema="public"):
        raw = str(location)
        self.path = None
        if raw.startswith(("postgresql://", "postgresql+psycopg://")):
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema): raise ValueError("Invalid database schema")
            url = make_url(raw).set(drivername="postgresql+psycopg")
            self.backend, self.schema = "postgresql", schema
            self.engine = create_engine(url, pool_size=5, max_overflow=5, pool_timeout=5,
                pool_pre_ping=True, connect_args={"connect_timeout":5,
                    "options":f"-c search_path={schema} -c statement_timeout=15000 -c lock_timeout=5000"})
            with self.engine.begin() as conn:
                conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS "'+schema+'"')
        else:
            if "://" in raw: raise ValueError("Unsupported database backend")
            self.backend, self.schema = "sqlite", None
            self.path = Path(location).absolute()
            for ancestor in (self.path, *self.path.parents):
                if ancestor.is_symlink() or ancestor.is_junction(): raise ValueError("Database paths must not follow links")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine("sqlite:///"+self.path.as_posix(), poolclass=NullPool,
                isolation_level="AUTOCOMMIT", connect_args={"timeout":10, "check_same_thread":False})

    @contextmanager
    def connect(self, write=False):
        with self.engine.connect() as conn:
            try:
                if self.backend == "sqlite" and write: conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield Connection(conn, self.backend, write, self.schema)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def schema_transaction(self):
        with self.connect(True) as db:
            if self.backend == "postgresql":
                key = int.from_bytes(hashlib.sha256(("resolvedesk-schema:"+self.schema).encode()).digest()[:8], "big", signed=True)
                db.execute("SELECT pg_advisory_xact_lock(?)", (key,))
            yield db

    def has_column(self, table, column):
        with self.connect() as db:
            return db.has_column(table,column)

    def close(self): self.engine.dispose()
