import pytest

from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_TRANSITIONS,
    TERMINAL_STATUSES,
    DeploymentStatus,
    is_terminal,
    require_transition,
)


def test_every_deployment_status_is_in_a_transition_row_or_terminal():
    """Nothing the enum can name is unreachable or dead weight."""
    for status in DeploymentStatus:
        assert is_terminal(status) or status in DEPLOYMENT_TRANSITIONS
        if is_terminal(status):
            assert status not in DEPLOYMENT_TRANSITIONS, (
                f"terminal status {status.value} must not list further transitions"
            )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentStatus.QUEUED, DeploymentStatus.RUNNING),
        (DeploymentStatus.QUEUED, DeploymentStatus.FAILED),
        (DeploymentStatus.RUNNING, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.RUNNING, DeploymentStatus.FAILED),
        (DeploymentStatus.RUNNING, DeploymentStatus.TIMED_OUT),
        (DeploymentStatus.RUNNING, DeploymentStatus.ABANDONED),
    ],
)
def test_the_lifecycle_allows_these_transitions(current, target):
    require_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeploymentStatus.QUEUED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.QUEUED, DeploymentStatus.TIMED_OUT),
        (DeploymentStatus.QUEUED, DeploymentStatus.ABANDONED),
        (DeploymentStatus.RUNNING, DeploymentStatus.QUEUED),
        (DeploymentStatus.SUCCEEDED, DeploymentStatus.RUNNING),
        (DeploymentStatus.SUCCEEDED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.FAILED, DeploymentStatus.SUCCEEDED),
        (DeploymentStatus.TIMED_OUT, DeploymentStatus.ABANDONED),
        (DeploymentStatus.ABANDONED, DeploymentStatus.RUNNING),
    ],
)
def test_the_lifecycle_refuses_these_transitions(current, target):
    with pytest.raises(ValueError, match="illegal deployment transition"):
        require_transition(current, target)


def test_terminal_statuses_are_closed():
    for status in TERMINAL_STATUSES:
        assert is_terminal(status)
        with pytest.raises(ValueError):
            require_transition(status, DeploymentStatus.RUNNING)
