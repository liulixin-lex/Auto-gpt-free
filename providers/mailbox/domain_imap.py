"""Domain IMAP mailbox provider registration."""

from core.domain_imap_mailbox import DomainImapMailboxPool  # noqa: F401
from providers.registry import register_provider


register_provider("mailbox", "domain_imap")(DomainImapMailboxPool)
