import pytest
import scenarios


@pytest.mark.parametrize("sc", scenarios.SCENARIOS, ids=lambda s: s["name"][:3])
def test_scenario(sc):
    out = scenarios.run(sc)
    assert scenarios.check(sc, out) == [], out["report"]
