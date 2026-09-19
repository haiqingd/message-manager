"""组队游戏插槽：把群里的 @ 消息变成当日的组队报名板。

指令（不区分大小写，按顺序匹配，命中其一即处理）：
- 「查询组队 / 谁组队 / 名单」    → 展示当前报名名单和预计上号时间
- 「退出 / 不参加 / 取消」        → 取消自己的报名
- 「参加 / 加入 / 报名 / 上号 / +1」→ 报名，可附带时间和昵称（如「参加 21点 我是小明」）
- 「组队 / 斗 / 有无 / 开黑」等    → 创建（或展示）当日组队邀请卡片

回复以纯文本卡片 + 指令按钮键盘发出；按钮是「指令型」，点击后相当于该成员
@ 机器人发送对应指令，会重新进入本插槽，因此每次交互都有新的被动回复窗口。
未命中任何指令的消息会穿透到下一条路由规则（例如兜底的自动建渠道）。
"""

import re
import time

from . import senders
from .database import Database


QUERY_KEYS = ("查询组队", "组队查询", "谁组队", "谁参加", "组队名单", "名单")
LEAVE_KEYS = ("不参加", "退出", "取消", "咕了")
JOIN_KEYS = ("参加", "加入", "报名", "上号", "+1", "＋1", "冲", "带我", "来一")
CREATE_KEYS = ("组队", "斗", "有无", "开黑", "排位", "来人", "等人", "上分")

TIME_PATTERN = re.compile(r"\d{1,2}\s*[:：点时]\s*\d{0,2}\s*半?|随时")
NAME_PATTERN = re.compile(r"(?:我是|昵称|代号|叫我)\s*([^\s，。,]{1,12})")


def classify(content: str) -> str | None:
    """Map an @ message to a party command, or None when unrelated."""
    text = str(content or "").strip().casefold()
    if not text:
        return None
    if any(key in text for key in QUERY_KEYS):
        return "query"
    if any(key in text for key in LEAVE_KEYS):
        return "leave"
    if any(key in text for key in JOIN_KEYS):
        return "join"
    if any(key in text for key in CREATE_KEYS):
        return "create"
    return None


def _extract_time(content: str) -> str:
    match = TIME_PATTERN.search(content)
    return match.group(0).replace(" ", "") if match else ""


def _extract_name(content: str) -> str:
    match = NAME_PATTERN.search(content)
    return match.group(1) if match else ""


def _display_name(member_openid: str, name: str) -> str:
    return name or member_openid[:8]


def party_keyboard() -> dict:
    """Inline command keyboard; clicking a button makes the member send the
    button's data string as an @bot message, which re-enters this slot."""

    def button(button_id: str, label: str, visited: str, style: int, data: str) -> dict:
        return {
            "id": button_id,
            "render_data": {"label": label, "visited_label": visited, "style": style},
            "action": {"type": 2, "permission": {"type": 2}, "data": data,
                       "at_bot_show_channel_list": False},
        }

    return {"content": {"rows": [
        {"buttons": [
            button("join", "✅ 我参加", "已登记 ✓", 1, "参加"),
            button("time20", "20点", "已选 20点", 0, "参加 20点"),
            button("time21", "21点", "已选 21点", 0, "参加 21点"),
            button("time22", "22点", "已选 22点", 0, "参加 22点"),
        ]},
        {"buttons": [
            button("quit", "❌ 退出", "已退出", 0, "退出"),
            button("query", "📋 查询组队", "已刷新", 0, "查询组队"),
        ]},
    ]}}


def party_card(party: dict, members: list[dict]) -> str:
    lines = [f"🎮 组队游戏邀请 · {party['day'][5:]}"]
    if party.get("creator_name"):
        lines.append(f"发起人：{party['creator_name']}")
    if not members:
        lines.append("还没有人报名，来做第一个！")
    else:
        lines.append(f"已报名 {len(members)} 人：")
        for index, member in enumerate(members, 1):
            expected = f" · 预计 {member['expected_time']}" if member["expected_time"] else ""
            lines.append(f"{index}. {member['name']}{expected}")
    lines.append("👇 点按钮，或 @我 发送「参加 21点」；发送「查询组队」看名单")
    return "\n".join(lines)


async def handle_event(db: Database, credentials: dict, app_id: str, data: dict, slot_config: dict) -> str:
    """Run one party command and reply with the updated card."""
    command = classify(str(data.get("content") or ""))
    if not command:
        return "message did not match any party command"
    group_openid = str(data.get("group_openid") or "").strip()
    member_openid = str((data.get("author") or {}).get("member_openid")
                        or (data.get("author") or {}).get("id") or "").strip()
    if not group_openid or not member_openid:
        return "party event is missing group/member openid; skipped"
    content = str(data.get("content") or "")

    if command == "leave":
        party = db.qq_party_today(group_openid)
        if party:
            db.qq_party_leave(party["id"], member_openid)
        detail = f"{_display_name(member_openid, '')} 退出了组队"
    else:
        party = db.qq_party_ensure(group_openid, member_openid,
                                   _display_name(member_openid, _extract_name(content)))
        if command in ("join", "create"):
            db.qq_party_join(party["id"], member_openid, _extract_name(content), _extract_time(content))
            detail = f"{_display_name(member_openid, _extract_name(content))} {'创建了组队' if command == 'create' else '加入了组队'}"
        else:
            detail = "查询组队名单"

    card = party_card(party, db.qq_party_members(party["id"]))
    message_id = str(data.get("id") or "")
    if not message_id:
        return f"{detail}; no msg_id to reply to"
    seq = db.claim_qq_reply_seq(message_id)
    if seq is None:
        return f"{detail}; 回复次数已用完，请重新 @ 机器人"
    config = {"app_id": app_id, "client_secret": credentials["client_secret"],
              "group_openid": group_openid}
    template_id = str(slot_config.get("markdown_template_id") or "")
    want_buttons = slot_config.get("buttons", True)
    # Groups only render buttons attached to markdown messages, and markdown
    # requires a platform-approved template; without one we send plain text.
    await senders.send_qq_reply(config, card, message_id, seq,
                                keyboard=party_keyboard() if want_buttons and template_id else None,
                                markdown_template_id=template_id)
    if want_buttons and not template_id:
        detail += "；未附按钮：群聊按钮需要 Markdown 模板，请在路由配置中填写 markdown_template_id"
    return detail
