"""Which loop a test's sessions run on.

A module opts in with ``pytestmark = pytest.mark.usefixtures("loop")``,
and each of its tests then runs on agno and on agex (when agex is
installed), the knob set for each. A test of one loop's own machinery
says so with ``@pytest.mark.agno_only("why")`` and runs on agno alone.
One agex does not do yet is marked ``@pytest.mark.agex_gap("what")``,
and fails there as expected, strictly, so closing the gap shows.
"""

import importlib.util

import pytest

LOOPS = ["agno"] + (["agex"] if importlib.util.find_spec("agex") else [])


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "agno_only(reason): a test of agno's own machinery"
    )
    config.addinivalue_line(
        "markers", "agex_gap(reason): behavior agex's loop does not have yet"
    )


def pytest_generate_tests(metafunc):
    if "loop" in metafunc.fixturenames:
        only = metafunc.definition.get_closest_marker("agno_only")
        metafunc.parametrize("loop", ["agno"] if only else LOOPS, indirect=True)


@pytest.fixture
def loop(request, monkeypatch):
    gap = request.node.get_closest_marker("agex_gap")
    if gap is not None and request.param == "agex":
        request.node.add_marker(pytest.mark.xfail(reason=gap.args[0], strict=True))
    monkeypatch.setenv("NONTAINER_STUDIO_LOOP", request.param)
    return request.param
