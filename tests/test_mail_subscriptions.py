import base64
import re
import ssl
from datetime import datetime, timezone

import app.subscriptions as subscription_module
from email import policy
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from imapclient.exceptions import LoginError
from imapclient.response_parser import parse_fetch_response
from imapclient.response_types import BodyData

from app.main import create_app
from app.mail_subscription import body_parts, mail_matches, validate_mail_config
from test_subscriptions import HEADERS, app, channel, check, subscription, subscription_clock


def text_part(kind=b"PLAIN", charset=b"UTF-8", encoding=b"8BIT", disposition=None, params=None):
    return (b"TEXT", kind, params or (b"CHARSET", charset), None, None, encoding, 30, 1, None, disposition)


def mail(subject="服务通知", sender="通知服务 <alerts@example.com>", text="这是正文", structure=None, parts=None, received=None):
    header = EmailMessage(policy=policy.SMTP)
    header["Subject"] = subject
    header["From"] = sender
    header["Date"] = "Thu, 15 Oct 2026 09:00:00 +0800"
    return {"received": received or datetime.fromtimestamp(subscription_module.time.time(), timezone.utc), "headers": header.as_bytes(), "structure": structure or BodyData.create(text_part()),
            "parts": parts or {"1": text.encode()}}


class Mailbox:
    def __init__(self):
        self.messages = {1: mail("历史服务通知")}
        self.validity = 50
        self.uidnext = 2
        self.calls = []
        self.login_error = False
        self.body_error = False
        self.omit_header = False
        self.omit_body = False

    def __call__(self, host, **kwargs):
        self.calls.append(("connect", host, kwargs))
        assert kwargs["ssl"] and kwargs["port"] == 993
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl_context"].check_hostname
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(("logout",))

    def login(self, username, password):
        if self.login_error:
            raise LoginError(f"provider echoed {password}")

    def has_capability(self, capability):
        return True

    def id_(self, fields):
        self.calls.append(("id", fields))

    def select_folder(self, folder, readonly):
        assert folder == "INBOX" and readonly is True
        return {b"UIDVALIDITY": self.validity, b"UIDNEXT": self.uidnext}

    def search(self, criteria):
        self.calls.append(("search", criteria))
        if criteria[0] != "UID":
            return sorted(self.messages)
        bounds = criteria[1].split(":")
        start, end = int(bounds[0]), int(bounds[-1])
        return sorted(uid for uid in self.messages if start <= uid <= end)

    def fetch(self, uids, selectors):
        self.calls.append(("fetch", list(uids), selectors))
        if selectors == ["BODY.PEEK[HEADER]", "BODYSTRUCTURE", "INTERNALDATE"]:
            return {} if self.omit_header else {
                uid: {b"BODY[HEADER]": self.messages[uid]["headers"], b"BODYSTRUCTURE": self.messages[uid]["structure"], b"INTERNALDATE": self.messages[uid]["received"]}
                for uid in uids if uid in self.messages}
        if self.body_error:
            raise OSError("network error private-mail-password")
        if self.omit_body:
            return {}
        section = re.fullmatch(r"BODY\.PEEK\[([\d.]+)\]<0\.\d+>", selectors[0])[1]
        uid = uids[0]
        raw = self.messages[uid]["parts"][section]
        return {uid: {f"BODY[{section}]<0>".encode(): raw}}

    def add(self, uid, **kwargs):
        kwargs.setdefault("received", datetime.fromtimestamp(subscription_module.time.time() + 1, timezone.utc))
        self.messages[uid] = mail(**kwargs)
        self.uidnext = max(self.uidnext, uid + 1)


@pytest.fixture
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr("app.mail_subscription.IMAPClient", box)
    return box


def config(**kwargs):
    return {"provider": "qq", "username": "demo@qq.com", "password": "private-mail-password",
            "subject_keywords": ["通知"], "senders": [], "match_mode": "any", **kwargs}


def test_baseline_header_filters_forward_and_restart(app, mailbox):
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client), channel(client, "dingtalk")], "imap", config())
        assert check(client, item)["baseline"]
        assert not any(call[0] == "fetch" for call in mailbox.calls)
        mailbox.add(2, subject="新服务通知")
        mailbox.add(3, subject="忽略这封邮件")
        assert check(client, item)["queued"] == 1
        assert check(client, item)["queued"] == 0
        message = app.state.db.list_messages()["items"][0]
        assert message["title"] == "新服务通知"
        assert "通知服务 <alerts@example.com>" in message["body"]
        assert "这是正文" in message["body"]
        assert len(message["deliveries"]) == 2
        assert app.state.db.get_subscription(item["id"])["last_success_at"] == subscription_module.time.time()
        body_calls = [call for call in mailbox.calls if call[0] == "fetch" and call[2] != ["BODY.PEEK[HEADER]", "BODYSTRUCTURE", "INTERNALDATE"]]
        assert all(call[1] == [2] for call in body_calls)  # Nonmatches never fetch bodies.
    restarted = create_app(app.state.settings, start_worker=False)
    with TestClient(restarted) as client:
        assert check(client, item)["queued"] == 0


