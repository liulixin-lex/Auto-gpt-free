from infrastructure.provider_definitions_repository import (
    ProviderDefinitionsRepository,
    SUPPORTED_MAILBOX_PROVIDER_KEYS,
)


def test_mailbox_catalog_only_exposes_supported_providers():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()

    definitions = repository.list_by_type("mailbox", enabled_only=True)

    assert {item.provider_key for item in definitions} == set(SUPPORTED_MAILBOX_PROVIDER_KEYS)
    # Public free temp-mail providers must not reappear.
    assert "mail_tm" not in SUPPORTED_MAILBOX_PROVIDER_KEYS
    assert "tempmail_lol" not in SUPPORTED_MAILBOX_PROVIDER_KEYS
    assert "guerrilla_mail" not in SUPPORTED_MAILBOX_PROVIDER_KEYS


def test_mailbox_driver_catalog_only_exposes_supported_providers():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()

    drivers = repository.list_driver_templates("mailbox")

    assert {item["driver_type"] for item in drivers} == {
        "local_ms_pool",
        "api_mailbox",
        "cloud_mail",
        "domain_imap",
    }
