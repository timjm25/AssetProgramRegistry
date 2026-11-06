import itertools

import pytest

from asset_registry import Registry
from asset_registry.seed import seed


@pytest.fixture
def clock():
    counter = itertools.count(1)
    return lambda: f"2026-11-06T12:{next(counter) % 60:02d}:00Z"


@pytest.fixture
def reg(clock):
    r = Registry(clock=clock)
    yield r
    r.close()


@pytest.fixture
def seeded(reg):
    return reg, seed(reg)
