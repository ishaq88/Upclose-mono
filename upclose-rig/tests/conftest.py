import logging

import pytest

from upclose_rig.simulator import SimConfig, simulate


@pytest.fixture(scope="session")
def idle_records():
    return simulate(SimConfig(seconds=60, rpm=780, seed=1))


@pytest.fixture(autouse=True, scope="session")
def _quiet():
    logging.getLogger("pint").setLevel(logging.ERROR)
