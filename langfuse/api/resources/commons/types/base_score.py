# This file was auto-generated by Fern from our API Definition.

import datetime as dt
import typing

from ....core.datetime_utils import serialize_datetime
from ....core.pydantic_utilities import deep_union_pydantic_dicts, pydantic_v1
from .score_source import ScoreSource


class BaseScore(pydantic_v1.BaseModel):
    id: str
    trace_id: str = pydantic_v1.Field(alias="traceId")
    name: str
    source: ScoreSource
    observation_id: typing.Optional[str] = pydantic_v1.Field(
        alias="observationId", default=None
    )
    timestamp: dt.datetime
    created_at: dt.datetime = pydantic_v1.Field(alias="createdAt")
    updated_at: dt.datetime = pydantic_v1.Field(alias="updatedAt")
    author_user_id: typing.Optional[str] = pydantic_v1.Field(
        alias="authorUserId", default=None
    )
    comment: typing.Optional[str] = None
    config_id: typing.Optional[str] = pydantic_v1.Field(alias="configId", default=None)
    """
    Reference a score config on a score. When set, config and score name must be equal and value must comply to optionally defined numerical range
    """

    queue_id: typing.Optional[str] = pydantic_v1.Field(alias="queueId", default=None)
    """
    Reference an annotation queue on a score. Populated if the score was initially created in an annotation queue.
    """

    environment: typing.Optional[str] = pydantic_v1.Field(default=None)
    """
    The environment from which this score originated. Can be any lowercase alphanumeric string with hyphens and underscores that does not start with 'langfuse'.
    """

    def json(self, **kwargs: typing.Any) -> str:
        kwargs_with_defaults: typing.Any = {
            "by_alias": True,
            "exclude_unset": True,
            **kwargs,
        }
        return super().json(**kwargs_with_defaults)

    def dict(self, **kwargs: typing.Any) -> typing.Dict[str, typing.Any]:
        kwargs_with_defaults_exclude_unset: typing.Any = {
            "by_alias": True,
            "exclude_unset": True,
            **kwargs,
        }
        kwargs_with_defaults_exclude_none: typing.Any = {
            "by_alias": True,
            "exclude_none": True,
            **kwargs,
        }

        return deep_union_pydantic_dicts(
            super().dict(**kwargs_with_defaults_exclude_unset),
            super().dict(**kwargs_with_defaults_exclude_none),
        )

    class Config:
        frozen = True
        smart_union = True
        allow_population_by_field_name = True
        populate_by_name = True
        extra = pydantic_v1.Extra.allow
        json_encoders = {dt.datetime: serialize_datetime}
