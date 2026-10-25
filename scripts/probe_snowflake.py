"""Connect to Snowflake, optionally set up cost guardrails, create the schema, and
check that Cortex AI_EMBED / AI_COMPLETE respond.

Run:  python -m scripts.probe_snowflake [--guardrails]
      --guardrails needs ACCOUNTADMIN: X-Small warehouse, AUTO_SUSPEND=60, and a
      resource monitor that suspends at SNOWFLAKE_CREDIT_CAP credits per month.
"""
import argparse

from store.snowflake import SnowflakeStore


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guardrails", action="store_true")
    args = ap.parse_args()
    s = SnowflakeStore()
    if args.guardrails:
        s.setup_account_guardrails()
        print("guardrails applied")
    s.create_schema()
    print("schema ok")
    print("embed dims:", len(s.embed_texts(["Bills line moves after injury news"])[0]))
    print("complete:", s.complete("Reply with the single word OK.")[:80])


if __name__ == "__main__":
    main()