@pytest.mark.parametrize("provider,host", [("qq", "imap.qq.com"), ("gmail", "imap.gmail.com")])
def test_provider_and_password_redaction_and_rule_edit(app, mailbox, provider, host):
    with TestClient(app) as client:
        item, payload = subscription(client, [channel(client)], "imap", config(provider=provider))
        assert item["config"]["password"] == "" and "password" in item["configured_secrets"]
        assert "private-mail-password" not in str(item)
        assert b"private-mail-password" not in open(app.state.db.path, "rb").read()
        check(client, item)
        assert mailbox.calls[0][1] == host
        updated = {**payload, "config": config(provider=provider, password="", subject_keywords=[], senders=["alerts@example.com"])}
        result = client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS, json=updated)
        assert result.status_code == 200 and result.json()["initialized"]
        assert app.state.db.get_subscription(item["id"], True)["config"]["password"] == "private-mail-password"
        mailbox.add(2, subject="无标题关键词但发件人匹配")
        assert check(client, item)["queued"] == 1
        # An account change establishes a fresh baseline; credentials are not echoed.
        updated["config"]["username"] = "other@example.com"
        assert not client.put(f"/api/admin/subscriptions/{item['id']}", headers=HEADERS, json=updated).json()["initialized"]


@pytest.mark.parametrize("subject,sender,settings,expected", [
    ("Service ALERT", "Else <other@example.com>", {}, True),
    ("其他", "通知服务 <alerts@example.com>", {"senders": ["alerts@example.com"]}, True),
    ("其他", "通知服务 <not-alerts@example.com>", {"senders": ["alerts@example.com"]}, False),
    ("其他", "通知服务 <other@example.com>", {"senders": ["通知"]}, True),
    ("Service ALERT", "Other <other@example.com>", {"senders": ["alerts@example.com"], "match_mode": "all"}, False),
    ("Service ALERT", "Service <ALERTS@example.com>", {"senders": ["alerts@example.com"], "match_mode": "all"}, True),
    ("其他", "Other <other@example.com>", {"subject_keywords": [], "senders": ["OTHER@example.com"], "match_mode": "all"}, True),
])
def test_matching(subject, sender, settings, expected):
    assert mail_matches(subject, sender, config(**{"subject_keywords": ["alert", "通知"], **settings})) is expected


def test_mime_plain_alternative_html_charset_and_attachments(app, mailbox):
    # Real wire parser verifies the structure layout used by the extraction code.
    structure = parse_fetch_response([b'1 (UID 2 BODYSTRUCTURE ((("TEXT" "PLAIN" ("CHARSET" "GB18030") NIL NIL "BASE64" 40 1 NIL NIL) ("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "8BIT" 100 1 NIL NIL) "ALTERNATIVE" NIL NIL) ("APPLICATION" "PDF" ("NAME" "file.pdf") NIL NIL "BASE64" 1000 NIL ("ATTACHMENT" ("FILENAME" "file.pdf"))) "MIXED" NIL NIL))'])[2][b"BODYSTRUCTURE"]
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        mailbox.add(2, structure=structure, parts={"1.1": base64.b64encode("中文正文，不带附件".encode("gb18030"))})
        assert check(client, item)["queued"] == 1
        message = app.state.db.list_messages()["items"][0]
        assert "中文正文，不带附件" in message["body"]
        assert "file.pdf" not in message["body"]
        mailbox.add(3, structure=BodyData.create(text_part(kind=b"HTML")), parts={"1": b'<p>Hello<br>world</p><script>hidden</script>'})
        assert check(client, item)["queued"] == 1
        messages = app.state.db.list_messages()["items"]
        assert any("Hello\nworld" in message["body"] for message in messages)
        assert all("hidden" not in message["body"] for message in messages)
        body_calls = [call[2][0] for call in mailbox.calls if call[0] == "fetch" and "BODYSTRUCTURE" not in call[2]]
        assert body_calls == ["BODY.PEEK[1.1]<0.131072>", "BODY.PEEK[1]<0.131072>"]


def test_attached_text_file_and_message_are_not_body():
    attachment = text_part(disposition=(b"ATTACHMENT", (b"FILENAME", b"secret.txt")))
    assert body_parts(BodyData.create(attachment)) == []
    assert body_parts(BodyData.create(text_part(params=(b"NAME", b"secret.txt")))) == []
    assert body_parts(BodyData.create((b"MESSAGE", b"RFC822", None, None, None, b"7BIT", 30))) == []


def test_failure_does_not_advance_and_retry_queues_all(app, mailbox):
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        before = app.state.db.get_subscription(item["id"])["last_success_at"]
        mailbox.add(2)
        mailbox.body_error = True
        assert check(client, item)["error"]
        assert app.state.db.get_subscription(item["id"])["last_success_at"] == before
        assert app.state.db.stats()["messages"] == 0
        mailbox.body_error = False
        assert check(client, item)["queued"] == 1


