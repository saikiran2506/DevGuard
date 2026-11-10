import pytest

import app as appmodule


def _reset():
    with appmodule.state_lock:
        appmodule.rooms.clear()
        appmodule.sessions.clear()


@pytest.fixture(autouse=True)
def reset_shared_state():
    _reset()
    yield
    _reset()
