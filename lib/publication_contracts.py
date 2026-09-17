"""Validated request contracts for explicit publication actions."""

from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
)
from pydantic_core import PydanticCustomError


def _uuid_string(value: object) -> UUID:
    if not isinstance(value, str):
        raise PydanticCustomError(
            "uuid_type", "Short destination request_id must be a UUID string"
        )
    return UUID(value)


RequestId = Annotated[UUID, BeforeValidator(_uuid_string)]


class ShortDestinationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    destinations: list[str] = Field(min_length=1)
    clip_ids: list[str] | None = None
    request_id: RequestId
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=1000)
    expected_release_revision: str = Field(min_length=1)
    variant_overrides: dict[str, str] = Field(default_factory=dict)
    copy_overrides: dict[str, dict[str, dict[str, str]]] = Field(default_factory=dict)
    publish_now: bool = False


class ShortDestinationExecution(ShortDestinationRequest):
    preview_revision: str = Field(min_length=1)
