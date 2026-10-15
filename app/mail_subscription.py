"""Read-only IMAP subscriptions. Only matching message bodies are downloaded."""
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import getaddresses

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .senders import valid_email
from .subscriptions import SourceError, plain_text

PROVIDERS = {"qq": "imap.qq.com", "gmail": "imap.gmail.com"}
MAIL_BATCH_SIZE = 100
MAX_PART_BYTES = 128 * 1024


def validate_mail_config(config):
    provider = config.get("provider", "qq")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("邮箱类型仅支持 QQ 邮箱或 Gmail")
    username = str(config.get("username") or "").strip().lower()
    if not valid_email(username) or len(username) > 254:
        raise ValueError("请填写完整、有效的邮箱地址")
    password = "".join(str(config.get("password") or "").split())
    if not password or len(password) > 512:
        raise ValueError("请填写 QQ 邮箱授权码或 Gmail 应用专用密码")
    filters = {}
    for key in ("subject_keywords", "senders"):
        values = config.get(key, [])
        if not isinstance(values, list) or len(values) > 30:
            raise ValueError("每类过滤条件最多填写 30 项")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
            raise ValueError("过滤条件必须是 1–200 个字符的文本")
        filters[key] = list(dict.fromkeys(value.strip() for value in values))
    if not any(filters.values()):
        raise ValueError("请至少填写一个标题关键词或发件人条件")
    match_mode = config.get("match_mode", "any")
    if match_mode not in ("any", "all"):
        raise ValueError("请选择任一条件满足或全部条件满足")
    return {"provider": provider, "username": username, "password": password,
            "match_mode": match_mode, **filters}


def mail_matches(subject, sender, config):
    checks = []
    if config["subject_keywords"]:
        checks.append(any(value.casefold() in subject.casefold() for value in config["subject_keywords"]))
    if config["senders"]:
        addresses = getaddresses([sender])
        checks.append(any(
            any(value.casefold() == address.casefold() for _, address in addresses) if "@" in value
            else any(value.casefold() in name.casefold() for name, _ in addresses)
            for value in config["senders"]))
    return bool(checks) and (all(checks) if config["match_mode"] == "all" else any(checks))


def _text(value):
    return value.decode("ascii", errors="replace").lower() if isinstance(value, bytes) else str(value or "").lower()


def _parameters(values):
    return {_text(values[i]): values[i + 1] for i in range(0, len(values) - 1, 2)} if isinstance(values, (tuple, list)) else {}


def body_parts(node, prefix="", depth=0):
    """Choose readable MIME parts, excluding named files and attached messages."""
    if depth > 20 or not isinstance(node, (tuple, list)) or len(node) < 2:
        raise SourceError("邮件 MIME 结构异常，未更新进度")
    multipart = isinstance(node[0], list)
    if multipart:
        params = _parameters(node[2] if len(node) > 2 else None)
        disposition = node[3] if len(node) > 3 else None
    else:
        if len(node) < 7:
            raise SourceError("邮件 MIME 结构不完整，未更新进度")
        params = _parameters(node[2])
        disposition = node[9] if _text(node[0]) == "text" and len(node) > 9 else None
    disposition_params = _parameters(disposition[1] if isinstance(disposition, tuple) and len(disposition) > 1 else None)
    if (isinstance(disposition, tuple) and _text(disposition[0]) == "attachment") or any(
            key.startswith(("filename", "name")) for key in (*params, *disposition_params)):
        return []
    if multipart:
        children = [body_parts(child, f"{prefix}.{i}".lstrip("."), depth + 1) for i, child in enumerate(node[0], 1)]
        if _text(node[1]) == "alternative":
            return next((parts for parts in children if any(_text(part[1][1]) == "plain" for part in parts)),
                        next((parts for parts in children if parts), []))
        return [part for parts in children for part in parts]
    if _text(node[0]) == "text" and _text(node[1]) in ("plain", "html"):
        return [(prefix or "1", node)]
    return []


def decode_part(raw, node):
    message = EmailMessage()
    message["Content-Transfer-Encoding"] = _text(node[5])
    message.set_payload(raw)
    decoded = message.get_payload(decode=True) or b""
    charset = _text(_parameters(node[2]).get("charset", b"utf-8"))
    try:
        text = decoded.decode(charset, errors="replace")
    except LookupError:
        text = decoded.decode("utf-8", errors="replace")
    return plain_text(text) if _text(node[1]) == "html" else text.strip()


