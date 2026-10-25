"""Ask: grounded answers over moves, posts, claims, insider scores and predictions.

  1. rag.parse turns the question into filters (teams, players, insiders, claim types,
     time window, intent).
  2. Retrieval filters with SQL and, in Snowflake, ranks posts and moves by
     VECTOR_COSINE_SIMILARITY to the question (store.ask_* functions).
  3. The evidence becomes a numbered block: [M#] moves, [P#] posts, [C#] claims,
     [S#] insider stats, [G#] model predictions. Insider stats for the asked-about
     claim types and window are computed on the spot with score_insiders' formulas.
  4. AI_COMPLETE at temperature 0 answers from that block only.
  5. rag.guard drops sentences with numbers not in the evidence and uncited causal
     claims, strips unknown citations, and refuses instead of guessing. With no
     evidence at all, it refuses without calling the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rag import guard as g
from rag.parse import Query, parse
from rag.retrieve import gather

NO_DATA = "I don't have stored data that answers that."
GUARD_FAILED = "The model's draft didn't survive the grounding checks. See the evidence below."

PROMPT = """Answer the question about NFL betting lines and NFL insiders using only the numbered evidence.

Rules:
- At most four sentences, plain prose, no lists or headings.
- Write citations in square brackets right after the claim, exactly like [M1] or [P2][C3]. Never write "as seen in P2".
- Copy numbers exactly as written in the evidence. Never compute, round, convert or estimate a number.
- Every claim about a cause needs a citation.
- A post published after a move reports on it; it didn't cause it.
- If the evidence doesn't answer the question, say so instead of guessing.

Question: {question}

Evidence:
{evidence}

Answer:"""


@dataclass
class Answer:
    question: str
    text: str
    evidence: list[dict] = field(default_factory=list)   # {id, kind, line, url}
    query: Query | None = None
    refused: bool = False
    dropped: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def answer(question: str, store, ai, now: datetime | None = None, roster: list[dict] | None = None,
           insider_rows: list[dict] | None = None) -> Answer:
    from common.settings import insiders
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    season = max((x["season"] for x in store.get_games()), default=now.year)
    season_start = datetime(season, 8, 1)
    weeks: dict[int, list[datetime]] = {}
    for x in store.get_games(season):
        weeks.setdefault(x["week"], []).append(x["kickoff_at"])
    week_ranges = {w: (min(k) - timedelta(days=4), max(k) + timedelta(hours=4)) for w, k in weeks.items()}
    roster = roster if roster is not None else store.roster(season)
    q = parse(question, now, roster, insider_rows if insider_rows is not None else insiders(), season_start, week_ranges)
    items = gather(store, q, now, season_start)
    if not items:
        scope = ", ".join(q.teams + [p["full_name"] for p in q.players] + ["@" + h for h in q.insiders]) or "that"
        window = f" in {q.window_label}" if q.window_label else ""
        return Answer(question, f"I don't have stored moves, posts, claims or predictions for {scope}{window}, so I "
                                f"can't answer from evidence. I won't answer from general knowledge.", [], q, refused=True)
    evidence = "\n".join(f"[{it['id']}] {it['line']}" for it in items)
    reply = ai.complete(PROMPT.format(question=question, evidence=evidence), temperature=0)
    res = g.guard(reply.strip().strip('"'), evidence, {it["id"] for it in items}, set(), max_sentences=4,
                  post_prefix=("P", "C", "M", "S", "G"), require_citation=True, extra_context=question)
    warnings = list(res.warnings)
    cited = set(g.CITE.findall(res.text))
    if any(it["kind"] == "P" for it in items) and q.intent == "moves" and not any(c.startswith("P") for c in cited):
        warnings.append("the answer cites no posts, so it explains no cause")
    text = res.text if not res.refused else GUARD_FAILED
    return Answer(question, text, items, q, refused=res.refused, dropped=res.dropped, warnings=warnings)


def ask_backends():
    """(store, ai) for Ask.

    AI_BACKEND=snowflake: retrieval in Snowflake, which holds the Cortex embeddings
      (data up to one sync old); ASK_STORE=local retrieves from the loop's fresher local
      snapshot instead (recency ranking) and still writes the answer with Cortex.
    AI_BACKEND=local: retrieval from the local snapshot with local vectors, answer from
      Ollama. Nothing leaves the machine."""
    from common.settings import env
    from pipelines.explain_moves import ai_backend, get_ai
    from store import get_read_store
    ai = get_ai()
    if ai is None:
        raise RuntimeError("Ask needs an AI backend: Snowflake (SNOWFLAKE_ACCOUNT) or local "
                           "(AI_BACKEND=local with `ollama serve` running)")
    if ai_backend() == "local" or env("ASK_STORE", "snowflake") == "local":
        return get_read_store(), ai
    return ai, ai
