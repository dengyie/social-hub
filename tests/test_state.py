"""状态机迁移合法性 + 错误分类映射。"""

from __future__ import annotations

import pytest

from social_hub.adapters.base import (
    CaptchaWaitError,
    CredentialsError,
    NeedsLoginError,
    PermanentError,
    TransientError,
)
from social_hub.core.orchestrator import _error_state
from social_hub.core.state import ALLOWED, TERMINAL, can_transition


def test_terminal_has_no_outgoing():
    for t in TERMINAL:
        assert ALLOWED[t] == set() or t == "failed" and ALLOWED["failed"] == {"queued"}


def test_happy_path_allowed():
    for src, dst in [("queued", "running"), ("running", "verifying"), ("verifying", "done")]:
        assert can_transition(src, dst)


def test_needs_login_is_recoverable():
    assert can_transition("needs_login", "queued")
    assert can_transition("running", "needs_login")


def test_illegal_transition_rejected():
    assert not can_transition("done", "running")
    assert not can_transition("queued", "done")


def test_error_state_mapping():
    assert _error_state(TransientError("x")) == "queued"
    assert _error_state(CredentialsError("x")) == "needs_login"
    assert _error_state(NeedsLoginError("x")) == "needs_login"
    assert _error_state(CaptchaWaitError("x")) == "captcha_wait"
    assert _error_state(PermanentError("x")) == "failed"
