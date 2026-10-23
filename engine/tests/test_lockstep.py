"""Tests of the lockstep clock agent's pure part (task P2-T04)."""

from __future__ import annotations

import json

from fieldproof.lockstep import Agent


def test_agent_acknowledges_each_step_boundary_once() -> None:
    published: list[str] = []
    agent = Agent("ugv1", 0.1, published.append)
    assert agent.on_clock(0.0) is not None  # step 0
    assert agent.on_clock(0.05) is None
    assert agent.on_clock(0.0999) is None  # not yet on the next boundary
    ack = agent.on_clock(0.1)
    assert ack is not None
    assert json.loads(ack) == {"vehicle": "ugv1", "sim": 0.1, "step": 1}
    assert agent.on_clock(0.1) is None
    assert agent.on_clock(0.3) is not None  # a skipped boundary still counts once
    assert agent.last_step == 3
    assert agent.acks == 3
    assert [json.loads(p)["step"] for p in published] == [0, 1, 3]


def test_agent_acknowledges_the_grid_despite_floating_point() -> None:
    agent = Agent("ugv1", 0.1, lambda _text: None)
    for k in range(1, 50):
        sim = k * 0.1  # 0.30000000000000004 and the like
        ack = agent.on_clock(sim)
        assert ack is not None, sim
        assert json.loads(ack)["step"] == k


def test_agent_repeats_its_last_acknowledgement() -> None:
    published: list[str] = []
    agent = Agent("ugv1", 0.5, published.append)
    agent.repeat()
    assert published == []
    agent.on_clock(1.0)
    agent.repeat()
    assert published == [agent.last_ack, agent.last_ack]
    assert json.loads(published[-1]) == {"vehicle": "ugv1", "sim": 1.0, "step": 2}
