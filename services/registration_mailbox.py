"""Mailbox wrapper that atomically leases the resolved address per attempt."""
from __future__ import annotations

from core.base_mailbox import BaseMailbox, MailboxAccount
from domain.registration_runtime import stable_resource_ref
from infrastructure.registration_repository import (
    ResourceLeaseConflict,
    resource_leases,
)


class ResourceLeasedMailbox(BaseMailbox):
    """Prevent duplicate mailbox allocation across threads and processes.

    Providers still own address creation and message polling. This wrapper adds
    the cross-process claim after an address is returned, then delegates every
    mailbox operation unchanged.
    """

    def __init__(
        self,
        delegate: BaseMailbox,
        *,
        owner_attempt_id: str,
        provider: str,
        ttl_seconds: int,
        allocation_attempts: int = 5,
    ) -> None:
        self.delegate = delegate
        self.owner_attempt_id = str(owner_attempt_id or "")
        self.provider = str(provider or "")
        self.ttl_seconds = max(int(ttl_seconds), 1)
        self.allocation_attempts = max(int(allocation_attempts), 1)
        self.last_account: MailboxAccount | None = None

    def get_email(self) -> MailboxAccount:
        last_conflict: ResourceLeaseConflict | None = None
        for _ in range(self.allocation_attempts):
            account = self.delegate.get_email()
            email = str(getattr(account, "email", "") or "").strip()
            if not email:
                return account
            try:
                lease = resource_leases.acquire(
                    resource_type="mailbox",
                    resource_id=stable_resource_ref(email.lower()),
                    owner_attempt_id=self.owner_attempt_id,
                    ttl_seconds=self.ttl_seconds,
                    metadata={"provider": self.provider},
                )
            except ResourceLeaseConflict as exc:
                last_conflict = exc
                continue
            account.extra = dict(account.extra or {})
            account.extra["resource_lease_id"] = lease.id
            account.extra["resource_id"] = stable_resource_ref(email.lower())
            self.last_account = account
            return account
        detail = str(last_conflict or "mailbox provider did not return an allocatable address")
        raise ResourceLeaseConflict(
            "mailbox allocation exhausted after "
            f"{self.allocation_attempts} attempts; last_error={detail}"
        ) from last_conflict

    def mark_current(self, status: str, *, cooldown_seconds: int = 0) -> bool:
        account = self.last_account
        email = str(getattr(account, "email", "") or "").strip().lower() if account else ""
        if not email:
            return False
        return resource_leases.mark_resource(
            resource_type="mailbox",
            resource_id=stable_resource_ref(email),
            status=status,
            cooldown_seconds=cooldown_seconds,
            metadata={"provider": self.provider},
        )

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        return self.delegate.wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        return self.delegate.get_current_ids(account)

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        return self.delegate.wait_for_link(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
        )


def lease_mailbox_for_attempt(
    mailbox: BaseMailbox | None,
    *,
    owner_attempt_id: str,
    provider: str,
    ttl_seconds: int,
) -> BaseMailbox | None:
    if mailbox is None or not owner_attempt_id:
        return mailbox
    if isinstance(mailbox, ResourceLeasedMailbox):
        return mailbox
    return ResourceLeasedMailbox(
        mailbox,
        owner_attempt_id=owner_attempt_id,
        provider=provider,
        ttl_seconds=ttl_seconds,
    )
