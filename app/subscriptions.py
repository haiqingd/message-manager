"""Polling sources feed the existing durable delivery queue."""
import asyncio
import calendar
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import feedparser
import httpx

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SourceError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class PollRateLimited(Exception):
    pass


def validate_subscription(kind, config):
    if kind == "imap":
        from .mail_subscription import validate_mail_config
        return validate_mail_config(config)
    if kind == "rss":
        url = str(config.get("feed_url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("RSS 地址必须是 HTTP/HTTPS URL，不能包含用户名或密码")
        return {"feed_url": url}
    username = str(config.get("username") or "").strip()
    if username.startswith(("https://", "http://")):
        parsed = urlparse(username)
        if parsed.hostname not in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
            raise ValueError("请输入 X 账号名或 x.com 账号主页地址")
        username = parsed.path.strip("/")
    username = username.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        raise ValueError("X 账号名只能包含 1–15 个字母、数字或下划线")
    token = str(config.get("bearer_token") or "").strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("请配置有效的 X API Bearer Token")
    for key in ("include_replies", "include_reposts"):
        if key in config and not isinstance(config[key], bool):
            raise ValueError("回复和转帖选项必须是布尔值")
    return {"username": username.lower(), "bearer_token": token,
            "include_replies": config.get("include_replies", False),
            "include_reposts": config.get("include_reposts", False)}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def iso_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


async def fetch_response(client, url, *, headers=None, params=None):
    # Never include provider URLs, bodies or credentials in errors/logs.
    async with client.stream("GET", url, headers=headers, params=params) as response:
        if response.status_code == 304:
            return response, b""
        if response.status_code >= 400:
            retry_after = 0
            if response.status_code == 429:
                try:
                    retry_after = max(float(response.headers.get("retry-after", "0")),
                                      float(response.headers.get("x-rate-limit-reset", "0")) - time.time(), 60)
                except ValueError:
                    retry_after = 900
            reason = {401: "认证失败，请检查凭据", 402: "额度不足", 403: "没有访问权限",
                      404: "账号或订阅地址不存在", 429: "请求频率受限，已延后检查"}.get(response.status_code, "上游服务请求失败")
            raise SourceError(f"{reason}（HTTP {response.status_code}）", retry_after)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise SourceError("订阅响应超过 5 MB，未更新游标")
        return response, bytes(body)


async def fetch_rss(client, subscription):
    state = dict(subscription["state"])
    headers = {}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    response, content = await fetch_response(client, subscription["config"]["feed_url"], headers=headers)
    if response.status_code == 304:
        return [], state
    feed = feedparser.parse(content, response_headers={"content-location": str(response.url),
                                                     "content-type": response.headers.get("content-type", "")})
    # Some valid feeds are served as text/plain or with a wrong charset.
    # Those metadata warnings do not mean the XML itself is malformed.
    metadata_warning = isinstance(feed.get("bozo_exception"),
                                  (feedparser.NonXMLContentType, feedparser.CharacterEncodingOverride))
    if not feed.version or (feed.bozo and not metadata_warning):
        raise SourceError("订阅地址未返回有效的 RSS/Atom，未更新游标")
    entries = []
    future_entries = False
    for item in reversed(feed.entries):
        date = item.get("published_parsed") or item.get("updated_parsed")
        published_at = calendar.timegm(date) if date else None
        future_entries |= published_at is not None and published_at > subscription.get("window_end", time.time())
        title = plain_text(item.get("title")) or subscription["name"]
        contents = item.get("content") or []
        body = plain_text(contents[0].get("value") if contents else item.get("summary"))
        link = item.get("link", "")
        # Stable fallback ignores mutable summaries when a title/date is available.
        identity = item.get("id") or link or f"{title}|{item.get('published') or item.get('updated') or body}"
        parts = [body or title]
        if item.get("published"):
            parts.append(f"发布时间：{item['published']}")
        if urlparse(link).scheme in ("http", "https"):
            parts.append(link)
        entries.append({"id": hashlib.sha256(identity.encode()).hexdigest(), "title": title,
                        "body": "\n\n".join(parts), "published_at": published_at})
    state = {key: value for key, value in {"etag": response.headers.get("etag"),
             "last_modified": response.headers.get("last-modified")}.items() if value}
    if future_entries:
        # A 304 next time would otherwise hide items just beyond this cutoff.
        state = {}
    return entries, state


async def x_json(client, path, token, params=None):
    _, content = await fetch_response(client, f"https://api.x.com/2/{path}",
                                      headers={"Authorization": f"Bearer {token}"}, params=params)
    try:
        result = json.loads(content)
    except (ValueError, UnicodeError):
        raise SourceError("X API 返回了无效响应") from None
    if not isinstance(result, dict) or result.get("errors"):
        raise SourceError("X API 返回错误，未更新游标，请检查账号和接口权限")
    return result


async def fetch_x(client, subscription):
    config = subscription["config"]
    state = dict(subscription["state"])
    username, token = config["username"], config["bearer_token"]
    if not state.get("user_id"):
        user = await x_json(client, f"users/by/username/{username}", token)
        user_id = (user.get("data") or {}).get("id")
        if not user_id or not str(user_id).isdigit():
            raise SourceError("无法解析 X 账号 ID")
        state["user_id"] = str(user_id)
    params = {"max_results": 100 if subscription["window_start"] is not None else 5,
              "tweet.fields": "created_at,entities"}
    exclude = []
    if not config["include_replies"]:
        exclude.append("replies")
    if not config["include_reposts"]:
        exclude.append("retweets")
    if exclude:
        params["exclude"] = ",".join(exclude)
    state.pop("since_id", None)
    if subscription["window_start"] is not None:
        # The query includes the start second; the exact exclusive lower bound
        # and inclusive upper bound are enforced again in the DB transaction.
        params["start_time"] = datetime.fromtimestamp(subscription["window_start"], timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        params["end_time"] = datetime.fromtimestamp(subscription["window_end"], timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    entries = []
    seen_tokens = set()
    while True:
        page = await x_json(client, f"users/{state['user_id']}/tweets", token, params)
        if "data" not in page and (page.get("meta") or {}).get("result_count") != 0:
            raise SourceError("X API 时间线响应不完整，未更新游标")
        for post in page.get("data") or []:
            post_id = str(post.get("id") or "")
            if not post_id.isdigit() or not isinstance(post.get("text"), str):
                raise SourceError("X API 推文数据不完整，未更新游标")
            published_at = iso_timestamp(post.get("created_at"))
            if published_at is None:
                raise SourceError("X API 推文缺少有效发布时间，未更新检查进度")
            text = post["text"]
            for entity in (post.get("entities") or {}).get("urls", []):
                if entity.get("url") and entity.get("expanded_url"):
                    text = text.replace(entity["url"], entity["expanded_url"])
            parts = [text]
            if post.get("created_at"):
                parts.append(f"发布时间：{post['created_at']}")
            parts.append(f"https://x.com/{username}/status/{post_id}")
            entries.append({"id": post_id, "title": f"@{username} 发布了新推文", "body": "\n\n".join(parts), "published_at": published_at})
        next_token = (page.get("meta") or {}).get("next_token")
        if not next_token or subscription["window_start"] is None:
            break
        if next_token in seen_tokens or len(seen_tokens) >= 100:
            raise SourceError("X API 分页异常或积压超过 100 页，未更新游标")
        seen_tokens.add(next_token)
        params["pagination_token"] = next_token
    entries.sort(key=lambda entry: int(entry["id"]))
    return entries, state


class SubscriptionPoller:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    async def check(self, subscription_id, due_only=False):
        async with self.lock:
            subscription = self.db.get_subscription(subscription_id, include_config=True)
            if not subscription:
                raise ValueError("Subscription not found")
            if not subscription["enabled"]:
                raise ValueError("订阅已停用，请先启用")
            if subscription["next_poll_at"] > time.time():
                if due_only:
                    return {"queued": 0, "skipped": True}
                if subscription["last_error"] and "HTTP 429" in subscription["last_error"]:
                    raise PollRateLimited("上游正在限流，请等到下次检查时间")
            try:
                subscription["window_start"] = subscription["last_success_at"] if subscription["initialized"] else None
                subscription["window_end"] = time.time()
                if subscription["window_start"] is not None and subscription["window_end"] <= subscription["window_start"]:
                    raise SourceError("当前时间未晚于上次成功检查时间，请检查系统时钟")
                if subscription["kind"] == "imap":
                    from .mail_subscription import fetch_mail
                    entries, state = await asyncio.to_thread(fetch_mail, subscription)
                else:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=subscription["kind"] == "rss",
                                                 headers={"User-Agent": "MessageManager/0.1 subscriptions"}) as client:
                        fetch = fetch_x if subscription["kind"] == "x" else fetch_rss
                        entries, state = await fetch(client, subscription)
                count = self.db.apply_subscription_entries(subscription, entries, state)
                return {"queued": count, "baseline": subscription["window_start"] is None,
                        "subscription": self.db.get_subscription(subscription_id)}
            except Exception as exc:
                if isinstance(exc, SourceError):
                    error = str(exc)
                elif isinstance(exc, ValueError):
                    error = str(exc) if "Channel " in str(exc) or "channel " in str(exc) else "订阅数据无效，请检查配置"
                elif isinstance(exc, httpx.HTTPError):
                    error = "抓取连接失败或超时，请检查源服务和网络"
                else:
                    error = "订阅检查失败，未更新游标"
                self.db.fail_subscription(subscription, error, getattr(exc, "retry_after", 0))
                logger.warning("Subscription %s failed: %s", subscription_id, error)
                return {"queued": 0, "error": error, "subscription": self.db.get_subscription(subscription_id)}

    async def run(self):
        while True:
            try:
                for subscription in self.db.list_subscriptions(due_only=True):
                    # It may have been removed/disabled since the due snapshot.
                    try:
                        await self.check(subscription["id"], due_only=True)
                    except ValueError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Subscription scheduler failed; retrying")
            await asyncio.sleep(1)
