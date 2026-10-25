import pytest

from common import teams


def test_aliases_map_to_nflverse():
    assert teams.canon("JAC") == "JAX"
    assert teams.canon("LAR") == "LA"
    assert teams.canon("gb") == "GB"
    with pytest.raises(KeyError):
        teams.canon("XYZ")


@pytest.mark.parametrize("pair,expected", [
    ("CHISEA", ("CHI", "SEA")), ("LARLV", ("LA", "LV")), ("NENYG", ("NE", "NYG")),
    ("LACKC", ("LAC", "KC")), ("GBJAC", ("GB", "JAX")), ("ZZZQQ", None),
])
def test_split_pair(pair, expected):
    assert teams.split_pair(pair) == expected


def test_full_names():
    assert teams.from_full_name("Las Vegas Raiders") == "LV"
    assert teams.from_full_name("los angeles rams") == "LA"
