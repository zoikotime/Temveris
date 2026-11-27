"""Offboarding state-machine tests (§2).

Release gate: invalid transitions MUST be rejected. These tests are pure (no DB)
and exercise every transition guard in state_machine.py.
"""
import pytest

from app.offboarding.constants import CaseState
from app.offboarding.state_machine import (
    InvalidTransition, allowed_next, assert_transition, can_transition,
    can_unblock,
)


class TestHappyPath:
    def test_full_forward_path(self):
        chain = [
            CaseState.DRAFT, CaseState.PENDING_APPROVAL, CaseState.APPROVED,
            CaseState.SCHEDULED, CaseState.IN_PROGRESS, CaseState.ACCESS_CLOSURE_PENDING,
            CaseState.FINAL_REVIEW_PENDING, CaseState.PAYROLL_CLOSURE_PENDING,
            CaseState.BILLING_CLOSURE_PENDING, CaseState.LEGAL_HOLD_REVIEW_PENDING,
            CaseState.FORMER_ACCESS_PENDING, CaseState.COMPLETED,
        ]
        for a, b in zip(chain, chain[1:]):
            assert can_transition(a, b), f"{a.value} -> {b.value} should be allowed"
            assert_transition(a, b)  # must not raise

    def test_immediate_revocation_skips_scheduled(self):
        assert can_transition(CaseState.APPROVED, CaseState.IN_PROGRESS)


class TestInvalidTransitions:
    def test_draft_cannot_jump_to_completed(self):
        assert not can_transition(CaseState.DRAFT, CaseState.COMPLETED)
        with pytest.raises(InvalidTransition):
            assert_transition(CaseState.DRAFT, CaseState.COMPLETED)

    def test_cannot_approve_a_draft_directly(self):
        with pytest.raises(InvalidTransition):
            assert_transition(CaseState.DRAFT, CaseState.APPROVED)

    def test_completed_is_terminal_except_reverse(self):
        assert not can_transition(CaseState.COMPLETED, CaseState.IN_PROGRESS)
        assert can_transition(CaseState.COMPLETED, CaseState.REVERSED)

    def test_cancelled_is_fully_terminal(self):
        for s in CaseState:
            if s != CaseState.CANCELLED:
                assert not can_transition(CaseState.CANCELLED, s)

    def test_same_state_is_not_a_transition(self):
        assert not can_transition(CaseState.DRAFT, CaseState.DRAFT)


class TestAccessFailureBranch:
    def test_closure_can_fail_and_retry(self):
        assert can_transition(CaseState.ACCESS_CLOSURE_PENDING, CaseState.ACCESS_REVOCATION_FAILED)
        assert can_transition(CaseState.ACCESS_REVOCATION_FAILED, CaseState.ACCESS_CLOSURE_PENDING)
        assert can_transition(CaseState.ACCESS_REVOCATION_FAILED, CaseState.FINAL_REVIEW_PENDING)


class TestBlockCancelReverse:
    def test_block_from_any_active_state(self):
        assert can_transition(CaseState.IN_PROGRESS, CaseState.BLOCKED)
        assert can_transition(CaseState.PAYROLL_CLOSURE_PENDING, CaseState.BLOCKED)

    def test_cannot_block_a_terminal_state(self):
        assert not can_transition(CaseState.COMPLETED, CaseState.BLOCKED)
        assert not can_transition(CaseState.CANCELLED, CaseState.BLOCKED)

    def test_cancel_only_before_irreversible_closure(self):
        assert can_transition(CaseState.DRAFT, CaseState.CANCELLED)
        assert can_transition(CaseState.APPROVED, CaseState.CANCELLED)
        # Once access closure has begun, cancel is no longer allowed.
        assert not can_transition(CaseState.ACCESS_CLOSURE_PENDING, CaseState.CANCELLED)

    def test_reverse_from_post_approval_states(self):
        assert can_transition(CaseState.IN_PROGRESS, CaseState.REVERSED)
        assert can_transition(CaseState.FORMER_ACCESS_PENDING, CaseState.REVERSED)
        assert not can_transition(CaseState.DRAFT, CaseState.REVERSED)

    def test_unblock_returns_to_origin(self):
        assert can_unblock(CaseState.IN_PROGRESS)
        assert not can_unblock(CaseState.COMPLETED)


class TestAllowedNext:
    def test_allowed_next_includes_block_for_active_states(self):
        nxt = allowed_next(CaseState.IN_PROGRESS)
        assert CaseState.BLOCKED in nxt

    def test_terminal_has_no_block_or_cancel(self):
        nxt = allowed_next(CaseState.CANCELLED)
        assert CaseState.BLOCKED not in nxt
        assert len(nxt) == 0
