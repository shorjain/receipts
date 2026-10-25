"""Snowflake implementation of the store, including Cortex AI functions and the
account-level cost guardrails (X-Small warehouse, auto-suspend, resource monitor)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

import snowflake.connector

from common.settings import env, env_int
from .base import SqlStore
from .schema import EMBED_DIM, JSON, VEC

snowflake.connector.paramstyle = "qmark"


def _connect_kwargs() -> dict:
    kw = {
        "account": env("SNOWFLAKE_ACCOUNT"), "user": env("SNOWFLAKE_USER"),
        "role": env("SNOWFLAKE_ROLE"), "warehouse": env("SNOWFLAKE_WAREHOUSE"),
        "database": env("SNOWFLAKE_DATABASE"), "schema": env("SNOWFLAKE_SCHEMA", "PUBLIC"),
        "client_session_keep_alive": False,
        "enable_server_session_keep_alive_auto_detection": False,
    }
    key_path = env("SNOWFLAKE_PRIVATE_KEY_PATH")
    if key_path:  # key-pair (JWT) sign-in; the password is ignored
        kw["authenticator"] = "SNOWFLAKE_JWT"
        kw["private_key_file"] = str(Path(key_path).expanduser())
    else:
        kw["password"] = env("SNOWFLAKE_PASSWORD")
    return kw


class SnowflakeStore(SqlStore):
    dialect = "snowflake"
    sim_range = (0.65, 0.90)   # typical AI_EMBED cosine range: unrelated .. closely related
    TYPE_MAP = {
        "STR": "VARCHAR", "INT": "NUMBER(38,0)", "FLOAT": "FLOAT", "BOOL": "BOOLEAN",
        "TS": "TIMESTAMP_NTZ", "JSON": "VARIANT", "VEC": f"VECTOR(FLOAT, {EMBED_DIM})",
    }

    def __init__(self, connect: bool = True):
        self.con = snowflake.connector.connect(**_connect_kwargs()) if connect else None

    def _execute(self, sql: str, params: Sequence | None = None) -> None:
        with self.con.cursor() as cur:
            cur.execute(sql, params or None)

    def _executemany(self, sql: str, rows: list[Sequence]) -> None:
        with self.con.cursor() as cur:
            cur.executemany(sql, rows)

    def _query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        with self.con.cursor(snowflake.connector.DictCursor) as cur:
            cur.execute(sql, params or None)
            return [{k.lower(): v for k, v in row.items()} for row in cur.fetchall()]

    def _cast_from_text(self, expr: str, col_type: str) -> str:
        if col_type == VEC:
            return f"PARSE_JSON({expr})::ARRAY::VECTOR(FLOAT, {EMBED_DIM})"
        if col_type == JSON:
            return f"PARSE_JSON({expr})"
        return expr

    # ---- cost guardrails (run once, needs ACCOUNTADMIN) -------------------
    def setup_account_guardrails(self) -> None:
        wh, db = env("SNOWFLAKE_WAREHOUSE"), env("SNOWFLAKE_DATABASE")
        cap = env_int("SNOWFLAKE_CREDIT_CAP", 20)
        role = env("SNOWFLAKE_ROLE", "SYSADMIN")
        stmts = [
            # Account-level pieces need ACCOUNTADMIN.
            "USE ROLE ACCOUNTADMIN",
            f"CREATE WAREHOUSE IF NOT EXISTS {wh} WAREHOUSE_SIZE = 'XSMALL' "
            "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE",
            f"ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60",
            f"CREATE OR REPLACE RESOURCE MONITOR NFL_LINES_MONITOR WITH CREDIT_QUOTA = {cap} "
            "FREQUENCY = MONTHLY START_TIMESTAMP = IMMEDIATELY "
            "TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND "
            "ON 110 PERCENT DO SUSPEND_IMMEDIATE",
            f"ALTER WAREHOUSE {wh} SET RESOURCE_MONITOR = NFL_LINES_MONITOR",
            f"GRANT USAGE, OPERATE ON WAREHOUSE {wh} TO ROLE {role}",
            # The app's role creates (and so owns) the database and its schema.
            f"USE ROLE {role}",
            f"USE WAREHOUSE {wh}",
            f"CREATE DATABASE IF NOT EXISTS {db}",
            f"USE DATABASE {db}",
            f"CREATE SCHEMA IF NOT EXISTS {env('SNOWFLAKE_SCHEMA', 'PUBLIC')}",
            f"USE SCHEMA {env('SNOWFLAKE_SCHEMA', 'PUBLIC')}",
        ]
        for s in stmts:
            self._execute(s)

    # ---- sync helpers ---------------------------------------------------------
    def suspend_warehouse(self) -> None:
        """Suspend right after a batch job instead of idling for AUTO_SUSPEND seconds."""
        try:
            self._execute(f"ALTER WAREHOUSE {env('SNOWFLAKE_WAREHOUSE')} SUSPEND")
        except snowflake.connector.errors.ProgrammingError as e:
            if "cannot be suspended" not in str(e) and "not running" not in str(e):
                raise

    def embed_missing(self, table: str, text_col: str) -> int:
        """Fill NULL embeddings server-side with AI_EMBED (no data leaves Snowflake)."""
        model = self._model_literal(env("SNOWFLAKE_EMBED_MODEL", "snowflake-arctic-embed-m-v1.5"))
        n = self._query(f"SELECT COUNT(*) AS n FROM {table} WHERE embedding IS NULL "
                        f"AND {text_col} IS NOT NULL AND {text_col} <> ''")[0]["n"]
        if n:
            self._execute(f"UPDATE {table} SET embedding = AI_EMBED({model}, {text_col}) "
                          f"WHERE embedding IS NULL AND {text_col} IS NOT NULL AND {text_col} <> ''")
        return int(n)

    # ---- Cortex -------------------------------------------------------------
    @staticmethod
    def _model_literal(name: str) -> str:
        """Cortex wants the model as a string literal, not a bind; allow only safe names."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"unsafe model name: {name!r}")
        return f"'{name}'"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = self._model_literal(env("SNOWFLAKE_EMBED_MODEL", "snowflake-arctic-embed-m-v1.5"))
        out = []
        for t in texts:
            row = self._query(f"SELECT AI_EMBED({model}, ?)::ARRAY AS e", [t])[0]["e"]
            out.append(json.loads(row) if isinstance(row, str) else list(row))
        return out

    def _similarity_sql(self, query: str | None) -> tuple[str, list] | None:
        if not query:
            return None
        model = self._model_literal(env("SNOWFLAKE_EMBED_MODEL", "snowflake-arctic-embed-m-v1.5"))
        return f"VECTOR_COSINE_SIMILARITY(embedding, AI_EMBED({model}, ?))", [query]

    def similarities(self, query: str, texts: list[str]) -> list[float]:
        """Cosine similarity of AI_EMBED(query) with AI_EMBED(each text), one round trip."""
        if not texts:
            return []
        model = self._model_literal(env("SNOWFLAKE_EMBED_MODEL", "snowflake-arctic-embed-m-v1.5"))
        values = ", ".join("(?, ?)" for _ in texts)
        params: list = [query]
        for i, t in enumerate(texts):
            params += [i, t or ""]
        rows = self._query(
            f"WITH q AS (SELECT AI_EMBED({model}, ?) AS e) "
            f"SELECT v.column1 AS i, VECTOR_COSINE_SIMILARITY(q.e, AI_EMBED({model}, v.column2)) AS s "
            f"FROM (VALUES {values}) v, q", params)
        by_i = {int(r["i"]): float(r["s"]) for r in rows}
        return [by_i.get(i, 0.0) for i in range(len(texts))]

    def complete_json(self, prompt: str, schema: dict, model: str | None = None) -> dict:
        """AI_COMPLETE with structured output: the reply is validated against `schema`."""
        model = self._model_literal(model or env("SNOWFLAKE_EXTRACT_MODEL", env("SNOWFLAKE_COMPLETE_MODEL", "claude-sonnet-4-5")))
        row = self._query(
            f"SELECT AI_COMPLETE(model => {model}, prompt => ?, model_parameters => {{'temperature': 0}}, "
            "response_format => PARSE_JSON(?)) AS r",
            [prompt, json.dumps({"type": "json", "schema": schema})])[0]["r"]
        return json.loads(row) if isinstance(row, str) else row

    def complete(self, prompt: str, temperature: float = 0.0) -> str:
        model = self._model_literal(env("SNOWFLAKE_COMPLETE_MODEL", "claude-sonnet-4-5"))
        row = self._query(
            f"SELECT AI_COMPLETE(model => {model}, prompt => ?, "
            "model_parameters => PARSE_JSON(?)) AS r",
            [prompt, json.dumps({"temperature": temperature})],
        )[0]["r"]
        return row if isinstance(row, str) else json.dumps(row)

    def close(self) -> None:
        if self.con:
            self.con.close()
