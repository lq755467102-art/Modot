"""Set up an isolated executor and provider for request budget tests."""
from unittest.mock import patch

from tools.test_context_checkpoints import CheckpointTests


def request_fixture(test):
    fixture = CheckpointTests()
    fixture.setUp()
    test.addCleanup(fixture.doCleanups)
    fixture.session.executor_core = object()
    fixture.session.task_input = dict(id="task")
    scope = patch("loop.projects.scope", return_value=dict(resources=[]))
    scope.start()
    test.addCleanup(scope.stop)
    del fixture.provider.count_input_tokens
    fixture.provider.max_tokens = 256
    return fixture.session, fixture.provider, fixture.events
