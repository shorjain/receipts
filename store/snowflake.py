"""Snowflake implementation of the store, including Cortex AI functions and the
account-level cost guardrails (X-Small warehouse, auto-suspend, resource monitor)."""
from __future__ import annotations

import json
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
    }
    key_path = env("SNOWFLAKE_PRIVATE_KEY_PATH")
    if key_path:
        kw["private_key_file"] = key_path
    else:
        kw["password"] = env("SNOWFLAKE_PASSWORD")
    return kw


class SnowflakeStore(SqlStore):
    dialect = "snowflake"
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

    # ---- Cortex -------------------------------------------------------------
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = env("SNOWFLAKE_EMBED_MODEL", "snowflake-arctic-embed-m-v1.5")
        out = []
        for t in texts:
            row = self._query("SELECT AI_EMBED(?, ?)::ARRAY AS e", [model, t])[0]["e"]
            out.append(json.loads(row) if isinstance(row, str) else list(row))
        return out

    def complete(self, prompt: str, temperature: float = 0.0) -> str:
        model = env("SNOWFLAKE_COMPLETE_MODEL", "claude-sonnet-4-5")
        row = self._query(
            "SELECT AI_COMPLETE(model => ?, prompt => ?, "
            "model_parameters => PARSE_JSON(?)) AS r",
            [model, prompt, json.dumps({"temperature": temperature})],
        )[0]["r"]
        return row if isinstance(row, str) else json.dumps(row)

    def close(self) -> None:
        if self.con:
            self.con.close()
