"""Post-generation grounding guard, shared by move explanations and the Ask chatbot.

Given model output and the numbered evidence block it was written from:
  1. citations like [P7] that aren't in the evidence are stripped;
  2. a sentence containing a number that doesn't appear in the evidence is dropped;
  3. a causal sentence ("because", "after", "sparked", ...) with no post citation is dropped;
  4. a causal sentence whose only cited posts came after the move is dropped (those
     posts report on the move; they can't have caused it);
  5. at most max_sentences survive. If none do, the caller refuses instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE = re.compile(r"\[([A-Z]\d+)\]")
NUM = re.compile(r"(?<![A-Za-z\[])[-+]?\d+(?:\.\d+)?")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\"'(@])")
# Words that assert cause. "after"/"following" also describe timing, so rule 4 (after-
# the-move posts) uses only the stronger set.
CAUSAL = re.compile(r"\b(because|due to|after|following|driven by|drove|sparked|prompted|caused|"
                    r"triggered|in response to|as a result|on (?:the )?news|reacting to|led to)\b", re.I)
STRONG_CAUSAL = re.compile(r"\b(because|due to|driven by|drove|sparked|prompted|caused|triggered|"
                           r"in response to|as a result|led to)\b", re.I)
REFUSAL = "No grounded explanation: the evidence didn't support one."


@dataclass
class GuardResult:
    text: str
    kept: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return not self.kept


def numbers_in(text: str) -> list[float]:
    stripped = CITE.sub(" ", text)
    out = []
    for m in NUM.finditer(stripped):
        try:
            out.append(abs(float(m.group())))
        except ValueError:
            continue
    return out


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s.strip() for s in SENTENCE_END.split(text) if s.strip()]


def _supported(n: float, allowed: list[float], tol: float = 0.051) -> bool:
    return any(abs(n - a) <= tol for a in allowed)


def guard(answer: str, evidence: str, valid_ids: set[str], after_ids: set[str] = frozenset(),
          max_sentences: int = 3, post_prefix: str = "P") -> GuardResult:
    allowed = numbers_in(evidence)
    res = GuardResult(text="")
    for sentence in split_sentences(answer):
        s = CITE.sub(lambda m: m.group(0) if m.group(1) in valid_ids else "", sentence)
        s = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s{2,}", " ", s)).strip()
        if not s:
            continue
        bad = [n for n in numbers_in(s) if not _supported(n, allowed)]
        if bad:
            res.dropped.append((sentence, f"unsupported number {bad[0]:g}"))
            continue
        posts = [c for c in CITE.findall(s) if c.startswith(post_prefix)]
        if CAUSAL.search(s) and not posts:
            res.dropped.append((sentence, "causal claim without a cited post"))
            continue
        if posts and STRONG_CAUSAL.search(s) and all(p in after_ids for p in posts):
            res.dropped.append((sentence, "credits a post published after the move as the cause"))
            continue
        if len(res.kept) < max_sentences:
            res.kept.append(s)
        else:
            res.dropped.append((sentence, "over the sentence limit"))
    res.text = " ".join(res.kept) if res.kept else REFUSAL
    return res
