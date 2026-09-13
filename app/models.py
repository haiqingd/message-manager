from typing import Literal

from pydantic import BaseModel, Field, field_validator


ChannelType = Literal["dingtalk", "feishu", "email"]


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: ChannelType
    enabled: bool = True
    config: dict[str, object]


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
