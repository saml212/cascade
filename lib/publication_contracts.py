"""Validated request contracts for explicit publication actions."""

from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
)


def _uuid_string(value: object) -> UUID:
    if not isinstance(value, str):
        raise TypeError("Short destination request_id must be a UUID string")
    return UUID(value)


RequestId = Annotated[UUID, BeforeValidator(_uuid_string)]


class ShortDestinationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    destinations: list[StrictStr] = Field(min_length=1)
    clip_ids: list[StrictStr] | None = None
    request_id: RequestId
    actor: StrictStr = Field(min_length=1, max_length=200)
    reason: StrictStr = Field(min_length=3, max_length=1000)
    expected_release_revision: StrictStr = Field(min_length=1)
    variant_overrides: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    copy_overrides: dict[StrictStr, dict[StrictStr, StrictStr]] = Field(
        default_factory=dict
    )
    publish_now: StrictBool = False


class ShortDestinationExecution(ShortDestinationRequest):
    preview_revision: StrictStr = Field(min_length=1)