def test_uidvalidity_reset_and_bounded_backlog(app, mailbox, monkeypatch):
    monkeypatch.setattr("app.mail_subscription.MAIL_BATCH_SIZE", 2)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        for uid in range(2, 7):
            mailbox.add(uid)
        assert check(client, item)["queued"] == 5
        assert app.state.db.get_subscription(item["id"])["last_success_at"] == subscription_module.time.time()
        metadata_calls = [call for call in mailbox.calls if call[0] == "fetch" and "INTERNALDATE" in call[2]]
        assert len(metadata_calls) == 3 and all(len(call[1]) <= 2 for call in metadata_calls)
        assert check(client, item)["queued"] == 0
        mailbox.validity += 1
        assert check(client, item)["queued"] == 0
        mailbox.add(7)
        assert check(client, item)["queued"] == 1


def test_missing_headers_or_body_retry_but_deleted_mail_is_skipped(app, mailbox):
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        mailbox.add(2)
        mailbox.omit_header = True
        assert check(client, item)["error"]
        mailbox.omit_header = False
        mailbox.omit_body = True
        assert check(client, item)["error"]
        mailbox.omit_body = False
        del mailbox.messages[2]
        assert check(client, item)["queued"] == 0
        assert app.state.db.get_subscription(item["id"])["last_success_at"] == subscription_module.time.time()


def test_auth_error_sanitized(app, mailbox, caplog):
    mailbox.login_error = True
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        result = check(client, item)
        assert "认证失败" in result["error"]
        assert "private-mail-password" not in str(result) + caplog.text
        assert not result["subscription"]["initialized"]


def test_mail_strict_window_boundaries_and_new_uid_old_mail(app, mailbox):
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        start = app.state.db.get_subscription(item["id"])["last_success_at"]
        for uid, offset in ((2, -10), (3, 0), (4, 60), (5, 61)):
            mailbox.add(uid, received=datetime.fromtimestamp(start + offset, timezone.utc))
        result = check(client, item)
        assert result["queued"] == 1
        assert result["subscription"]["last_success_at"] == start + 60
        messages = app.state.db.list_messages()["items"]
        assert [message["idempotency_key"] for message in messages] == ["mail:50:4"]
        assert check(client, item)["queued"] == 1
        assert {message["idempotency_key"] for message in app.state.db.list_messages()["items"]} == {"mail:50:4", "mail:50:5"}
        body_calls = [call[1] for call in mailbox.calls if call[0] == "fetch" and "INTERNALDATE" not in call[2]]
        assert body_calls == [[4], [5]]  # No body reads for old mail, even with new UIDs.


def test_mail_arriving_during_check_is_left_for_next_window(app, mailbox, monkeypatch):
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        start = app.state.db.get_subscription(item["id"])["last_success_at"]
        original_search = mailbox.search
        injected = {"done": False}
        def during_search(criteria):
            if criteria[0] == "SINCE" and not injected["done"]:
                injected["done"] = True
                mailbox.add(2, received=datetime.fromtimestamp(start + 65, timezone.utc))
                subscription_module.time.advance(10)
            return original_search(criteria)
        monkeypatch.setattr(mailbox, "search", during_search)
        result = check(client, item)
        assert result["queued"] == 0
        assert result["subscription"]["last_success_at"] == start + 60
        assert check(client, item)["queued"] == 1


def test_later_metadata_batch_failure_keeps_entire_window(app, mailbox, monkeypatch):
    monkeypatch.setattr("app.mail_subscription.MAIL_BATCH_SIZE", 2)
    with TestClient(app) as client:
        item, _ = subscription(client, [channel(client)], "imap", config())
        check(client, item)
        start = app.state.db.get_subscription(item["id"])["last_success_at"]
        for uid in range(2, 7):
            mailbox.add(uid)
        original_fetch = mailbox.fetch
        def broken_fetch(uids, selectors):
            if "INTERNALDATE" in selectors and 4 in uids:
                raise OSError("temporary batch error")
            return original_fetch(uids, selectors)
        monkeypatch.setattr(mailbox, "fetch", broken_fetch)
        result = check(client, item)
        assert result["error"]
        assert result["subscription"]["last_success_at"] == start
        assert result["subscription"]["last_checked_at"] > start
        assert app.state.db.stats()["messages"] == 0
        monkeypatch.setattr(mailbox, "fetch", original_fetch)
        assert check(client, item)["queued"] == 5


@pytest.mark.parametrize("values", [{"provider": "other"}, {"provider": []}, {"password": ""},
    {"username": "invalid"}, {"subject_keywords": [], "senders": []}, {"match_mode": "invalid"},
    {"subject_keywords": "a"}, {"senders": [123]}, {"subject_keywords": ["a"] * 31}])
def test_invalid_mail_configuration(values):
    with pytest.raises(ValueError):
        validate_mail_config(config(**values))
