from rag.guard import REFUSAL, guard, numbers_in, split_sentences

EVIDENCE = """[M1] Los Angeles Rams at Las Vegas Raiders: Las Vegas Raiders win probability 40.0% -> 45.0% (+5.0 pp), 2026-10-25 14:05-14:30 UTC, multi-venue (kalshi,polymarket).
[P1] @AdamSchefter, posted 12 minutes before the move started (13:53 UTC): "Rams QB Matthew Stafford is inactive today."
[P2] @VinnyBonsignore, posted 5 minutes AFTER the move started (14:10 UTC): "Raiders line moving fast."""
VALID, AFTER = {"M1", "P1", "P2"}, {"P2"}


def test_grounded_answer_passes_untouched():
    ans = ("The Raiders' win probability rose from 40.0% to 45.0% [M1] after Adam Schefter reported "
           "Matthew Stafford is inactive [P1]. Bonsignore then reported the move [P2].")
    r = guard(ans, EVIDENCE, VALID, AFTER)
    assert not r.refused and r.text == ans and r.dropped == []


def test_unsupported_number_drops_sentence():
    r = guard("The line moved 7 points [M1]. Schefter reported Stafford inactive [P1].", EVIDENCE, VALID, AFTER)
    assert r.kept == ["Schefter reported Stafford inactive [P1]."]
    assert r.dropped[0][1] == "unsupported number 7"


def test_unknown_citations_are_stripped():
    r = guard("Schefter reported Stafford inactive [P1][P9].", EVIDENCE, VALID, AFTER)
    assert r.text == "Schefter reported Stafford inactive [P1]."


def test_causal_claim_needs_a_post():
    r = guard("The move happened because of sharp money. Probability went to 45.0% [M1].", EVIDENCE, VALID, AFTER)
    assert r.kept == ["Probability went to 45.0% [M1]."]
    assert r.dropped[0][1] == "causal claim without a cited post"


def test_after_post_cannot_be_the_cause():
    r = guard("The move was sparked by Bonsignore's post [P2].", EVIDENCE, VALID, AFTER)
    assert r.refused and r.text == REFUSAL


def test_at_most_three_sentences():
    ans = " ".join(["Schefter reported Stafford inactive [P1]."] * 5)
    r = guard(ans, EVIDENCE, VALID, AFTER)
    assert len(r.kept) == 3 and len(r.dropped) == 2


def test_helpers():
    assert numbers_in("rose 40.0% -> 45.0% (+5.0 pp) [P1] 49ers $1,250.50") == ["40", "45", "5", "49", "1250.5"]
    assert split_sentences("Line moved from 3.5 to 4.5 [M1]. Then [P1] said so.") == [
        "Line moved from 3.5 to 4.5 [M1].", "Then [P1] said so."]


def test_require_citation_drops_uncited_sentences():
    r = guard("Schefter reported Stafford inactive [P1]. Everyone knew already.", EVIDENCE, VALID, AFTER,
              require_citation=True)
    assert r.kept == ["Schefter reported Stafford inactive [P1]."] and r.dropped[0][1] == "no citation"


def test_numbers_match_exactly_after_normalizing():
    # 45% == 45.0%, but 45.04 is not 45.0 and 0.45 is not 45% (no converting or rounding)
    assert guard("Up to 45% [M1].", EVIDENCE, VALID, AFTER).kept
    assert not guard("Up to 45.04% [M1].", EVIDENCE, VALID, AFTER).kept
    assert not guard("Up to 0.45 [M1].", EVIDENCE, VALID, AFTER).kept


def test_numbers_from_the_question_may_be_echoed():
    r = guard("Over the last 6 hours, Schefter reported it [P1].", EVIDENCE, VALID, AFTER,
              extra_context="What happened in the last 6 hours?")
    assert r.kept and not guard("Over the last 6 hours, Schefter reported it [P1].", EVIDENCE, VALID, AFTER).kept


def test_bare_refs_are_bracketed_and_warnings_reported():
    r = guard("Schefter reported Stafford inactive P1. Line moved 9 points [M1].", EVIDENCE, VALID, AFTER)
    assert r.kept == ["Schefter reported Stafford inactive [P1]."]
    assert r.warnings == ["removed a sentence with numbers not in the evidence: 9"]
