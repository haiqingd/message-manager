from typing import Literal

from pydantic import BaseModel, Field, field_validator


ChannelType = Literal["dingtalk", "feishu", "qq", "email"]


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: ChannelType
    enabled: bool = True
    config: dict[str, object]


class RouteInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    event_types: list[str] = Field(min_length=1, max_length=8)
    chat_type: Literal["any", "group", "user"] = "any"
    openid: str = Field(default="", max_length=128)
    action: Literal["log", "channel", "forward", "party"]
    config: dict[str, object] = Field(default_factory=dict)


class Destination(BaseModel):
    channel_id: str
    recipients: list[str] = Field(default_factory=list, max_length=50)


class MessageInput(BaseModel):
    source: str = Field(default="default", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    destinations: list[Destination] = Field(min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("destinations")
    @classmethod
    def distinct_destinations(cls, value: list[Destination]) -> list[Destination]:
        ids = [item.channel_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Each channel may appear only once per message")
        return value


class SubscriptionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["rss", "x", "imap"] = "rss"
    enabled: bool = True
    config: dict[str, object] = Field(default_factory=dict)
    destinations: list[Destination] = Field(min_length=1, max_length=20)
    poll_interval: int = Field(default=300, ge=60, le=86400)

    @field_validator("destinations")
    @classmethod
    def distinct_destinations(cls, value):
        return MessageInput.distinct_destinations(value)