def _mail_body(client, uid, structure):
    parts = body_parts(structure)
    if len(parts) > 16:
        raise SourceError("邮件正文分段过多，未更新进度")
    bodies = []
    truncated = False
    for section, node in parts:
        key = f"BODY[{section}]<0>".encode()
        response = client.fetch([uid], [f"BODY.PEEK[{section}]<0.{MAX_PART_BYTES}>"])
        if uid not in response:
            if not client.search(["UID", str(uid)]):
                return None  # Removed while fetching; do not block later mail.
            raise SourceError("未能读取邮件正文，未更新进度")
        # Some servers omit the origin marker when a whole part fits.
        raw = response[uid].get(key, response[uid].get(f"BODY[{section}]".encode()))
        if not isinstance(raw, bytes):
            raise SourceError("邮件正文响应不完整，未更新进度")
        bodies.append(decode_part(raw, node))
        truncated |= len(raw) >= MAX_PART_BYTES
    body = "\n\n".join(value for value in bodies if value) or "（无可转发的文本正文）"
    if len(body) > 16000:
        body = body[:16000]
        truncated = True
    return body + ("\n\n（正文过长，已截取）" if truncated else "")


def fetch_mail(subscription):
    """Run in a worker thread; return entries and progress for atomic DB commit."""
    config = subscription["config"]
    try:
        with IMAPClient(PROVIDERS[config["provider"]], port=993, ssl=True,
                        ssl_context=ssl.create_default_context(), timeout=30) as client:
            client.login(config["username"], config["password"])
            if client.has_capability("ID"):
                client.id_({"name": "MessageManager", "version": "0.1"})
            client.normalise_times = False
            folder = client.select_folder("INBOX", readonly=True)
            validity = folder.get(b"UIDVALIDITY")
            if not isinstance(validity, int) or validity < 1:
                raise SourceError("邮箱未返回有效的 UIDVALIDITY，未更新进度")
            window_start, window_end = subscription["window_start"], subscription["window_end"]
            state = {"uidvalidity": validity}
            if window_start is None:
                return [], state
            # IMAP searches by calendar date, not seconds. Widen by a day for
            # timezone offsets, then enforce the exact interval on INTERNALDATE.
            criteria = ["SINCE", datetime.fromtimestamp(window_start, timezone.utc).date() - timedelta(days=1),
                        "BEFORE", datetime.fromtimestamp(window_end, timezone.utc).date() + timedelta(days=2)]
            uids = sorted(set(client.search(criteria)))
            entries = []
            for offset in range(0, len(uids), MAIL_BATCH_SIZE):
                batch = uids[offset:offset + MAIL_BATCH_SIZE]
                rows = client.fetch(batch, ["BODY.PEEK[HEADER]", "BODYSTRUCTURE", "INTERNALDATE"])
                for uid in batch:
                    row = rows.get(uid)
                    if row is None:
                        if not client.search(["UID", str(uid)]):
                            continue
                        raise SourceError("未能读取邮件头，未更新进度")
                    received = row.get(b"INTERNALDATE")
                    if not isinstance(received, datetime) or received.tzinfo is None:
                        raise SourceError("邮件缺少有效接收时间，未更新检查进度")
                    received_at = received.timestamp()
                    if not window_start < received_at <= window_end:
                        continue
                    raw_headers = row.get(b"BODY[HEADER]")
                    if not isinstance(raw_headers, bytes):
                        raise SourceError("邮件头响应不完整，未更新进度")
                    headers = BytesHeaderParser(policy=policy.default).parsebytes(raw_headers)
                    subject = str(headers.get("Subject", ""))
                    sender = str(headers.get("From", ""))
                    if not mail_matches(subject, sender, config):
                        continue
                    body = _mail_body(client, uid, row.get(b"BODYSTRUCTURE"))
                    if body is None:
                        continue
                    entries.append({"id": f"mail:{validity}:{uid}", "title": subject or "（无标题邮件）",
                                    "body": f"发件人：{sender or '未知'}\n标题：{subject or '（无标题）'}\n"
                                            f"接收时间：{received.isoformat()}\n\n{body}",
                                    "published_at": received_at})
            # Commit the whole window only after every metadata batch succeeds.
            # UID cursors would incorrectly hide late-visible or out-of-order mail.
            return entries, state
    except SourceError:
        raise
    except LoginError:
        raise SourceError("邮箱认证失败：QQ 请检查 IMAP 授权码，Gmail 请检查应用专用密码及账号权限") from None
    except (OSError, IMAPClientError):
        raise SourceError("邮箱连接或读取失败，请检查 IMAP 服务、网络和账号权限；进度已保留") from None
