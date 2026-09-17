"""Validated request contracts for explicit publication actions."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ShortDestinationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destinations: list[str] = Field(min_length=1)
    clip_ids: list[str] | None = None
    request_id: UUID
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=1000)
    expected_release_revision: str = Field(min_length=1)
    variant_overrides: dict[str, str] = Field(default_factory=dict)
    copy_overrides: dict[str, dict] = Field(default_factory=dict)
    publish_now: bool = False


class ShortDestinationExecution(ShortDestinationRequest):
    preview_revision: str = Field(min_length=1)
