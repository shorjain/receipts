"""Post-generation grounding guard, shared by move explanations and the Ask chatbot.

Lined up with shipyard-rs (idt/rag/parse.py: enforce): numbers must appear in the
evidence (or the question) exactly, after normalizing $, commas, % and trailing zeros;
bare references like "P2" become "[P2]"; unknown citations are stripped; every removal
is reported as a warning the UI can show. The causal rules (3 and 4) go further than
shipyard-rs because the brief requires every causal claim to cite a post.

Given model output and the numbered evidence block it was written from:
  0. bare evidence IDs ("P2") are bracketed ("[P2]");
  1. citations like [P7] that aren't in the evidence are stripped;
  2. a sentence containing a number that doesn't appear in the evidence (or in
     `extra_context`, e.g. the user's question) is dropped;
  3. a causal sentence ("because", "after", "sparked", ...) with no post citation is dropped;
  4. a causal sentence whose only cited posts came after the move is dropped (those
     posts report on the move; they can't have caused it);
  (optional) require_citation: any sentence without a valid citation is dropped (Ask);
  5. at most max_sentences survive. If none do, the caller refuses instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE = re.compile(r"\[([A-Z]\d+)\]")
BARE_REF = re.compile(r"(?<!\[)\b([A-Z]\d+)\b(?!\])")
NUM = re.compile(r"(?<![A-Za-z\d.])\$?\d[\d,]*(?:\.\d+)?%?")
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
    warnings: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return not self.kept


def _norm(n: str) -> str:
    v = n.replace("$", "").replace(",", "").rstrip("%")
    return v.rstrip("0").rstrip(".") if "." in v else v


def numbers_in(text: str) -> list[str]:
    """Normalized numbers in text, citation markers excluded ("45.0%" -> "45")."""
    return [_norm(n) for n in NUM.findall(CITE.sub(" ", text))]


def bracket_refs(text: str, valid_ids: set[str]) -> str:
    """Models sometimes write 'P2' instead of '[P2]'; bracket bare references to known IDs."""
    return BARE_REF.sub(lambda m: f"[{m.group(1)}]" if m.group(1) in valid_ids else m.group(0), text)


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s.strip() for s in SENTENCE_END.split(text) if s.strip()]


def guard(answer: str, evidence: str, valid_ids: set[str], after_ids: set[str] = frozenset(),
          max_sentences: int = 3, post_prefix: str | tuple[str, ...] = "P",
          require_citation: bool = False, extra_context: str = "") -> GuardResult:
    allowed = set(numbers_in(evidence + "\n" + extra_context))
    res = GuardResult(text="")
    for sentence in split_sentences(bracket_refs(answer, valid_ids)):
        unknown = [c for c in CITE.findall(sentence) if c not in valid_ids]
        if unknown:
            res.warnings.append(f"removed unknown citations: {', '.join(unknown)}")
        s = CITE.sub(lambda m: m.group(0) if m.group(1) in valid_ids else "", sentence)
        s = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s{2,}", " ", s)).strip()
        if not s:
            continue
        bad = [n for n in numbers_in(s) if n not in allowed]
        if bad:
            res.dropped.append((sentence, f"unsupported number {bad[0]}"))
            res.warnings.append(f"removed a sentence with numbers not in the evidence: {', '.join(bad)}")
            continue
        if require_citation and not CITE.search(s):
            res.dropped.append((sentence, "no citation"))
            res.warnings.append("removed a sentence that cited no evidence")
            continue
        posts = [c for c in CITE.findall(s) if c.startswith(post_prefix)]
        if CAUSAL.search(s) and not posts:
            res.dropped.append((sentence, "causal claim without a cited post"))
            res.warnings.append("removed a claim about a cause that cited no post")
            continue
        if posts and STRONG_CAUSAL.search(s) and all(p in after_ids for p in posts):
            res.dropped.append((sentence, "credits a post published after the move as the cause"))
            res.warnings.append("removed a sentence crediting a post published after the move as the cause")
            continue
        if len(res.kept) < max_sentences:
            res.kept.append(s)
        else:
            res.dropped.append((sentence, "over the sentence limit"))
    res.text = " ".join(res.kept) if res.kept else REFUSAL
    return res
