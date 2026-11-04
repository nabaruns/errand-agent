from __future__ import annotations

import pytest

from errand_agent.harness import SESSION, START, THIS_WEEK, Harness, full_request, propose, search

__all__ = ["SESSION", "START", "THIS_WEEK", "Harness", "full_request", "propose", "search"]


@pytest.fixture
def harness(tmp_path):
    made: list[Harness] = []

    def make(**kwargs) -> Harness:
        h = Harness(tmp_path, **kwargs)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()
