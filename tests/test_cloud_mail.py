from __future__ import annotations

import json

from core.cloud_mail import CloudMailMailboxPool


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self.payload, str):
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, email_payloads=None):
        self.email_payloads = list(email_payloads or [[]])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/api/setting/websiteConfig"):
            return FakeResponse({"code": 200, "data": {"domainList": ["@example.com"]}})
        if url.endswith("/api/public/addUser"):
            return FakeResponse({"code": 200, "data": None})
        if url.endswith("/api/public/emailList"):
            payload = self.email_payloads.pop(0) if len(self.email_payloads) > 1 else self.email_payloads[0]
            return FakeResponse({"code": 200, "data": payload})
        raise AssertionError(f"unexpected request: {method} {url}")


def test_cloud_mail_creates_mailbox_with_raw_public_token():
    session = FakeSession()
    mailbox = CloudMailMailboxPool(
        api_base="https://mail.example.com/api/",
        api_key="token-123",
        domain="@example.com",
        session=session,
    )

    account = mailbox.get_email()

    assert account.email.endswith("@example.com")
    assert account.extra["provider_account"]["provider_name"] == "cloud_mail"
    assert account.extra["provider_account"]["credentials"]["email"] == account.email
    assert "password" in account.extra["provider_account"]["credentials"]
    add_call = next(call for call in session.calls if call[1].endswith("/api/public/addUser"))
    assert add_call[2]["headers"]["Authorization"] == "token-123"
    assert add_call[2]["json"]["list"][0]["email"] == account.email


def test_cloud_mail_connection_test_is_read_only():
    session = FakeSession()
    mailbox = CloudMailMailboxPool(api_base="https://mail.example.com", api_key="token-123", session=session)

    result = mailbox.test_connection()

    assert result["ok"] is True
    assert result["email"] == "随机地址@example.com"
    assert not any(call[1].endswith("/api/public/addUser") for call in session.calls)
    probe_call = next(call for call in session.calls if call[1].endswith("/api/public/emailList"))
    assert probe_call[2]["headers"]["Authorization"] == "token-123"
    assert len(probe_call[2]["json"]["toEmail"]) < 50
    assert "_" not in probe_call[2]["json"]["toEmail"]


def test_cloud_mail_waits_for_new_message_and_extracts_html_subject_code():
    session = FakeSession(
        email_payloads=[
            [{"emailId": 1, "subject": "old", "text": "verification code: 111111"}],
            [{"emailId": 1, "subject": "old", "text": "verification code: 111111"}],
            [{
                "emailId": 2,
                "subject": "SpaceXAI confirmation code: WKT-B4B",
                "content": "<p>Use this code to continue.</p>",
            }],
        ]
    )
    mailbox = CloudMailMailboxPool(
        api_base="https://mail.example.com",
        api_key="token-123",
        poll_interval=0,
        session=session,
    )
    account = type("Account", (), {"email": "user@example.com"})()
    before_ids = mailbox.get_current_ids(account)

    assert mailbox.wait_for_code(account, timeout=1, before_ids=before_ids) == "WKT-B4B"


def test_cloud_mail_can_get_token_from_admin_credentials():
    session = FakeSession()
    original_request = session.request

    def request(method, url, **kwargs):
        if url.endswith("/api/public/genToken"):
            session.calls.append((method, url, kwargs))
            return FakeResponse({"code": 200, "data": {"token": "generated-token"}})
        return original_request(method, url, **kwargs)

    session.request = request
    mailbox = CloudMailMailboxPool(
        api_base="https://mail.example.com",
        admin_email="admin@example.com",
        admin_password="admin-password",
        session=session,
    )

    assert mailbox._token() == "generated-token"
    assert mailbox._token() == "generated-token"
    assert len([call for call in session.calls if call[1].endswith("/api/public/genToken")]) == 1


def test_cloud_mail_shortens_long_email_query_and_filters_exact_address():
    long_domain = "very-long-cloud-mail-domain.example.invalid"
    email = f"faiabcdefghijkl@{long_domain}"
    session = FakeSession(email_payloads=[[{"emailId": 7, "toEmail": email, "subject": "code: 123456"}]])
    mailbox = CloudMailMailboxPool(
        api_base="https://mail.example.com",
        api_key="token-123",
        session=session,
    )

    messages = mailbox._email_list(email)

    assert messages[0]["toEmail"] == email
    call = next(call for call in session.calls if call[1].endswith("/api/public/emailList"))
    assert call[2]["json"]["toEmail"] == "faiabcdefghijkl@%"
    assert len(call[2]["json"]["toEmail"]) < 50
