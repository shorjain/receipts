"""Repo rules from the brief: no SQL outside store/, no secrets committed."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL = re.compile(r"\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|MERGE\s+INTO|CREATE\s+(OR\s+REPLACE\s+)?TABLE|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b",
                 re.IGNORECASE)


def py_files():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in (".venv", "store", "tests") or "site-packages" in rel.parts:
            continue
        yield p


def test_no_sql_outside_store():
    offenders = [str(p.relative_to(ROOT)) for p in py_files() if SQL.search(p.read_text())]
    assert offenders == []


def test_env_example_has_no_values_for_secrets():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if re.match(r"^\w*(KEY|PASSWORD|SECRET|TOKEN)\w*=", line) and not line.endswith("_PATH="):
            name, _, value = line.partition("=")
            assert value == "" or name.endswith(("_MIN_CREDITS", "_LIVE_MIN_CREDITS")), line
