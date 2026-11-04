"""Short failure-injection soak (mock Claude + mock Zoom): no safety invariant may break."""
import pytest

from subtalk.soak import run_soak


@pytest.mark.parametrize("seed", [7, 11])
async def test_soak_with_doubled_fault_rates_keeps_every_invariant(tmp_path, seed):
    rep = await run_soak(seconds=20, seed=seed, tmp=tmp_path, fault_scale=2.0)
    assert rep.violations == []
    assert rep.segments >= 8 and rep.zoom_sent >= 1
    assert sum(v for k, v in rep.faults.items() if k.startswith(("zoom", "claude_"))) >= 3  # faults really happened
    assert rep.queue_max <= 8
