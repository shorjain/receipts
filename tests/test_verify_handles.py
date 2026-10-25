import pytest

from scripts import verify_handles as vh

YAML = """insiders:
- handle: DanDuggan
  name: Dan Duggan
  tier: beat
  teams:
  - NYG
  verified: false
  active: true
- handle: DDugganFan
  name: Someone Else
  verified: false
"""


@pytest.mark.parametrize("display,name,ok", [
    ("Brooke Pryor", "Brooke Pryor", True),
    ("TURRON DAVENPORT", "Turron Davenport", True),
    ("MikeKlis9NEWS", "Mike Klis", True),
    ("DJ Bien-Aime | NASM CPT", "DJ Bien-Aime", True),
    ("David Teicher", "Adam Teicher", False),
    ("Adam", "Adam Teicher", False),
    ("Alindz99", "Alaina Getzenberg", False),
    (None, "Dan Duggan", False),
])
def test_names_match(display, name, ok):
    assert vh.names_match(display, name) is ok


def test_mentions_team():
    assert vh.mentions_team(["Giants WR out Sunday"], ["NYG"])
    assert vh.mentions_team(["New York is cold"], ["NYG"])
    assert not vh.mentions_team(["Great day at the beach", None], ["CAR"])


def test_mark_and_replace_touch_only_one_entry():
    t = vh.replace_handle(YAML, "DanDuggan", "DDuggan21")
    assert "# handle corrected from DanDuggan\n- handle: DDuggan21\n" in t
    assert t.count("verified: true") == 1 and "- handle: DDugganFan" in t
    assert vh.mark_verified(t, "DDugganFan").count("verified: true") == 2
