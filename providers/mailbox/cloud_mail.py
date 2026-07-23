"""Cloud Mail mailbox provider registration."""

from core.cloud_mail import CloudMailMailboxPool  # noqa: F401
from providers.registry import register_provider


register_provider("mailbox", "cloud_mail")(CloudMailMailboxPool)
