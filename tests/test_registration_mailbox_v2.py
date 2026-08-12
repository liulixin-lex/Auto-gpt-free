from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.db import AccountModel, ResourceLeaseModel, engine
from domain.registration_runtime import stable_resource_ref
from infrastructure.registration_repository import ResourceLeaseConflict, resource_leases
from services.registration_mailbox import ResourceLeasedMailbox


class _Mailbox(BaseMailbox):
    def __init__(self, addresses):
        self.addresses = iter(addresses)
        self.calls = 0

    def get_email(self):
        self.calls += 1
        return MailboxAccount(email=next(self.addresses))

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None, code_pattern=None):
        return "123456"

    def get_current_ids(self, account):
        return {"message-before"}


def _leased(delegate, owner):
    return ResourceLeasedMailbox(
        delegate,
        owner_attempt_id=owner,
        provider="fixture",
        ttl_seconds=60,
    )


def test_mailbox_wrapper_retries_when_provider_returns_leased_address():
    first = _leased(_Mailbox(["same@example.com"]), "attempt-one").get_email()
    second = _leased(
        _Mailbox(["same@example.com", "other@example.com"]),
        "attempt-two",
    ).get_email()

    assert first.email == "same@example.com"
    assert second.email == "other@example.com"
    assert second.extra["resource_lease_id"]


def test_mailbox_wrapper_skips_reused_address_and_allocates_a_fresh_one():
    consumed = _leased(_Mailbox(["consumed@example.com"]), "attempt-consumed")
    consumed.get_email()
    assert consumed.mark_current("consumed") is True

    provider = _Mailbox(["consumed@example.com", "fresh@example.com"])
    account = _leased(provider, "attempt-fresh").get_email()

    assert provider.calls == 2
    assert account.email == "fresh@example.com"
    assert account.extra["resource_lease_id"]


def test_mailbox_lease_is_idempotent_for_same_attempt():
    mailbox = _leased(_Mailbox(["same@example.com", "same@example.com"]), "attempt-one")

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert first.extra["resource_lease_id"] == second.extra["resource_lease_id"]


def test_mailbox_wrapper_reports_exhausted_allocation():
    resource_leases.acquire(
        resource_type="mailbox",
        resource_id=stable_resource_ref("busy@example.com"),
        owner_attempt_id="another-attempt",
        ttl_seconds=60,
    )
    mailbox = ResourceLeasedMailbox(
        _Mailbox(["busy@example.com"]),
        owner_attempt_id="current-attempt",
        provider="fixture",
        ttl_seconds=60,
        allocation_attempts=1,
    )

    with pytest.raises(ResourceLeaseConflict):
        mailbox.get_email()


def test_mailbox_lifecycle_moves_reserved_to_side_effect_and_consumed():
    mailbox = _leased(_Mailbox(["lifecycle@example.com"]), "attempt-lifecycle")
    account = mailbox.get_email()

    with Session(engine) as session:
        lease = session.exec(
            select(ResourceLeaseModel).where(
                ResourceLeaseModel.resource_id == stable_resource_ref(account.email)
            )
        ).one()
        assert lease.status == "reserved"
        assert lease.active_key

    assert mailbox.mark_current("side_effect_started") is True
    with Session(engine) as session:
        lease = session.exec(
            select(ResourceLeaseModel).where(
                ResourceLeaseModel.resource_id == stable_resource_ref(account.email)
            )
        ).one()
        assert lease.status == "side_effect_started"
        assert lease.active_key

    assert mailbox.mark_current("consumed") is True
    reused_provider = _Mailbox([account.email] * 5)
    with pytest.raises(ResourceLeaseConflict, match="MAILBOX_REUSED"):
        _leased(reused_provider, "attempt-reuse").get_email()
    assert reused_provider.calls == 5


def test_existing_account_email_is_never_reallocated_without_legacy_lease():
    with Session(engine) as session:
        session.add(
            AccountModel(
                platform="chatgpt",
                email="legacy@example.com",
                password="fixture",
            )
        )
        session.commit()

    reused_provider = _Mailbox(["legacy@example.com"] * 5)
    with pytest.raises(ResourceLeaseConflict, match="MAILBOX_REUSED"):
        _leased(reused_provider, "attempt-new").get_email()
    assert reused_provider.calls == 5
