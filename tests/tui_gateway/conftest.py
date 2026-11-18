"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import importlib
from unittest.mock import MagicMock, patch

import hermes_bootstrap
import pytest


@pytest.fixture()
def protocol_server():
    """A ``tui_gateway.server`` bound to a fixed test home (the import-window pattern).

    The same ordering ``tests/tui_gateway/test_protocol.py::server`` uses, shared by the
    protocol tests split into their own files so each one does not copy the window.
    """
    import tui_gateway.server_requests
    import tui_gateway.transport
    import hermes_constants  # real get_process_hermes_home: a mocked one puts state.db under the cwd
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test_protocol"),
                                      get_process_hermes_home=hermes_constants.get_process_hermes_home),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    for sid in list(mod._sessions):
        mod._close_session_by_id(sid, end_reason="test_cleanup")
    from tui_gateway import server_requests
    server_requests.reset_for_tests()
    mod._live_transports.clear()
