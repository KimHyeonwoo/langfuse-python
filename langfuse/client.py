import datetime as dt
import logging
import os
import time
import tracemalloc
import typing
import urllib.parse
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Sequence, Union, overload

import backoff
import httpx

from langfuse.api.resources.commons.types.dataset_run_with_items import (
    DatasetRunWithItems,
)
from langfuse.api.resources.commons.types.observations_view import ObservationsView
from langfuse.api.resources.commons.types.session import Session
from langfuse.api.resources.commons.types.trace_with_details import TraceWithDetails
from langfuse.api.resources.datasets.types.paginated_dataset_runs import (
    PaginatedDatasetRuns,
)
from langfuse.api.resources.ingestion.types.create_event_body import CreateEventBody
from langfuse.api.resources.ingestion.types.create_generation_body import (
    CreateGenerationBody,
)
from langfuse.api.resources.ingestion.types.create_span_body import CreateSpanBody
from langfuse.api.resources.ingestion.types.score_body import ScoreBody
from langfuse.api.resources.ingestion.types.sdk_log_body import SdkLogBody
from langfuse.api.resources.ingestion.types.trace_body import TraceBody
from langfuse.api.resources.ingestion.types.update_generation_body import (
    UpdateGenerationBody,
)
from langfuse.api.resources.ingestion.types.update_span_body import UpdateSpanBody
from langfuse.api.resources.observations.types.observations_views import (
    ObservationsViews,
)
from langfuse.api.resources.prompts.types import (
    CreatePromptRequest_Chat,
    CreatePromptRequest_Text,
    Prompt_Chat,
    Prompt_Text,
)
from langfuse.api.resources.trace.types.traces import Traces
from langfuse.api.resources.utils.resources.pagination.types.meta_response import (
    MetaResponse,
)
from langfuse.model import (
    ChatMessageDict,
    ChatPromptClient,
    CreateDatasetItemRequest,
    CreateDatasetRequest,
    CreateDatasetRunItemRequest,
    DatasetItem,
    DatasetStatus,
    ModelUsage,
    PromptClient,
    TextPromptClient,
)
from langfuse.parse_error import handle_fern_exception
from langfuse.prompt_cache import PromptCache

try:
    import pydantic.v1 as pydantic  # type: ignore
except ImportError:
    import pydantic  # type: ignore

from langfuse._task_manager.task_manager import TaskManager
from langfuse.api.client import FernLangfuse
from langfuse.environment import get_common_release_envs
from langfuse.logging import clean_logger
from langfuse.model import Dataset, MapValue, Observation, TraceWithFullDetails
from langfuse.request import LangfuseClient
from langfuse.types import MaskFunction, ScoreDataType, SpanLevel
from langfuse.utils import (
    _convert_usage_input,
    _create_prompt_context,
    _get_timestamp,
    _extract_usage_details,
)

from .version import __version__ as version


@dataclass
class FetchTracesResponse:
    """Response object for fetch_traces method."""

    data: typing.List[TraceWithDetails]
    meta: MetaResponse


@dataclass
class FetchTraceResponse:
    """Response object for fetch_trace method."""

    data: TraceWithFullDetails


@dataclass
class FetchObservationsResponse:
    """Response object for fetch_observations method."""

    data: typing.List[ObservationsView]
    meta: MetaResponse


@dataclass
class FetchObservationResponse:
    """Response object for fetch_observation method."""

    data: Observation


@dataclass
class FetchSessionsResponse:
    """Response object for fetch_sessions method."""

    data: typing.List[Session]
    meta: MetaResponse


class Langfuse(object):
    """Langfuse Python client.

    Attributes:
        log (logging.Logger): Logger for the Langfuse client.
        base_url (str): Base URL of the Langfuse API, serving as the root address for API endpoint construction.
        httpx_client (httpx.Client): HTTPX client utilized for executing requests to the Langfuse API.
        client (FernLangfuse): Core interface for Langfuse API interaction.
        task_manager (TaskManager): Task Manager dedicated to handling asynchronous tasks.
        release (str): Identifies the release number or hash of the application.
        prompt_cache (PromptCache): A cache for efficiently storing and retrieving PromptClient instances.

    Example:
        Initiating the Langfuse client should always be first step to use Langfuse.
        ```python
        import os
        from langfuse import Langfuse

        # Set the public and secret keys as environment variables
        os.environ['LANGFUSE_PUBLIC_KEY'] = public_key
        os.environ['LANGFUSE_SECRET_KEY'] = secret_key

        # Initialize the Langfuse client using the credentials
        langfuse = Langfuse()
        ```
    """

    log = logging.getLogger("langfuse")
    """Logger for the Langfuse client."""

    host: str
    """Host of Langfuse API."""

    def __init__(
        self,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        host: Optional[str] = None,
        release: Optional[str] = None,
        debug: bool = False,
        threads: Optional[int] = None,
        flush_at: Optional[int] = None,
        flush_interval: Optional[float] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[int] = None,  # seconds
        sdk_integration: Optional[str] = "default",
        httpx_client: Optional[httpx.Client] = None,
        enabled: Optional[bool] = True,
        sample_rate: Optional[float] = None,
        mask: Optional[MaskFunction] = None,
    ):
        """Initialize the Langfuse client.

        Args:
            public_key: Public API key of Langfuse project. Can be set via `LANGFUSE_PUBLIC_KEY` environment variable.
            secret_key: Secret API key of Langfuse project. Can be set via `LANGFUSE_SECRET_KEY` environment variable.
            host: Host of Langfuse API. Can be set via `LANGFUSE_HOST` environment variable. Defaults to `https://cloud.langfuse.com`.
            release: Release number/hash of the application to provide analytics grouped by release. Can be set via `LANGFUSE_RELEASE` environment variable.
            debug: Enables debug mode for more verbose logging. Can be set via `LANGFUSE_DEBUG` environment variable.
            threads: Number of consumer threads to execute network requests. Helps scaling the SDK for high load. Only increase this if you run into scaling issues.
            flush_at: Max batch size that's sent to the API.
            flush_interval: Max delay until a new batch is sent to the API.
            max_retries: Max number of retries in case of API/network errors.
            timeout: Timeout of API requests in seconds. Defaults to 20 seconds.
            httpx_client: Pass your own httpx client for more customizability of requests.
            sdk_integration: Used by intgerations that wrap the Langfuse SDK to add context for debugging and support. Not to be used directly.
            enabled: Enables or disables the Langfuse client. If disabled, all observability calls to the backend will be no-ops.
            sample_rate: Sampling rate for tracing. If set to 0.2, only 20% of the data will be sent to the backend. Can be set via `LANGFUSE_SAMPLE_RATE` environment variable.
            mask (langfuse.types.MaskFunction): Masking function for 'input' and 'output' fields in events. Function must take a single keyword argument `data` and return a serializable, masked version of the data.

        Raises:
            ValueError: If public_key or secret_key are not set and not found in environment variables.

        Example:
            Initiating the Langfuse client should always be first step to use Langfuse.
            ```python
            import os
            from langfuse import Langfuse

            # Set the public and secret keys as environment variables
            os.environ['LANGFUSE_PUBLIC_KEY'] = public_key
            os.environ['LANGFUSE_SECRET_KEY'] = secret_key

            # Initialize the Langfuse client using the credentials
            langfuse = Langfuse()
            ```
        """
        self.enabled = enabled
        public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
        sample_rate = (
            sample_rate
            if sample_rate
            is not None  # needs explicit None check, as 0 is a valid value
            else float(os.environ.get("LANGFUSE_SAMPLE_RATE", 1.0))
        )

        if sample_rate is not None and (
            sample_rate > 1 or sample_rate < 0
        ):  # default value 1 will be set in the taskmanager
            self.enabled = False
            self.log.warning(
                "Langfuse client is disabled since the sample rate provided is not between 0 and 1."
            )

        threads = threads or int(os.environ.get("LANGFUSE_THREADS", 1))
        flush_at = flush_at or int(os.environ.get("LANGFUSE_FLUSH_AT", 15))
        flush_interval = flush_interval or float(
            os.environ.get("LANGFUSE_FLUSH_INTERVAL", 0.5)
        )

        max_retries = max_retries or int(os.environ.get("LANGFUSE_MAX_RETRIES", 3))
        timeout = timeout or int(os.environ.get("LANGFUSE_TIMEOUT", 20))

        if not self.enabled:
            self.log.warning(
                "Langfuse client is disabled. No observability data will be sent."
            )

        elif not public_key:
            self.enabled = False
            self.log.warning(
                "Langfuse client is disabled since no public_key was provided as a parameter or environment variable 'LANGFUSE_PUBLIC_KEY'. See our docs: https://langfuse.com/docs/sdk/python/low-level-sdk#initialize-client"
            )

        elif not secret_key:
            self.enabled = False
            self.log.warning(
                "Langfuse client is disabled since no secret_key was provided as a parameter or environment variable 'LANGFUSE_SECRET_KEY'. See our docs: https://langfuse.com/docs/sdk/python/low-level-sdk#initialize-client"
            )

        set_debug = debug if debug else (os.getenv("LANGFUSE_DEBUG", "False") == "True")

        if set_debug is True:
            # Ensures that debug level messages are logged when debug mode is on.
            # Otherwise, defaults to WARNING level.
            # See https://docs.python.org/3/howto/logging.html#what-happens-if-no-configuration-is-provided
            logging.basicConfig()
            # Set level for all loggers under langfuse package
            logging.getLogger("langfuse").setLevel(logging.DEBUG)

            clean_logger()
        else:
            logging.getLogger("langfuse").setLevel(logging.WARNING)
            clean_logger()

        self.base_url = (
            host
            if host
            else os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        )

        self.httpx_client = httpx_client or httpx.Client(timeout=timeout)

        self.client = FernLangfuse(
            base_url=self.base_url,
            username=public_key,
            password=secret_key,
            x_langfuse_sdk_name="python",
            x_langfuse_sdk_version=version,
            x_langfuse_public_key=public_key,
            httpx_client=self.httpx_client,
        )

        langfuse_client = LangfuseClient(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self.base_url,
            version=version,
            timeout=timeout,
            session=self.httpx_client,
        )

        args = {
            "threads": threads,
            "flush_at": flush_at,
            "flush_interval": flush_interval,
            "max_retries": max_retries,
            "client": langfuse_client,
            "api_client": self.client,
            "public_key": public_key,
            "sdk_name": "python",
            "sdk_version": version,
            "sdk_integration": sdk_integration,
            "enabled": self.enabled,
            "sample_rate": sample_rate,
            "mask": mask,
        }

        self.task_manager = TaskManager(**args)

        self.trace_id = None

        self.release = self._get_release_value(release)

        self.prompt_cache = PromptCache()

    def _get_release_value(self, release: Optional[str] = None) -> Optional[str]:
        if release:
            return release
        elif "LANGFUSE_RELEASE" in os.environ:
            return os.environ["LANGFUSE_RELEASE"]
        else:
            return get_common_release_envs()

    def get_trace_id(self) -> str:
        """Get the current trace id."""
        return self.trace_id

    def get_trace_url(self) -> str:
        """Get the URL of the current trace to view it in the Langfuse UI."""
        return f"{self.base_url}/trace/{self.trace_id}"

    def get_dataset(
        self, name: str, *, fetch_items_page_size: Optional[int] = 50
    ) -> "DatasetClient":
        """Fetch a dataset by its name.

        Args:
            name (str): The name of the dataset to fetch.
            fetch_items_page_size (Optional[int]): All items of the dataset will be fetched in chunks of this size. Defaults to 50.

        Returns:
            DatasetClient: The dataset with the given name.
        """
        try:
            self.log.debug(f"Getting datasets {name}")
            dataset = self.client.datasets.get(dataset_name=name)

            dataset_items = []
            page = 1
            while True:
                new_items = self.client.dataset_items.list(
                    dataset_name=self._url_encode(name),
                    page=page,
                    limit=fetch_items_page_size,
                )
                dataset_items.extend(new_items.data)
                if new_items.meta.total_pages <= page:
                    break
                page += 1

            items = [DatasetItemClient(i, langfuse=self) for i in dataset_items]

            return DatasetClient(dataset, items=items)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_dataset_item(self, id: str) -> "DatasetItemClient":
        """Get the dataset item with the given id."""
        try:
            self.log.debug(f"Getting dataset item {id}")
            dataset_item = self.client.dataset_items.get(id=id)
            return DatasetItemClient(dataset_item, langfuse=self)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def auth_check(self) -> bool:
        """Check if the provided credentials (public and secret key) are valid.

        Raises:
            Exception: If no projects were found for the provided credentials.

        Note:
            This method is blocking. It is discouraged to use it in production code.
        """
        try:
            projects = self.client.projects.get()
            self.log.debug(
                f"Auth check successful, found {len(projects.data)} projects"
            )
            if len(projects.data) == 0:
                raise Exception(
                    "Auth check failed, no project found for the keys provided."
                )
            return True

        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_dataset_runs(
        self,
        dataset_name: str,
        *,
        page: typing.Optional[int] = None,
        limit: typing.Optional[int] = None,
    ) -> PaginatedDatasetRuns:
        """Get all dataset runs.

        Args:
            dataset_name (str): Name of the dataset.
            page (Optional[int]): Page number of the dataset runs to return, starts at 1. Defaults to None.
            limit (Optional[int]): Maximum number of dataset runs to return. Defaults to 50.

        Returns:
            PaginatedDatasetRuns: The dataset runs.
        """
        try:
            self.log.debug("Getting dataset runs")
            return self.client.datasets.get_runs(
                dataset_name=self._url_encode(dataset_name), page=page, limit=limit
            )
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_dataset_run(
        self,
        dataset_name: str,
        dataset_run_name: str,
    ) -> DatasetRunWithItems:
        """Get a dataset run.

        Args:
            dataset_name: Name of the dataset.
            dataset_run_name: Name of the dataset run.

        Returns:
            DatasetRunWithItems: The dataset run.
        """
        try:
            self.log.debug(
                f"Getting dataset runs for dataset {dataset_name} and run {dataset_run_name}"
            )
            return self.client.datasets.get_run(
                dataset_name=self._url_encode(dataset_name),
                run_name=self._url_encode(dataset_run_name),
            )
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def create_dataset(
        self,
        name: str,
        description: Optional[str] = None,
        metadata: Optional[Any] = None,
    ) -> Dataset:
        """Create a dataset with the given name on Langfuse.

        Args:
            name: Name of the dataset to create.
            description: Description of the dataset. Defaults to None.
            metadata: Additional metadata. Defaults to None.

        Returns:
            Dataset: The created dataset as returned by the Langfuse API.
        """
        try:
            body = CreateDatasetRequest(
                name=name, description=description, metadata=metadata
            )
            self.log.debug(f"Creating datasets {body}")
            return self.client.datasets.create(request=body)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def create_dataset_item(
        self,
        dataset_name: str,
        input: Optional[Any] = None,
        expected_output: Optional[Any] = None,
        metadata: Optional[Any] = None,
        source_trace_id: Optional[str] = None,
        source_observation_id: Optional[str] = None,
        status: Optional[DatasetStatus] = None,
        id: Optional[str] = None,
    ) -> DatasetItem:
        """Create a dataset item.

        Upserts if an item with id already exists.

        Args:
            dataset_name: Name of the dataset in which the dataset item should be created.
            input: Input data. Defaults to None. Can contain any dict, list or scalar.
            expected_output: Expected output data. Defaults to None. Can contain any dict, list or scalar.
            metadata: Additional metadata. Defaults to None. Can contain any dict, list or scalar.
            source_trace_id: Id of the source trace. Defaults to None.
            source_observation_id: Id of the source observation. Defaults to None.
            status: Status of the dataset item. Defaults to ACTIVE for newly created items.
            id: Id of the dataset item. Defaults to None. Provide your own id if you want to dedupe dataset items. Id needs to be globally unique and cannot be reused across datasets.

        Returns:
            DatasetItem: The created dataset item as returned by the Langfuse API.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Uploading items to the Langfuse dataset named "capital_cities"
            langfuse.create_dataset_item(
                dataset_name="capital_cities",
                input={"input": {"country": "Italy"}},
                expected_output={"expected_output": "Rome"},
                metadata={"foo": "bar"}
            )
            ```
        """
        try:
            body = CreateDatasetItemRequest(
                datasetName=dataset_name,
                input=input,
                expectedOutput=expected_output,
                metadata=metadata,
                sourceTraceId=source_trace_id,
                sourceObservationId=source_observation_id,
                status=status,
                id=id,
            )
            self.log.debug(f"Creating dataset item {body}")
            return self.client.dataset_items.create(request=body)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def fetch_trace(
        self,
        id: str,
    ) -> FetchTraceResponse:
        """Fetch a trace via the Langfuse API by its id.

        Args:
            id: The id of the trace to fetch.

        Returns:
            FetchTraceResponse: The trace with full details as returned by the Langfuse API on `data`.

        Raises:
            Exception: If the trace with the given id could not be found within the authenticated project or if an error occurred during the request.
        """
        try:
            self.log.debug(f"Getting trace {id}")
            trace = self.client.trace.get(id)
            return FetchTraceResponse(data=trace)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_trace(
        self,
        id: str,
    ) -> TraceWithFullDetails:
        """Get a trace via the Langfuse API by its id. Deprecated, use fetch_trace instead.

        Args:
            id: The id of the trace to fetch.

        Returns:
            TraceWithFullDetails: The trace with full details as returned by the Langfuse API.

        Raises:
            Exception: If the trace with the given id could not be found within the authenticated project or if an error occurred during the request.
        """
        warnings.warn(
            "get_trace is deprecated, use fetch_trace instead.",
            DeprecationWarning,
        )

        try:
            self.log.debug(f"Getting trace {id}")
            return self.client.trace.get(id)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def fetch_traces(
        self,
        *,
        page: Optional[int] = None,
        limit: Optional[int] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        session_id: Optional[str] = None,
        from_timestamp: Optional[dt.datetime] = None,
        to_timestamp: Optional[dt.datetime] = None,
        order_by: Optional[str] = None,
        tags: Optional[Union[str, Sequence[str]]] = None,
    ) -> FetchTracesResponse:
        """Fetch a list of traces in the current project matching the given parameters.

        Args:
            page (Optional[int]): Page number, starts at 1. Defaults to None.
            limit (Optional[int]): Limit of items per page. If you encounter API issues due to too large page sizes, try to reduce the limit. Defaults to None.
            name (Optional[str]): Filter by name of traces. Defaults to None.
            user_id (Optional[str]): Filter by user_id. Defaults to None.
            session_id (Optional[str]): Filter by session_id. Defaults to None.
            from_timestamp (Optional[dt.datetime]): Retrieve only traces with a timestamp on or after this datetime. Defaults to None.
            to_timestamp (Optional[dt.datetime]): Retrieve only traces with a timestamp before this datetime. Defaults to None.
            order_by (Optional[str]): Format of the string `[field].[asc/desc]`. Fields: id, timestamp, name, userId, release, version, public, bookmarked, sessionId. Example: `timestamp.asc`. Defaults to None.
            tags (Optional[Union[str, Sequence[str]]]): Filter by tags. Defaults to None.

        Returns:
            FetchTracesResponse, list of traces on `data` and metadata on `meta`.

        Raises:
            Exception: If an error occurred during the request.
        """
        try:
            self.log.debug(
                f"Getting traces... {page}, {limit}, {name}, {user_id}, {session_id}, {from_timestamp}, {to_timestamp}, {order_by}, {tags}"
            )
            res = self.client.trace.list(
                page=page,
                limit=limit,
                name=name,
                user_id=user_id,
                session_id=session_id,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                order_by=order_by,
                tags=tags,
            )
            return FetchTracesResponse(data=res.data, meta=res.meta)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_traces(
        self,
        *,
        page: Optional[int] = None,
        limit: Optional[int] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        session_id: Optional[str] = None,
        from_timestamp: Optional[dt.datetime] = None,
        to_timestamp: Optional[dt.datetime] = None,
        order_by: Optional[str] = None,
        tags: Optional[Union[str, Sequence[str]]] = None,
    ) -> Traces:
        """Get a list of traces in the current project matching the given parameters. Deprecated, use fetch_traces instead.

        Args:
            page (Optional[int]): Page number, starts at 1. Defaults to None.
            limit (Optional[int]): Limit of items per page. If you encounter API issues due to too large page sizes, try to reduce the limit. Defaults to None.
            name (Optional[str]): Filter by name of traces. Defaults to None.
            user_id (Optional[str]): Filter by user_id. Defaults to None.
            session_id (Optional[str]): Filter by session_id. Defaults to None.
            from_timestamp (Optional[dt.datetime]): Retrieve only traces with a timestamp on or after this datetime. Defaults to None.
            to_timestamp (Optional[dt.datetime]): Retrieve only traces with a timestamp before this datetime. Defaults to None.
            order_by (Optional[str]): Format of the string `[field].[asc/desc]`. Fields: id, timestamp, name, userId, release, version, public, bookmarked, sessionId. Example: `timestamp.asc`. Defaults to None.
            tags (Optional[Union[str, Sequence[str]]]): Filter by tags. Defaults to None.

        Returns:
            List of Traces

        Raises:
            Exception: If an error occurred during the request.
        """
        warnings.warn(
            "get_traces is deprecated, use fetch_traces instead.",
            DeprecationWarning,
        )
        try:
            self.log.debug(
                f"Getting traces... {page}, {limit}, {name}, {user_id}, {session_id}, {from_timestamp}, {to_timestamp}, {order_by}, {tags}"
            )
            return self.client.trace.list(
                page=page,
                limit=limit,
                name=name,
                user_id=user_id,
                session_id=session_id,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                order_by=order_by,
                tags=tags,
            )
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def fetch_observations(
        self,
        *,
        page: typing.Optional[int] = None,
        limit: typing.Optional[int] = None,
        name: typing.Optional[str] = None,
        user_id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        parent_observation_id: typing.Optional[str] = None,
        from_start_time: typing.Optional[dt.datetime] = None,
        to_start_time: typing.Optional[dt.datetime] = None,
        type: typing.Optional[str] = None,
    ) -> FetchObservationsResponse:
        """Get a list of observations in the current project matching the given parameters.

        Args:
            page (Optional[int]): Page number of the observations to return. Defaults to None.
            limit (Optional[int]): Maximum number of observations to return. Defaults to None.
            name (Optional[str]): Name of the observations to return. Defaults to None.
            user_id (Optional[str]): User identifier. Defaults to None.
            trace_id (Optional[str]): Trace identifier. Defaults to None.
            parent_observation_id (Optional[str]): Parent observation identifier. Defaults to None.
            from_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time on or after this datetime. Defaults to None.
            to_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time before this datetime. Defaults to None.
            type (Optional[str]): Type of the observation. Defaults to None.

        Returns:
            FetchObservationsResponse, list of observations on `data` and metadata on `meta`.

        Raises:
            Exception: If an error occurred during the request.
        """
        try:
            self.log.debug(
                f"Getting observations... {page}, {limit}, {name}, {user_id}, {trace_id}, {parent_observation_id}, {from_start_time}, {to_start_time}, {type}"
            )
            res = self.client.observations.get_many(
                page=page,
                limit=limit,
                name=name,
                user_id=user_id,
                trace_id=trace_id,
                parent_observation_id=parent_observation_id,
                from_start_time=from_start_time,
                to_start_time=to_start_time,
                type=type,
            )
            return FetchObservationsResponse(data=res.data, meta=res.meta)
        except Exception as e:
            self.log.exception(e)
            raise e

    def get_observations(
        self,
        *,
        page: typing.Optional[int] = None,
        limit: typing.Optional[int] = None,
        name: typing.Optional[str] = None,
        user_id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        parent_observation_id: typing.Optional[str] = None,
        from_start_time: typing.Optional[dt.datetime] = None,
        to_start_time: typing.Optional[dt.datetime] = None,
        type: typing.Optional[str] = None,
    ) -> ObservationsViews:
        """Get a list of observations in the current project matching the given parameters. Deprecated, use fetch_observations instead.

        Args:
            page (Optional[int]): Page number of the observations to return. Defaults to None.
            limit (Optional[int]): Maximum number of observations to return. Defaults to None.
            name (Optional[str]): Name of the observations to return. Defaults to None.
            user_id (Optional[str]): User identifier. Defaults to None.
            trace_id (Optional[str]): Trace identifier. Defaults to None.
            parent_observation_id (Optional[str]): Parent observation identifier. Defaults to None.
            from_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time on or after this datetime. Defaults to None.
            to_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time before this datetime. Defaults to None.
            type (Optional[str]): Type of the observation. Defaults to None.

        Returns:
            List of ObservationsViews: List of observations in the project matching the given parameters.

        Raises:
            Exception: If an error occurred during the request.
        """
        warnings.warn(
            "get_observations is deprecated, use fetch_observations instead.",
            DeprecationWarning,
        )
        try:
            self.log.debug(
                f"Getting observations... {page}, {limit}, {name}, {user_id}, {trace_id}, {parent_observation_id}, {from_start_time}, {to_start_time}, {type}"
            )
            return self.client.observations.get_many(
                page=page,
                limit=limit,
                name=name,
                user_id=user_id,
                trace_id=trace_id,
                parent_observation_id=parent_observation_id,
                from_start_time=from_start_time,
                to_start_time=to_start_time,
                type=type,
            )
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_generations(
        self,
        *,
        page: typing.Optional[int] = None,
        limit: typing.Optional[int] = None,
        name: typing.Optional[str] = None,
        user_id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        from_start_time: typing.Optional[dt.datetime] = None,
        to_start_time: typing.Optional[dt.datetime] = None,
        parent_observation_id: typing.Optional[str] = None,
    ) -> ObservationsViews:
        """Get a list of generations in the current project matching the given parameters. Deprecated, use fetch_observations(type='GENERATION') instead.

        Args:
            page (Optional[int]): Page number of the generations to return. Defaults to None.
            limit (Optional[int]): Maximum number of generations to return. Defaults to None.
            name (Optional[str]): Name of the generations to return. Defaults to None.
            user_id (Optional[str]): User identifier of the generations to return. Defaults to None.
            trace_id (Optional[str]): Trace identifier of the generations to return. Defaults to None.
            from_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time on or after this datetime. Defaults to None.
            to_start_time (Optional[dt.datetime]): Retrieve only observations with a start_time before this datetime. Defaults to None.
            parent_observation_id (Optional[str]): Parent observation identifier of the generations to return. Defaults to None.

        Returns:
            List of ObservationsViews: List of generations in the project matching the given parameters.

        Raises:
            Exception: If an error occurred during the request.
        """
        warnings.warn(
            "get_generations is deprecated, use `fetch_observations(type='GENERATION')` instead.",
            DeprecationWarning,
        )
        return self.get_observations(
            page=page,
            limit=limit,
            name=name,
            user_id=user_id,
            trace_id=trace_id,
            parent_observation_id=parent_observation_id,
            from_start_time=from_start_time,
            to_start_time=to_start_time,
            type="GENERATION",
        )

    def fetch_observation(
        self,
        id: str,
    ) -> FetchObservationResponse:
        """Get an observation in the current project with the given identifier.

        Args:
            id: The identifier of the observation to fetch.

        Returns:
            FetchObservationResponse: The observation with the given id on `data`.

        Raises:
            Exception: If the observation with the given id could not be found within the authenticated project or if an error occurred during the request.
        """
        try:
            self.log.debug(f"Getting observation {id}")
            observation = self.client.observations.get(id)
            return FetchObservationResponse(data=observation)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def get_observation(
        self,
        id: str,
    ) -> Observation:
        """Get an observation in the current project with the given identifier. Deprecated, use fetch_observation instead.

        Args:
            id: The identifier of the observation to fetch.

        Raises:
            Exception: If the observation with the given id could not be found within the authenticated project or if an error occurred during the request.
        """
        warnings.warn(
            "get_observation is deprecated, use fetch_observation instead.",
            DeprecationWarning,
        )
        try:
            self.log.debug(f"Getting observation {id}")
            return self.client.observations.get(id)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    def fetch_sessions(
        self,
        *,
        page: typing.Optional[int] = None,
        limit: typing.Optional[int] = None,
        from_timestamp: typing.Optional[dt.datetime] = None,
        to_timestamp: typing.Optional[dt.datetime] = None,
    ) -> FetchSessionsResponse:
        """Get a list of sessions in the current project.

        Args:
            page (Optional[int]): Page number of the sessions to return. Defaults to None.
            limit (Optional[int]): Maximum number of sessions to return. Defaults to None.
            from_timestamp (Optional[dt.datetime]): Retrieve only sessions with a timestamp on or after this datetime. Defaults to None.
            to_timestamp (Optional[dt.datetime]): Retrieve only sessions with a timestamp before this datetime. Defaults to None.

        Returns:
            FetchSessionsResponse, list of sessions on `data` and metadata on `meta`.

        Raises:
            Exception: If an error occurred during the request.
        """
        try:
            self.log.debug(
                f"Getting sessions... {page}, {limit}, {from_timestamp}, {to_timestamp}"
            )
            res = self.client.sessions.list(
                page=page,
                limit=limit,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
            )
            return FetchSessionsResponse(data=res.data, meta=res.meta)
        except Exception as e:
            handle_fern_exception(e)
            raise e

    @overload
    def get_prompt(
        self,
        name: str,
        version: Optional[int] = None,
        *,
        label: Optional[str] = None,
        type: Literal["chat"],
        cache_ttl_seconds: Optional[int] = None,
        fallback: Optional[List[ChatMessageDict]] = None,
        max_retries: Optional[int] = None,
        fetch_timeout_seconds: Optional[int] = None,
    ) -> ChatPromptClient: ...

    @overload
    def get_prompt(
        self,
        name: str,
        version: Optional[int] = None,
        *,
        label: Optional[str] = None,
        type: Literal["text"] = "text",
        cache_ttl_seconds: Optional[int] = None,
        fallback: Optional[str] = None,
        max_retries: Optional[int] = None,
        fetch_timeout_seconds: Optional[int] = None,
    ) -> TextPromptClient: ...

    def get_prompt(
        self,
        name: str,
        version: Optional[int] = None,
        *,
        label: Optional[str] = None,
        type: Literal["chat", "text"] = "text",
        cache_ttl_seconds: Optional[int] = None,
        fallback: Union[Optional[List[ChatMessageDict]], Optional[str]] = None,
        max_retries: Optional[int] = None,
        fetch_timeout_seconds: Optional[int] = None,
    ) -> PromptClient:
        """Get a prompt.

        This method attempts to fetch the requested prompt from the local cache. If the prompt is not found
        in the cache or if the cached prompt has expired, it will try to fetch the prompt from the server again
        and update the cache. If fetching the new prompt fails, and there is an expired prompt in the cache, it will
        return the expired prompt as a fallback.

        Args:
            name (str): The name of the prompt to retrieve.

        Keyword Args:
            version (Optional[int]): The version of the prompt to retrieve. If no label and version is specified, the `production` label is returned. Specify either version or label, not both.
            label: Optional[str]: The label of the prompt to retrieve. If no label and version is specified, the `production` label is returned. Specify either version or label, not both.
            cache_ttl_seconds: Optional[int]: Time-to-live in seconds for caching the prompt. Must be specified as a
            keyword argument. If not set, defaults to 60 seconds. Disables caching if set to 0.
            type: Literal["chat", "text"]: The type of the prompt to retrieve. Defaults to "text".
            fallback: Union[Optional[List[ChatMessageDict]], Optional[str]]: The prompt string to return if fetching the prompt fails. Important on the first call where no cached prompt is available. Follows Langfuse prompt formatting with double curly braces for variables. Defaults to None.
            max_retries: Optional[int]: The maximum number of retries in case of API/network errors. Defaults to 2. The maximum value is 4. Retries have an exponential backoff with a maximum delay of 10 seconds.
            fetch_timeout_seconds: Optional[int]: The timeout in milliseconds for fetching the prompt. Defaults to the default timeout set on the SDK, which is 10 seconds per default.

        Returns:
            The prompt object retrieved from the cache or directly fetched if not cached or expired of type
            - TextPromptClient, if type argument is 'text'.
            - ChatPromptClient, if type argument is 'chat'.

        Raises:
            Exception: Propagates any exceptions raised during the fetching of a new prompt, unless there is an
            expired prompt in the cache, in which case it logs a warning and returns the expired prompt.
        """
        if version is not None and label is not None:
            raise ValueError("Cannot specify both version and label at the same time.")

        if not name:
            raise ValueError("Prompt name cannot be empty.")

        cache_key = PromptCache.generate_cache_key(name, version=version, label=label)
        bounded_max_retries = self._get_bounded_max_retries(
            max_retries, default_max_retries=2, max_retries_upper_bound=4
        )

        self.log.debug(f"Getting prompt '{cache_key}'")
        cached_prompt = self.prompt_cache.get(cache_key)

        if cached_prompt is None or cache_ttl_seconds == 0:
            self.log.debug(
                f"Prompt '{cache_key}' not found in cache or caching disabled."
            )
            try:
                return self._fetch_prompt_and_update_cache(
                    name,
                    version=version,
                    label=label,
                    ttl_seconds=cache_ttl_seconds,
                    max_retries=bounded_max_retries,
                    fetch_timeout_seconds=fetch_timeout_seconds,
                )
            except Exception as e:
                if fallback:
                    self.log.warning(
                        f"Returning fallback prompt for '{cache_key}' due to fetch error: {e}"
                    )

                    fallback_client_args = {
                        "name": name,
                        "prompt": fallback,
                        "type": type,
                        "version": version or 0,
                        "config": {},
                        "labels": [label] if label else [],
                        "tags": [],
                    }

                    if type == "text":
                        return TextPromptClient(
                            prompt=Prompt_Text(**fallback_client_args),
                            is_fallback=True,
                        )

                    if type == "chat":
                        return ChatPromptClient(
                            prompt=Prompt_Chat(**fallback_client_args),
                            is_fallback=True,
                        )

                raise e

        if cached_prompt.is_expired():
            self.log.debug(f"Stale prompt '{cache_key}' found in cache.")
            try:
                # refresh prompt in background thread, refresh_prompt deduplicates tasks
                self.log.debug(f"Refreshing prompt '{cache_key}' in background.")
                self.prompt_cache.add_refresh_prompt_task(
                    cache_key,
                    lambda: self._fetch_prompt_and_update_cache(
                        name,
                        version=version,
                        label=label,
                        ttl_seconds=cache_ttl_seconds,
                        max_retries=bounded_max_retries,
                        fetch_timeout_seconds=fetch_timeout_seconds,
                    ),
                )
                self.log.debug(f"Returning stale prompt '{cache_key}' from cache.")
                # return stale prompt
                return cached_prompt.value

            except Exception as e:
                self.log.warning(
                    f"Error when refreshing cached prompt '{cache_key}', returning cached version. Error: {e}"
                )
                # creation of refresh prompt task failed, return stale prompt
                return cached_prompt.value

        return cached_prompt.value

    def _fetch_prompt_and_update_cache(
        self,
        name: str,
        *,
        version: Optional[int] = None,
        label: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        max_retries: int,
        fetch_timeout_seconds,
    ) -> PromptClient:
        try:
            cache_key = PromptCache.generate_cache_key(
                name, version=version, label=label
            )

            self.log.debug(f"Fetching prompt '{cache_key}' from server...")

            @backoff.on_exception(
                backoff.constant, Exception, max_tries=max_retries, logger=None
            )
            def fetch_prompts():
                return self.client.prompts.get(
                    self._url_encode(name),
                    version=version,
                    label=label,
                    request_options={
                        "timeout_in_seconds": fetch_timeout_seconds,
                    }
                    if fetch_timeout_seconds is not None
                    else None,
                )

            prompt_response = fetch_prompts()

            if prompt_response.type == "chat":
                prompt = ChatPromptClient(prompt_response)
            else:
                prompt = TextPromptClient(prompt_response)

            self.prompt_cache.set(cache_key, prompt, ttl_seconds)

            return prompt

        except Exception as e:
            self.log.error(f"Error while fetching prompt '{cache_key}': {str(e)}")
            raise e

    def _get_bounded_max_retries(
        self,
        max_retries: Optional[int],
        *,
        default_max_retries: int = 2,
        max_retries_upper_bound: int = 4,
    ) -> int:
        if max_retries is None:
            return default_max_retries

        bounded_max_retries = min(
            max(max_retries, 0),
            max_retries_upper_bound,
        )

        return bounded_max_retries

    @overload
    def create_prompt(
        self,
        *,
        name: str,
        prompt: List[ChatMessageDict],
        is_active: Optional[bool] = None,  # deprecated
        labels: List[str] = [],
        tags: Optional[List[str]] = None,
        type: Optional[Literal["chat"]],
        config: Optional[Any] = None,
    ) -> ChatPromptClient: ...

    @overload
    def create_prompt(
        self,
        *,
        name: str,
        prompt: str,
        is_active: Optional[bool] = None,  # deprecated
        labels: List[str] = [],
        tags: Optional[List[str]] = None,
        type: Optional[Literal["text"]] = "text",
        config: Optional[Any] = None,
    ) -> TextPromptClient: ...

    def create_prompt(
        self,
        *,
        name: str,
        prompt: Union[str, List[ChatMessageDict]],
        is_active: Optional[bool] = None,  # deprecated
        labels: List[str] = [],
        tags: Optional[List[str]] = None,
        type: Optional[Literal["chat", "text"]] = "text",
        config: Optional[Any] = None,
    ) -> PromptClient:
        """Create a new prompt in Langfuse.

        Keyword Args:
            name : The name of the prompt to be created.
            prompt : The content of the prompt to be created.
            is_active [DEPRECATED] : A flag indicating whether the prompt is active or not. This is deprecated and will be removed in a future release. Please use the 'production' label instead.
            labels: The labels of the prompt. Defaults to None. To create a default-served prompt, add the 'production' label.
            tags: The tags of the prompt. Defaults to None. Will be applied to all versions of the prompt.
            config: Additional structured data to be saved with the prompt. Defaults to None.
            type: The type of the prompt to be created. "chat" vs. "text". Defaults to "text".

        Returns:
            TextPromptClient: The prompt if type argument is 'text'.
            ChatPromptClient: The prompt if type argument is 'chat'.
        """
        try:
            self.log.debug(f"Creating prompt {name=}, {version=}, {labels=}")

            # Handle deprecated is_active flag
            if is_active:
                self.log.warning(
                    "The 'is_active' flag is deprecated and will be removed in a future release. Please use the 'production' label instead."
                )

                labels = labels if "production" in labels else labels + ["production"]

            if type == "chat":
                if not isinstance(prompt, list):
                    raise ValueError(
                        "For 'chat' type, 'prompt' must be a list of chat messages with role and content attributes."
                    )
                request = CreatePromptRequest_Chat(
                    name=name,
                    prompt=prompt,
                    labels=labels,
                    tags=tags,
                    config=config or {},
                    type="chat",
                )
                server_prompt = self.client.prompts.create(request=request)

                return ChatPromptClient(prompt=server_prompt)

            if not isinstance(prompt, str):
                raise ValueError("For 'text' type, 'prompt' must be a string.")

            request = CreatePromptRequest_Text(
                name=name,
                prompt=prompt,
                labels=labels,
                tags=tags,
                config=config or {},
                type="text",
            )

            server_prompt = self.client.prompts.create(request=request)
            return TextPromptClient(prompt=server_prompt)

        except Exception as e:
            handle_fern_exception(e)
            raise e

    def _url_encode(self, url: str) -> str:
        return urllib.parse.quote(url)

    def trace(
        self,
        *,
        id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        user_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        metadata: typing.Optional[typing.Any] = None,
        tags: typing.Optional[typing.List[str]] = None,
        timestamp: typing.Optional[dt.datetime] = None,
        public: typing.Optional[bool] = None,
        **kwargs,
    ) -> "StatefulTraceClient":
        """Create a trace.

        Args:
            id: The id of the trace can be set, defaults to a random id. Set it to link traces to external systems or when creating a distributed trace. Traces are upserted on id.
            name: Identifier of the trace. Useful for sorting/filtering in the UI.
            input: The input of the trace. Can be any JSON object.
            output: The output of the trace. Can be any JSON object.
            metadata: Additional metadata of the trace. Can be any JSON object. Metadata is merged when being updated via the API.
            user_id: The id of the user that triggered the execution. Used to provide user-level analytics.
            session_id: Used to group multiple traces into a session in Langfuse. Use your own session/thread identifier.
            version: The version of the trace type. Used to understand how changes to the trace type affect metrics. Useful in debugging.
            release: The release identifier of the current deployment. Used to understand how changes of different deployments affect metrics. Useful in debugging.
            tags: Tags are used to categorize or label traces. Traces can be filtered by tags in the UI and GET API. Tags can also be changed in the UI. Tags are merged and never deleted via the API.
            timestamp: The timestamp of the trace. Defaults to the current time if not provided.
            public: You can make a trace `public` to share it via a public link. This allows others to view the trace without needing to log in or be members of your Langfuse project.
            **kwargs: Additional keyword arguments that can be included in the trace.

        Returns:
            StatefulTraceClient: The created trace.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            trace = langfuse.trace(
                name="example-application",
                user_id="user-1234")
            )
            ```
        """
        new_id = id or str(uuid.uuid4())
        self.trace_id = new_id
        try:
            new_dict = {
                "id": new_id,
                "name": name,
                "userId": user_id,
                "sessionId": session_id
                or kwargs.get("sessionId", None),  # backward compatibility
                "release": self.release,
                "version": version,
                "metadata": metadata,
                "input": input,
                "output": output,
                "tags": tags,
                "timestamp": timestamp or _get_timestamp(),
                "public": public,
            }
            if kwargs is not None:
                new_dict.update(kwargs)

            new_body = TraceBody(**new_dict)

            self.log.debug(f"Creating trace {_filter_io_from_event_body(new_dict)}")
            event = {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "body": new_body,
            }

            self.task_manager.add_task(
                event,
            )

        except Exception as e:
            self.log.exception(e)
        finally:
            self._log_memory_usage()

            return StatefulTraceClient(
                self.client, new_id, StateType.TRACE, new_id, self.task_manager
            )

    def _log_memory_usage(self):
        try:
            is_malloc_tracing_enabled = bool(int(os.getenv("PYTHONTRACEMALLOC", 0)))
            report_interval = int(os.getenv("LANGFUSE_DEBUG_MEMORY_REPORT_INTERVAL", 0))
            top_k_items = int(os.getenv("LANGFUSE_DEBUG_MEMORY_TOP_K", 10))

            if (
                not is_malloc_tracing_enabled
                or report_interval <= 0
                or round(time.monotonic()) % report_interval != 0
            ):
                return

            snapshot = tracemalloc.take_snapshot().statistics("lineno")

            total_memory_usage = sum([stat.size for stat in snapshot]) / 1024 / 1024
            memory_usage_total_items = [f"{stat}" for stat in snapshot]
            memory_usage_langfuse_items = [
                stat for stat in memory_usage_total_items if "/langfuse/" in stat
            ]

            logged_memory_usage = {
                "all_files": [f"{stat}" for stat in memory_usage_total_items][
                    :top_k_items
                ],
                "langfuse_files": [f"{stat}" for stat in memory_usage_langfuse_items][
                    :top_k_items
                ],
                "total_usage": f"{total_memory_usage:.2f} MB",
                "langfuse_queue_length": self.task_manager._ingestion_queue.qsize(),
            }

            self.log.debug("Memory usage: ", logged_memory_usage)

            event = SdkLogBody(log=logged_memory_usage)
            self.task_manager.add_task(
                {
                    "id": str(uuid.uuid4()),
                    "type": "sdk-log",
                    "timestamp": _get_timestamp(),
                    "body": event.dict(),
                }
            )

        except Exception as e:
            self.log.exception(e)

    @overload
    def score(
        self,
        *,
        name: str,
        value: float,
        data_type: typing.Optional[Literal["NUMERIC", "BOOLEAN"]] = None,
        trace_id: typing.Optional[str] = None,
        id: typing.Optional[str] = None,
        comment: typing.Optional[str] = None,
        observation_id: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient": ...

    @overload
    def score(
        self,
        *,
        name: str,
        value: str,
        data_type: typing.Optional[Literal["CATEGORICAL"]] = "CATEGORICAL",
        trace_id: typing.Optional[str] = None,
        id: typing.Optional[str] = None,
        comment: typing.Optional[str] = None,
        observation_id: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient": ...

    def score(
        self,
        *,
        name: str,
        value: typing.Union[float, str],
        data_type: typing.Optional[ScoreDataType] = None,
        trace_id: typing.Optional[str] = None,
        id: typing.Optional[str] = None,
        comment: typing.Optional[str] = None,
        observation_id: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient":
        """Create a score attached to a trace (and optionally an observation).

        Args:
            name (str): Identifier of the score.
            value (Union[float, str]): The value of the score. Should be passed as float for numeric and boolean scores and as string for categorical scores.
            data_type (Optional[ScoreDataType]): The data type of the score. When not set, the data type is inferred from the score config's data type, when present.
              When no config is set, the data type is inferred from the value's type, i.e. float values are categorized as numeric scores and string values as categorical scores.
            trace_id (str): The id of the trace to which the score should be attached.
            id (Optional[str]): The id of the score. If not provided, a new UUID is generated.
            comment (Optional[str]): Additional context/explanation of the score.
            observation_id (Optional[str]): The id of the observation to which the score should be attached.
            config_id (Optional[str]): The id of the score config. When set, the score value is validated against the config. Defaults to None.
            **kwargs: Additional keyword arguments to include in the score.

        Returns:
            StatefulClient: Either the associated observation (if observation_id is provided) or the trace (if observation_id is not provided).

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name="example-application")

            # Get id of created trace
            trace_id = trace.id

            # Add score to the trace
            trace = langfuse.score(
                trace_id=trace_id,
                name="user-explicit-feedback",
                value=0.9,
                comment="I like how personalized the response is"
            )
            ```
        """
        trace_id = trace_id or self.trace_id or str(uuid.uuid4())
        new_id = id or str(uuid.uuid4())
        try:
            new_dict = {
                "id": new_id,
                "trace_id": trace_id,
                "observation_id": observation_id,
                "name": name,
                "value": value,
                "data_type": data_type,
                "comment": comment,
                "config_id": config_id,
                **kwargs,
            }

            self.log.debug(f"Creating score {new_dict}...")
            new_body = ScoreBody(**new_dict)

            event = {
                "id": str(uuid.uuid4()),
                "type": "score-create",
                "body": new_body,
            }
            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            if observation_id is not None:
                return StatefulClient(
                    self.client,
                    observation_id,
                    StateType.OBSERVATION,
                    trace_id,
                    self.task_manager,
                )
            else:
                return StatefulClient(
                    self.client, new_id, StateType.TRACE, new_id, self.task_manager
                )

    def span(
        self,
        *,
        id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        parent_observation_id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulSpanClient":
        """Create a span.

        A span represents durations of units of work in a trace.
        Usually, you want to add a span nested within a trace. Optionally you can nest it within another observation by providing a parent_observation_id.

        If no trace_id is provided, a new trace is created just for this span.

        Args:
            id (Optional[str]): The id of the span can be set, otherwise a random id is generated. Spans are upserted on id.
            trace_id (Optional[str]): The trace ID associated with this span. If not provided, a new UUID is generated.
            parent_observation_id (Optional[str]): The ID of the parent observation, if applicable.
            name (Optional[str]): Identifier of the span. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the span started, defaults to the current time.
            end_time (Optional[datetime]): The time at which the span ended. Automatically set by `span.end()`.
            metadata (Optional[dict]): Additional metadata of the span. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the span. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the span. Additional field for context of the event. E.g. the error message of an error event.
            input (Optional[dict]): The input to the span. Can be any JSON object.
            output (Optional[dict]): The output to the span. Can be any JSON object.
            version (Optional[str]): The version of the span type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the span.

        Returns:
            StatefulSpanClient: The created span.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            trace = langfuse.trace(name = "llm-feature")

            # Create a span
            retrieval = langfuse.span(name = "retrieval", trace_id = trace.id)

            # Create a nested span
            nested_span = langfuse.span(name = "retrieval", trace_id = trace.id, parent_observation_id = retrieval.id)
            ```
        """
        new_span_id = id or str(uuid.uuid4())
        new_trace_id = trace_id or str(uuid.uuid4())
        self.trace_id = new_trace_id
        try:
            span_body = {
                "id": new_span_id,
                "trace_id": new_trace_id,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "parent_observation_id": parent_observation_id,
                "version": version,
                "end_time": end_time,
                "trace": {"release": self.release},
                **kwargs,
            }

            if trace_id is None:
                self._generate_trace(new_trace_id, name or new_trace_id)

            self.log.debug(f"Creating span {_filter_io_from_event_body(span_body)}...")

            span_body = CreateSpanBody(**span_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "body": span_body,
            }

            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            self._log_memory_usage()

            return StatefulSpanClient(
                self.client,
                new_span_id,
                StateType.OBSERVATION,
                new_trace_id,
                self.task_manager,
            )

    def event(
        self,
        *,
        id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        parent_observation_id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulSpanClient":
        """Create an event.

        An event represents a discrete event in a trace.
        Usually, you want to add a event nested within a trace. Optionally you can nest it within another observation by providing a parent_observation_id.

        If no trace_id is provided, a new trace is created just for this event.

        Args:
            id (Optional[str]): The id of the event can be set, otherwise a random id is generated.
            trace_id (Optional[str]): The trace ID associated with this event. If not provided, a new trace is created just for this event.
            parent_observation_id (Optional[str]): The ID of the parent observation, if applicable.
            name (Optional[str]): Identifier of the event. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the event started, defaults to the current time.
            metadata (Optional[Any]): Additional metadata of the event. Can be any JSON object. Metadata is merged when being updated via the API.
            input (Optional[Any]): The input to the event. Can be any JSON object.
            output (Optional[Any]): The output to the event. Can be any JSON object.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the event. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the event. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the event type. Used to understand how changes to the event type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the event.

        Returns:
            StatefulSpanClient: The created event.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            trace = langfuse.trace(name = "llm-feature")

            # Create an event
            retrieval = langfuse.event(name = "retrieval", trace_id = trace.id)
            ```
        """
        event_id = id or str(uuid.uuid4())
        new_trace_id = trace_id or str(uuid.uuid4())
        self.trace_id = new_trace_id
        try:
            event_body = {
                "id": event_id,
                "trace_id": new_trace_id,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "parent_observation_id": parent_observation_id,
                "version": version,
                "trace": {"release": self.release},
                **kwargs,
            }

            if trace_id is None:
                self._generate_trace(new_trace_id, name or new_trace_id)

            request = CreateEventBody(**event_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "event-create",
                "body": request,
            }

            self.log.debug(
                f"Creating event {_filter_io_from_event_body(event_body)} ..."
            )
            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulSpanClient(
                self.client,
                event_id,
                StateType.OBSERVATION,
                new_trace_id,
                self.task_manager,
            )

    def generation(
        self,
        *,
        id: typing.Optional[str] = None,
        trace_id: typing.Optional[str] = None,
        parent_observation_id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        completion_start_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        model: typing.Optional[str] = None,
        model_parameters: typing.Optional[typing.Dict[str, MapValue]] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        usage: typing.Optional[typing.Union[pydantic.BaseModel, ModelUsage]] = None,
        usage_details: typing.Optional[typing.Dict[str, int]] = None,
        cost_details: typing.Optional[typing.Dict[str, float]] = None,
        prompt: typing.Optional[PromptClient] = None,
        **kwargs,
    ) -> "StatefulGenerationClient":
        """Create a generation.

        A generation is a span that is used to log generations of AI models. They contain additional metadata about the model, the prompt/completion, the cost of executing the model and are specifically rendered in the langfuse UI.

        Usually, you want to add a generation nested within a trace. Optionally you can nest it within another observation by providing a parent_observation_id.

        If no trace_id is provided, a new trace is created just for this generation.

        Args:
            id (Optional[str]): The id of the generation can be set, defaults to random id.
            trace_id (Optional[str]): The trace ID associated with this generation. If not provided, a new trace is created
            parent_observation_id (Optional[str]): The ID of the parent observation, if applicable.
            name (Optional[str]): Identifier of the generation. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime.datetime]): The time at which the generation started, defaults to the current time.
            end_time (Optional[datetime.datetime]): The time at which the generation ended. Automatically set by `generation.end()`.
            completion_start_time (Optional[datetime.datetime]): The time at which the completion started (streaming). Set it to get latency analytics broken down into time until completion started and completion duration.
            metadata (Optional[dict]): Additional metadata of the generation. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[str]): The level of the generation. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the generation. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the generation type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            model (Optional[str]): The name of the model used for the generation.
            model_parameters (Optional[dict]): The parameters of the model used for the generation; can be any key-value pairs.
            input (Optional[dict]): The prompt used for the generation. Can be any string or JSON object.
            output (Optional[dict]): The completion generated by the model. Can be any string or JSON object.
            usage (Optional[dict]): [DEPRECATED, use usage_details and cost_details instead] The usage object supports the OpenAi structure with {`promptTokens`, `completionTokens`, `totalTokens`} and a more generic version {`input`, `output`, `total`, `unit`, `inputCost`, `outputCost`, `totalCost`} where unit can be of value `"TOKENS"`, `"CHARACTERS"`, `"MILLISECONDS"`, `"SECONDS"`, or `"IMAGES"`. Refer to the docs on how to [automatically infer](https://langfuse.com/docs/model-usage-and-cost) token usage and costs in Langfuse.
            usage_details (Optional[dict]): The usage details of the generation. Also accepts OpenAI usage details. Keys are the usage type (e.g. "input", "input_cached", "output") and values are integers representing the number of units used. For accurate cost calculations in Langfuse, ensure each usage type has a corresponding price configured in the Langfuse models table. Example: {"input": 500, "output": 150}.
            cost_details (Optional[dict]): The cost details of the generation. Keys are the usage type (e.g. "input", "input_cached", "output") and values are floats representing the cost in USD. Example: {"input": 0.0015, "output": 0.002}.
            prompt (Optional[PromptClient]): The Langfuse prompt object used for the generation.
            **kwargs: Additional keyword arguments to include in the generation.

        Returns:
            StatefulGenerationClient: The created generation.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a generation in Langfuse
            generation = langfuse.generation(
                name="summary-generation",
                model="gpt-3.5-turbo",
                model_parameters={"maxTokens": "1000", "temperature": "0.9"},
                input=[{"role": "system", "content": "You are a helpful assistant."},
                       {"role": "user", "content": "Please generate a summary of the following documents ..."}],
                metadata={"interface": "whatsapp"}
            )
            ```
        """
        new_trace_id = trace_id or str(uuid.uuid4())
        new_generation_id = id or str(uuid.uuid4())
        self.trace_id = new_trace_id
        try:
            generation_body = {
                "id": new_generation_id,
                "trace_id": new_trace_id,
                "release": self.release,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "parent_observation_id": parent_observation_id,
                "version": version,
                "end_time": end_time,
                "completion_start_time": completion_start_time,
                "model": model,
                "model_parameters": model_parameters,
                "usage": _convert_usage_input(usage) if usage is not None else None,
                "usage_details": usage_details
                and _extract_usage_details(usage_details),
                "cost_details": cost_details,
                "trace": {"release": self.release},
                **_create_prompt_context(prompt),
                **kwargs,
            }

            if trace_id is None:
                trace = {
                    "id": new_trace_id,
                    "release": self.release,
                    "name": name,
                }
                request = TraceBody(**trace)

                event = {
                    "id": str(uuid.uuid4()),
                    "type": "trace-create",
                    "body": request,
                }

                self.log.debug("Creating trace...")

                self.task_manager.add_task(event)

            self.log.debug(
                f"Creating generation max {_filter_io_from_event_body(generation_body)}..."
            )
            request = CreateGenerationBody(**generation_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "generation-create",
                "body": request,
            }

            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulGenerationClient(
                self.client,
                new_generation_id,
                StateType.OBSERVATION,
                new_trace_id,
                self.task_manager,
            )

    def _generate_trace(self, trace_id: str, name: str):
        trace_dict = {
            "id": trace_id,
            "release": self.release,
            "name": name,
        }

        trace_body = TraceBody(**trace_dict)

        event = {
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "body": trace_body,
        }

        self.log.debug(f"Creating trace {_filter_io_from_event_body(trace_dict)}...")
        self.task_manager.add_task(event)

    def join(self):
        """Blocks until all consumer Threads are terminated. The SKD calls this upon termination of the Python Interpreter.

        If called before flushing, consumers might terminate before sending all events to Langfuse API. This method is called at exit of the SKD, right before the Python interpreter closes.
        To guarantee all messages have been delivered, you still need to call flush().
        """
        try:
            return self.task_manager.join()
        except Exception as e:
            self.log.exception(e)

    def flush(self):
        """Flush the internal event queue to the Langfuse API. It blocks until the queue is empty. It should be called when the application shuts down.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Some operations with Langfuse

            # Flushing all events to end Langfuse cleanly
            langfuse.flush()
            ```
        """
        try:
            return self.task_manager.flush()
        except Exception as e:
            self.log.exception(e)

    def shutdown(self):
        """Initiate a graceful shutdown of the Langfuse SDK, ensuring all events are sent to Langfuse API and all consumer Threads are terminated.

        This function calls flush() and join() consecutively resulting in a complete shutdown of the SDK. On success of this function, no more events will be sent to Langfuse API.
        As the SDK calls join() already on shutdown, refer to flush() to ensure all events arive at the Langfuse API.
        """
        try:
            return self.task_manager.shutdown()
        except Exception as e:
            self.log.exception(e)


class StateType(Enum):
    """Enum to distinguish observation and trace states.

    Attributes:
        OBSERVATION (int): Observation state.
        TRACE (int): Trace state.
    """

    OBSERVATION = 1
    TRACE = 0


class StatefulClient(object):
    """Base class for handling stateful operations in the Langfuse system.

    This client is capable of creating different nested Langfuse objects like spans, generations, scores, and events,
    associating them with either an observation or a trace based on the specified state type.

    Attributes:
        client (FernLangfuse): Core interface for Langfuse API interactions.
        id (str): Unique identifier of the stateful client (either observation or trace).
        state_type (StateType): Enum indicating whether the client is an observation or a trace.
        trace_id (str): Id of the trace associated with the stateful client.
        task_manager (TaskManager): Manager handling asynchronous tasks for the client.
    """

    log = logging.getLogger("langfuse")

    def __init__(
        self,
        client: FernLangfuse,
        id: str,
        state_type: StateType,
        trace_id: str,
        task_manager: TaskManager,
    ):
        """Initialize the StatefulClient.

        Args:
            client (FernLangfuse): Core interface for Langfuse API interactions.
            id (str): Unique identifier of the stateful client (either observation or trace).
            state_type (StateType): Enum indicating whether the client is an observation or a trace.
            trace_id (str): Id of the trace associated with the stateful client.
            task_manager (TaskManager): Manager handling asynchronous tasks for the client.
        """
        self.client = client
        self.trace_id = trace_id
        self.id = id
        self.state_type = state_type
        self.task_manager = task_manager

    def _add_state_to_event(self, body: dict):
        if self.state_type == StateType.OBSERVATION:
            body["parent_observation_id"] = self.id
            body["trace_id"] = self.trace_id
        else:
            body["trace_id"] = self.id
        return body

    def _add_default_values(self, body: dict):
        if body.get("start_time") is None:
            body["start_time"] = _get_timestamp()
        return body

    def generation(
        self,
        *,
        id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        completion_start_time: typing.Optional[dt.datetime] = None,
        model: typing.Optional[str] = None,
        model_parameters: typing.Optional[typing.Dict[str, MapValue]] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        usage: typing.Optional[typing.Union[pydantic.BaseModel, ModelUsage]] = None,
        usage_details: typing.Optional[typing.Dict[str, int]] = None,
        cost_details: typing.Optional[typing.Dict[str, float]] = None,
        prompt: typing.Optional[PromptClient] = None,
        **kwargs,
    ) -> "StatefulGenerationClient":
        """Create a generation nested within the current observation or trace.

        A generation is a span that is used to log generations of AI models. They contain additional metadata about the model, the prompt/completion, the cost of executing the model and are specifically rendered in the langfuse UI.

        Args:
            id (Optional[str]): The id of the generation can be set, defaults to random id.
            name (Optional[str]): Identifier of the generation. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime.datetime]): The time at which the generation started, defaults to the current time.
            end_time (Optional[datetime.datetime]): The time at which the generation ended. Automatically set by `generation.end()`.
            completion_start_time (Optional[datetime.datetime]): The time at which the completion started (streaming). Set it to get latency analytics broken down into time until completion started and completion duration.
            metadata (Optional[dict]): Additional metadata of the generation. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[str]): The level of the generation. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the generation. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the generation type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            model (Optional[str]): The name of the model used for the generation.
            model_parameters (Optional[dict]): The parameters of the model used for the generation; can be any key-value pairs.
            input (Optional[dict]): The prompt used for the generation. Can be any string or JSON object.
            output (Optional[dict]): The completion generated by the model. Can be any string or JSON object.
            usage (Optional[dict]): [DEPRECATED, use usage_details and cost_details instead] The usage object supports the OpenAi structure with {`promptTokens`, `completionTokens`, `totalTokens`} and a more generic version {`input`, `output`, `total`, `unit`, `inputCost`, `outputCost`, `totalCost`} where unit can be of value `"TOKENS"`, `"CHARACTERS"`, `"MILLISECONDS"`, `"SECONDS"`, or `"IMAGES"`. Refer to the docs on how to [automatically infer](https://langfuse.com/docs/model-usage-and-cost) token usage and costs in Langfuse.
            usage_details (Optional[dict]): The usage details of the generation. Also accepts OpenAI usage details. Keys are the usage type (e.g. "input", "input_cached", "output") and values are integers representing the number of units used. For accurate cost calculations in Langfuse, ensure each usage type has a corresponding price configured in the Langfuse models table. Example: {"input": 500, "output": 150}.
            cost_details (Optional[dict]): The cost details of the generation. Keys are the usage type (e.g. "input", "input_cached", "output") and values are floats representing the cost in USD. Example: {"input": 0.0015, "output": 0.002}.
            prompt (Optional[PromptClient]): The Langfuse prompt object used for the generation.
            **kwargs: Additional keyword arguments to include in the generation.

        Returns:
            StatefulGenerationClient: The created generation. Use this client to update the generation or create additional nested observations.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a nested generation in Langfuse
            generation = trace.generation(
                name="summary-generation",
                model="gpt-3.5-turbo",
                model_parameters={"maxTokens": "1000", "temperature": "0.9"},
                input=[{"role": "system", "content": "You are a helpful assistant."},
                       {"role": "user", "content": "Please generate a summary of the following documents ..."}],
                metadata={"interface": "whatsapp"}
            )
            ```
        """
        generation_id = id or str(uuid.uuid4())
        try:
            generation_body = {
                "id": generation_id,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "level": level,
                "status_message": status_message,
                "version": version,
                "end_time": end_time,
                "completion_start_time": completion_start_time,
                "model": model,
                "model_parameters": model_parameters,
                "input": input,
                "output": output,
                "usage": _convert_usage_input(usage) if usage is not None else None,
                "usage_details": usage_details
                and _extract_usage_details(usage_details),
                "cost_details": cost_details,
                **_create_prompt_context(prompt),
                **kwargs,
            }

            generation_body = self._add_state_to_event(generation_body)
            new_body = self._add_default_values(generation_body)

            new_body = CreateGenerationBody(**new_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "generation-create",
                "body": new_body.dict(exclude_none=True, exclude_unset=False),
            }

            self.log.debug(
                f"Creating generation {_filter_io_from_event_body(generation_body)}..."
            )
            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulGenerationClient(
                self.client,
                generation_id,
                StateType.OBSERVATION,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def span(
        self,
        *,
        id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulSpanClient":
        """Create a span nested within the current observation or trace.

        A span represents durations of units of work in a trace.

        Args:
            id (Optional[str]): The id of the span can be set, otherwise a random id is generated. Spans are upserted on id.
            name (Optional[str]): Identifier of the span. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the span started, defaults to the current time.
            end_time (Optional[datetime]): The time at which the span ended. Automatically set by `span.end()`.
            metadata (Optional[dict]): Additional metadata of the span. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the span. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the span. Additional field for context of the event. E.g. the error message of an error event.
            input (Optional[dict]): The input to the span. Can be any JSON object.
            output (Optional[dict]): The output to the span. Can be any JSON object.
            version (Optional[str]): The version of the span type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the span.

        Returns:
            StatefulSpanClient: The created span. Use this client to update the span or create additional nested observations.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a span
            retrieval = langfuse.span(name = "retrieval")
            ```
        """
        span_id = id or str(uuid.uuid4())
        try:
            span_body = {
                "id": span_id,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "version": version,
                "end_time": end_time,
                **kwargs,
            }

            self.log.debug(f"Creating span {_filter_io_from_event_body(span_body)}...")

            new_dict = self._add_state_to_event(span_body)
            new_body = self._add_default_values(new_dict)

            event = CreateSpanBody(**new_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "body": event,
            }

            self.task_manager.add_task(event)
        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulSpanClient(
                self.client,
                span_id,
                StateType.OBSERVATION,
                self.trace_id,
                task_manager=self.task_manager,
            )

    @overload
    def score(
        self,
        *,
        id: typing.Optional[str] = None,
        name: str,
        value: float,
        data_type: typing.Optional[Literal["NUMERIC", "BOOLEAN"]] = None,
        comment: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient": ...

    @overload
    def score(
        self,
        *,
        id: typing.Optional[str] = None,
        name: str,
        value: str,
        data_type: typing.Optional[Literal["CATEGORICAL"]] = "CATEGORICAL",
        comment: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient": ...

    def score(
        self,
        *,
        id: typing.Optional[str] = None,
        name: str,
        value: typing.Union[float, str],
        data_type: typing.Optional[ScoreDataType] = None,
        comment: typing.Optional[str] = None,
        config_id: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient":
        """Create a score attached for the current observation or trace.

        Args:
            name (str): Identifier of the score.
            value (Union[float, str]): The value of the score. Should be passed as float for numeric and boolean scores and as string for categorical scores.
            data_type (Optional[ScoreDataType]): The data type of the score. When not set, the data type is inferred from the score config's data type, when present.
              When no config is set, the data type is inferred from the value's type, i.e. float values are categorized as numeric scores and string values as categorical scores.
            comment (Optional[str]): Additional context/explanation of the score.
            id (Optional[str]): The id of the score. If not provided, a new UUID is generated.
            config_id (Optional[str]): The id of the score config. When set, the score value is validated against the config. Defaults to None.
            **kwargs: Additional keyword arguments to include in the score.

        Returns:
            StatefulClient: The current observation or trace for which the score was created. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name="example-application")

            # Add score to the trace
            trace = trace.score(
                name="user-explicit-feedback",
                value=0.8,
                comment="I like how personalized the response is"
            )
            ```
        """
        score_id = id or str(uuid.uuid4())
        try:
            new_score = {
                "id": score_id,
                "trace_id": self.trace_id,
                "name": name,
                "value": value,
                "data_type": data_type,
                "comment": comment,
                "config_id": config_id,
                **kwargs,
            }

            self.log.debug(f"Creating score {new_score}...")

            new_dict = self._add_state_to_event(new_score)

            if self.state_type == StateType.OBSERVATION:
                new_dict["observationId"] = self.id

            request = ScoreBody(**new_dict)

            event = {
                "id": str(uuid.uuid4()),
                "type": "score-create",
                "body": request,
            }

            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulClient(
                self.client,
                self.id,
                self.state_type,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def event(
        self,
        *,
        id: typing.Optional[str] = None,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulClient":
        """Create an event nested within the current observation or trace.

        An event represents a discrete event in a trace.

        Args:
            id (Optional[str]): The id of the event can be set, otherwise a random id is generated.
            name (Optional[str]): Identifier of the event. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the event started, defaults to the current time.
            metadata (Optional[Any]): Additional metadata of the event. Can be any JSON object. Metadata is merged when being updated via the API.
            input (Optional[Any]): The input to the event. Can be any JSON object.
            output (Optional[Any]): The output to the event. Can be any JSON object.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the event. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the event. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the event type. Used to understand how changes to the event type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the event.

        Returns:
            StatefulSpanClient: The created event. Use this client to update the event or create additional nested observations.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create an event
            retrieval = trace.event(name = "retrieval")
            ```
        """
        event_id = id or str(uuid.uuid4())
        try:
            event_body = {
                "id": event_id,
                "name": name,
                "start_time": start_time or _get_timestamp(),
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "version": version,
                **kwargs,
            }

            new_dict = self._add_state_to_event(event_body)
            new_body = self._add_default_values(new_dict)

            request = CreateEventBody(**new_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "event-create",
                "body": request,
            }

            self.log.debug(
                f"Creating event {_filter_io_from_event_body(event_body)}..."
            )
            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulClient(
                self.client,
                event_id,
                StateType.OBSERVATION,
                self.trace_id,
                self.task_manager,
            )

    def get_trace_url(self):
        """Get the URL to see the current trace in the Langfuse UI."""
        return f"{self.client._client_wrapper._base_url}/trace/{self.trace_id}"


class StatefulGenerationClient(StatefulClient):
    """Class for handling stateful operations of generations in the Langfuse system. Inherits from StatefulClient.

    This client extends the capabilities of the StatefulClient to specifically handle generation,
    allowing for the creation, update, and termination of generation processes in Langfuse.

    Attributes:
        client (FernLangfuse): Core interface for Langfuse API interaction.
        id (str): Unique identifier of the generation.
        state_type (StateType): Type of the stateful entity (observation or trace).
        trace_id (str): Id of trace associated with the generation.
        task_manager (TaskManager): Manager for handling asynchronous tasks.
    """

    log = logging.getLogger("langfuse")

    def __init__(
        self,
        client: FernLangfuse,
        id: str,
        state_type: StateType,
        trace_id: str,
        task_manager: TaskManager,
    ):
        """Initialize the StatefulGenerationClient."""
        super().__init__(client, id, state_type, trace_id, task_manager)

    # WHEN CHANGING THIS METHOD, UPDATE END() FUNCTION ACCORDINGLY
    def update(
        self,
        *,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        completion_start_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        model: typing.Optional[str] = None,
        model_parameters: typing.Optional[typing.Dict[str, MapValue]] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        usage: typing.Optional[typing.Union[pydantic.BaseModel, ModelUsage]] = None,
        usage_details: typing.Optional[typing.Dict[str, int]] = None,
        cost_details: typing.Optional[typing.Dict[str, float]] = None,
        prompt: typing.Optional[PromptClient] = None,
        **kwargs,
    ) -> "StatefulGenerationClient":
        """Update the generation.

        Args:
            name (Optional[str]): Identifier of the generation. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime.datetime]): The time at which the generation started, defaults to the current time.
            end_time (Optional[datetime.datetime]): The time at which the generation ended. Automatically set by `generation.end()`.
            completion_start_time (Optional[datetime.datetime]): The time at which the completion started (streaming). Set it to get latency analytics broken down into time until completion started and completion duration.
            metadata (Optional[dict]): Additional metadata of the generation. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[str]): The level of the generation. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the generation. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the generation type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            model (Optional[str]): The name of the model used for the generation.
            model_parameters (Optional[dict]): The parameters of the model used for the generation; can be any key-value pairs.
            input (Optional[dict]): The prompt used for the generation. Can be any string or JSON object.
            output (Optional[dict]): The completion generated by the model. Can be any string or JSON object.
            usage (Optional[dict]): [DEPRECATED, use usage_details and cost_details instead] The usage object supports the OpenAi structure with {`promptTokens`, `completionTokens`, `totalTokens`} and a more generic version {`input`, `output`, `total`, `unit`, `inputCost`, `outputCost`, `totalCost`} where unit can be of value `"TOKENS"`, `"CHARACTERS"`, `"MILLISECONDS"`, `"SECONDS"`, or `"IMAGES"`. Refer to the docs on how to [automatically infer](https://langfuse.com/docs/model-usage-and-cost) token usage and costs in Langfuse.
            usage_details (Optional[dict]): The usage details of the generation. Also accepts OpenAI usage details. Keys are the usage type (e.g. "input", "input_cached", "output") and values are integers representing the number of units used. For accurate cost calculations in Langfuse, ensure each usage type has a corresponding price configured in the Langfuse models table. Example: {"input": 500, "output": 150}.
            cost_details (Optional[dict]): The cost details of the generation. Keys are the usage type (e.g. "input", "input_cached", "output") and values are floats representing the cost in USD. Example: {"input": 0.0015, "output": 0.002}.
            prompt (Optional[PromptClient]): The Langfuse prompt object used for the generation.
            **kwargs: Additional keyword arguments to include in the generation.

        Returns:
            StatefulGenerationClient: The updated generation. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a nested generation in Langfuse
            generation = trace.generation(name="summary-generation")

            # Update the generation
            generation = generation.update(metadata={"interface": "whatsapp"})
            ```
        """
        try:
            generation_body = {
                "id": self.id,
                "trace_id": self.trace_id,  # Included to avoid relying on the order of events sent to the API
                "name": name,
                "start_time": start_time,
                "metadata": metadata,
                "level": level,
                "status_message": status_message,
                "version": version,
                "end_time": end_time,
                "completion_start_time": completion_start_time,
                "model": model,
                "model_parameters": model_parameters,
                "input": input,
                "output": output,
                "usage": _convert_usage_input(usage) if usage is not None else None,
                "usage_details": usage_details
                and _extract_usage_details(usage_details),
                "cost_details": cost_details,
                **_create_prompt_context(prompt),
                **kwargs,
            }

            self.log.debug(
                f"Update generation {_filter_io_from_event_body(generation_body)}..."
            )

            request = UpdateGenerationBody(**generation_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "generation-update",
                "body": request.dict(exclude_none=True, exclude_unset=False),
            }

            self.log.debug(
                f"Update generation {_filter_io_from_event_body(generation_body)}..."
            )
            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulGenerationClient(
                self.client,
                self.id,
                StateType.OBSERVATION,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def end(
        self,
        *,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        completion_start_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        model: typing.Optional[str] = None,
        model_parameters: typing.Optional[typing.Dict[str, MapValue]] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        usage: typing.Optional[typing.Union[pydantic.BaseModel, ModelUsage]] = None,
        usage_details: typing.Optional[typing.Dict[str, int]] = None,
        cost_details: typing.Optional[typing.Dict[str, float]] = None,
        prompt: typing.Optional[PromptClient] = None,
        **kwargs,
    ) -> "StatefulGenerationClient":
        """End the generation, optionally updating its properties.

        Args:
            name (Optional[str]): Identifier of the generation. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime.datetime]): The time at which the generation started, defaults to the current time.
            end_time (Optional[datetime.datetime]): Automatically set to the current time. Can be overridden to set a custom end time.
            completion_start_time (Optional[datetime.datetime]): The time at which the completion started (streaming). Set it to get latency analytics broken down into time until completion started and completion duration.
            metadata (Optional[dict]): Additional metadata of the generation. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[str]): The level of the generation. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the generation. Additional field for context of the event. E.g. the error message of an error event.
            version (Optional[str]): The version of the generation type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            model (Optional[str]): The name of the model used for the generation.
            model_parameters (Optional[dict]): The parameters of the model used for the generation; can be any key-value pairs.
            input (Optional[dict]): The prompt used for the generation. Can be any string or JSON object.
            output (Optional[dict]): The completion generated by the model. Can be any string or JSON object.
            usage (Optional[dict]): [DEPRECATED, use usage_details and cost_details instead] The usage object supports the OpenAi structure with {`promptTokens`, `completionTokens`, `totalTokens`} and a more generic version {`input`, `output`, `total`, `unit`, `inputCost`, `outputCost`, `totalCost`} where unit can be of value `"TOKENS"`, `"CHARACTERS"`, `"MILLISECONDS"`, `"SECONDS"`, or `"IMAGES"`. Refer to the docs on how to [automatically infer](https://langfuse.com/docs/model-usage-and-cost) token usage and costs in Langfuse.
            usage_details (Optional[dict]): The usage details of the generation. Also accepts OpenAI usage details. Keys are the usage type (e.g. "input", "input_cached", "output") and values are integers representing the number of units used. For accurate cost calculations in Langfuse, ensure each usage type has a corresponding price configured in the Langfuse models table. Example: {"input": 500, "output": 150}.
            cost_details (Optional[dict]): The cost details of the generation. Keys are the usage type (e.g. "input", "input_cached", "output") and values are floats representing the cost in USD. Example: {"input": 0.0015, "output": 0.002}.
            prompt (Optional[PromptClient]): The Langfuse prompt object used for the generation.
            **kwargs: Additional keyword arguments to include in the generation.

        Returns:
            StatefulGenerationClient: The ended generation. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a nested generation in Langfuse
            generation = trace.generation(name="summary-generation")

            # End the generation and update its properties
            generation = generation.end(metadata={"interface": "whatsapp"})
            ```
        """
        return self.update(
            name=name,
            start_time=start_time,
            end_time=end_time or _get_timestamp(),
            metadata=metadata,
            level=level,
            status_message=status_message,
            version=version,
            completion_start_time=completion_start_time,
            model=model,
            model_parameters=model_parameters,
            input=input,
            output=output,
            usage=usage,
            usage_details=usage_details and _extract_usage_details(usage_details),
            cost_details=cost_details,
            prompt=prompt,
            **kwargs,
        )


class StatefulSpanClient(StatefulClient):
    """Class for handling stateful operations of spans in the Langfuse system. Inherits from StatefulClient.

    Attributes:
        client (FernLangfuse): Core interface for Langfuse API interaction.
        id (str): Unique identifier of the span.
        state_type (StateType): Type of the stateful entity (observation or trace).
        trace_id (str): Id of trace associated with the span.
        task_manager (TaskManager): Manager for handling asynchronous tasks.
    """

    log = logging.getLogger("langfuse")

    def __init__(
        self,
        client: FernLangfuse,
        id: str,
        state_type: StateType,
        trace_id: str,
        task_manager: TaskManager,
    ):
        """Initialize the StatefulSpanClient."""
        super().__init__(client, id, state_type, trace_id, task_manager)

    # WHEN CHANGING THIS METHOD, UPDATE END() FUNCTION ACCORDINGLY
    def update(
        self,
        *,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulSpanClient":
        """Update the span.

        Args:
            name (Optional[str]): Identifier of the span. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the span started, defaults to the current time.
            end_time (Optional[datetime]): The time at which the span ended. Automatically set by `span.end()`.
            metadata (Optional[dict]): Additional metadata of the span. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the span. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the span. Additional field for context of the event. E.g. the error message of an error event.
            input (Optional[dict]): The input to the span. Can be any JSON object.
            output (Optional[dict]): The output to the span. Can be any JSON object.
            version (Optional[str]): The version of the span type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the span.

        Returns:
            StatefulSpanClient: The updated span. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a nested span in Langfuse
            span = trace.span(name="retrieval")

            # Update the span
            span = span.update(metadata={"interface": "whatsapp"})
            ```
        """
        try:
            span_body = {
                "id": self.id,
                "trace_id": self.trace_id,  # Included to avoid relying on the order of events sent to the API
                "name": name,
                "start_time": start_time,
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "version": version,
                "end_time": end_time,
                **kwargs,
            }
            self.log.debug(f"Update span {_filter_io_from_event_body(span_body)}...")

            request = UpdateSpanBody(**span_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "span-update",
                "body": request,
            }

            self.task_manager.add_task(event)
        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulSpanClient(
                self.client,
                self.id,
                StateType.OBSERVATION,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def end(
        self,
        *,
        name: typing.Optional[str] = None,
        start_time: typing.Optional[dt.datetime] = None,
        end_time: typing.Optional[dt.datetime] = None,
        metadata: typing.Optional[typing.Any] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        level: typing.Optional[SpanLevel] = None,
        status_message: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        **kwargs,
    ) -> "StatefulSpanClient":
        """End the span, optionally updating its properties.

        Args:
            name (Optional[str]): Identifier of the span. Useful for sorting/filtering in the UI.
            start_time (Optional[datetime]): The time at which the span started, defaults to the current time.
            end_time (Optional[datetime]): The time at which the span ended. Automatically set by `span.end()`.
            metadata (Optional[dict]): Additional metadata of the span. Can be any JSON object. Metadata is merged when being updated via the API.
            level (Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]]): The level of the span. Can be `DEBUG`, `DEFAULT`, `WARNING` or `ERROR`. Used for sorting/filtering of traces with elevated error levels and for highlighting in the UI.
            status_message (Optional[str]): The status message of the span. Additional field for context of the event. E.g. the error message of an error event.
            input (Optional[dict]): The input to the span. Can be any JSON object.
            output (Optional[dict]): The output to the span. Can be any JSON object.
            version (Optional[str]): The version of the span type. Used to understand how changes to the span type affect metrics. Useful in debugging.
            **kwargs: Additional keyword arguments to include in the span.

        Returns:
            StatefulSpanClient: The updated span. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Create a nested span in Langfuse
            span = trace.span(name="retrieval")

            # End the span and update its properties
            span = span.end(metadata={"interface": "whatsapp"})
            ```
        """
        try:
            span_body = {
                "name": name,
                "start_time": start_time,
                "metadata": metadata,
                "input": input,
                "output": output,
                "level": level,
                "status_message": status_message,
                "version": version,
                "end_time": end_time or _get_timestamp(),
                **kwargs,
            }
            return self.update(**span_body)

        except Exception as e:
            self.log.warning(e)
        finally:
            return StatefulSpanClient(
                self.client,
                self.id,
                StateType.OBSERVATION,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def get_langchain_handler(self, update_parent: bool = False):
        """Get langchain callback handler associated with the current span.

        Args:
            update_parent (bool): If set to True, the parent observation will be updated with the outcome of the Langchain run.

        Returns:
            CallbackHandler: An instance of CallbackHandler linked to this StatefulSpanClient.
        """
        from langfuse.callback import CallbackHandler

        return CallbackHandler(
            stateful_client=self, update_stateful_client=update_parent
        )


class StatefulTraceClient(StatefulClient):
    """Class for handling stateful operations of traces in the Langfuse system. Inherits from StatefulClient.

    Attributes:
        client (FernLangfuse): Core interface for Langfuse API interaction.
        id (str): Unique identifier of the trace.
        state_type (StateType): Type of the stateful entity (observation or trace).
        trace_id (str): The trace ID associated with this client.
        task_manager (TaskManager): Manager for handling asynchronous tasks.
    """

    log = logging.getLogger("langfuse")

    def __init__(
        self,
        client: FernLangfuse,
        id: str,
        state_type: StateType,
        trace_id: str,
        task_manager: TaskManager,
    ):
        """Initialize the StatefulTraceClient."""
        super().__init__(client, id, state_type, trace_id, task_manager)
        self.task_manager = task_manager

    def update(
        self,
        *,
        name: typing.Optional[str] = None,
        user_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        version: typing.Optional[str] = None,
        release: typing.Optional[str] = None,
        input: typing.Optional[typing.Any] = None,
        output: typing.Optional[typing.Any] = None,
        metadata: typing.Optional[typing.Any] = None,
        tags: typing.Optional[typing.List[str]] = None,
        public: typing.Optional[bool] = None,
        **kwargs,
    ) -> "StatefulTraceClient":
        """Update the trace.

        Args:
            name: Identifier of the trace. Useful for sorting/filtering in the UI.
            input: The input of the trace. Can be any JSON object.
            output: The output of the trace. Can be any JSON object.
            metadata: Additional metadata of the trace. Can be any JSON object. Metadata is merged when being updated via the API.
            user_id: The id of the user that triggered the execution. Used to provide user-level analytics.
            session_id: Used to group multiple traces into a session in Langfuse. Use your own session/thread identifier.
            version: The version of the trace type. Used to understand how changes to the trace type affect metrics. Useful in debugging.
            release: The release identifier of the current deployment. Used to understand how changes of different deployments affect metrics. Useful in debugging.
            tags: Tags are used to categorize or label traces. Traces can be filtered by tags in the UI and GET API. Tags can also be changed in the UI. Tags are merged and never deleted via the API.
            public: You can make a trace `public` to share it via a public link. This allows others to view the trace without needing to log in or be members of your Langfuse project.
            **kwargs: Additional keyword arguments that can be included in the trace.

        Returns:
            StatefulTraceClient: The updated trace. Passthrough for chaining.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(
                name="example-application",
                user_id="user-1234")
            )

            # Update the trace
            trace = trace.update(
                output={"result": "success"},
                metadata={"interface": "whatsapp"}
            )
            ```
        """
        try:
            trace_body = {
                "id": self.id,
                "name": name,
                "userId": user_id,
                "sessionId": session_id
                or kwargs.get("sessionId", None),  # backward compatibility
                "version": version,
                "release": release,
                "input": input,
                "output": output,
                "metadata": metadata,
                "public": public,
                "tags": tags,
                **kwargs,
            }
            self.log.debug(f"Update trace {_filter_io_from_event_body(trace_body)}...")

            request = TraceBody(**trace_body)

            event = {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "body": request,
            }

            self.task_manager.add_task(event)

        except Exception as e:
            self.log.exception(e)
        finally:
            return StatefulTraceClient(
                self.client,
                self.id,
                StateType.TRACE,
                self.trace_id,
                task_manager=self.task_manager,
            )

    def get_langchain_handler(self, update_parent: bool = False):
        """Get langchain callback handler associated with the current trace.

        This method creates and returns a CallbackHandler instance, linking it with the current
        trace. Use this if you want to group multiple Langchain runs within a single trace.

        Args:
            update_parent (bool): If set to True, the parent trace will be updated with the outcome of the Langchain run.

        Raises:
            ImportError: If the 'langchain' module is not installed, indicating missing functionality.

        Returns:
            CallbackHandler: Langchain callback handler linked to the current trace.

        Example:
            ```python
            from langfuse import Langfuse

            langfuse = Langfuse()

            # Create a trace
            trace = langfuse.trace(name = "llm-feature")

            # Get a langchain callback handler
            handler = trace.get_langchain_handler()
            ```
        """
        try:
            from langfuse.callback import CallbackHandler

            self.log.debug(f"Creating new handler for trace {self.id}")

            return CallbackHandler(
                stateful_client=self,
                debug=self.log.level == logging.DEBUG,
                update_stateful_client=update_parent,
            )
        except Exception as e:
            self.log.exception(e)

    def getNewHandler(self):
        """Alias for the `get_langchain_handler` method. Retrieves a callback handler for the trace. Deprecated."""
        return self.get_langchain_handler()


class DatasetItemClient:
    """Class for managing dataset items in Langfuse.

    Args:
        id (str): Unique identifier of the dataset item.
        status (DatasetStatus): The status of the dataset item. Can be either 'ACTIVE' or 'ARCHIVED'.
        input (Any): Input data of the dataset item.
        expected_output (Optional[Any]): Expected output of the dataset item.
        metadata (Optional[Any]): Additional metadata of the dataset item.
        source_trace_id (Optional[str]): Identifier of the source trace.
        source_observation_id (Optional[str]): Identifier of the source observation.
        dataset_id (str): Identifier of the dataset to which this item belongs.
        dataset_name (str): Name of the dataset to which this item belongs.
        created_at (datetime): Timestamp of dataset item creation.
        updated_at (datetime): Timestamp of the last update to the dataset item.
        langfuse (Langfuse): Instance of Langfuse client for API interactions.

    Example:
        ```python
        from langfuse import Langfuse

        langfuse = Langfuse()

        dataset = langfuse.get_dataset("<dataset_name>")

        for item in dataset.items:
            # Generate a completion using the input of every item
            completion, generation = llm_app.run(item.input)

            # Evaluate the completion
            generation.score(
                name="example-score",
                value=1
            )
        ```
    """

    log = logging.getLogger("langfuse")

    id: str
    status: DatasetStatus
    input: typing.Any
    expected_output: typing.Optional[typing.Any]
    metadata: Optional[Any]
    source_trace_id: typing.Optional[str]
    source_observation_id: typing.Optional[str]
    dataset_id: str
    dataset_name: str
    created_at: dt.datetime
    updated_at: dt.datetime

    langfuse: Langfuse

    def __init__(self, dataset_item: DatasetItem, langfuse: Langfuse):
        """Initialize the DatasetItemClient."""
        self.id = dataset_item.id
        self.status = dataset_item.status
        self.input = dataset_item.input
        self.expected_output = dataset_item.expected_output
        self.metadata = dataset_item.metadata
        self.source_trace_id = dataset_item.source_trace_id
        self.source_observation_id = dataset_item.source_observation_id
        self.dataset_id = dataset_item.dataset_id
        self.dataset_name = dataset_item.dataset_name
        self.created_at = dataset_item.created_at
        self.updated_at = dataset_item.updated_at

        self.langfuse = langfuse

    def flush(self, observation: StatefulClient, run_name: str):
        """Flushes an observations task manager's queue.

        Used before creating a dataset run item to ensure all events are persistent.

        Args:
            observation (StatefulClient): The observation or trace client associated with the dataset item.
            run_name (str): The name of the dataset run.
        """
        observation.task_manager.flush()

    def link(
        self,
        trace_or_observation: typing.Union[StatefulClient, str, None],
        run_name: str,
        run_metadata: Optional[Any] = None,
        run_description: Optional[str] = None,
        trace_id: Optional[str] = None,
        observation_id: Optional[str] = None,
    ):
        """Link the dataset item to observation within a specific dataset run. Creates a dataset run item.

        Args:
            trace_or_observation (Union[StatefulClient, str, None]): The trace or observation object to link. Deprecated: can also be an observation ID.
            run_name (str): The name of the dataset run.
            run_metadata (Optional[Any]): Additional metadata to include in dataset run.
            run_description (Optional[str]): Description of the dataset run.
            trace_id (Optional[str]): The trace ID to link to the dataset item. Set trace_or_observation to None if trace_id is provided.
            observation_id (Optional[str]): The observation ID to link to the dataset item (optional). Set trace_or_observation to None if trace_id is provided.
        """
        parsed_trace_id: str = None
        parsed_observation_id: str = None

        if isinstance(trace_or_observation, StatefulClient):
            # flush the queue before creating the dataset run item
            # to ensure that all events are persisted.
            if trace_or_observation.state_type == StateType.TRACE:
                parsed_trace_id = trace_or_observation.trace_id
            elif trace_or_observation.state_type == StateType.OBSERVATION:
                parsed_observation_id = trace_or_observation.id
                parsed_trace_id = trace_or_observation.trace_id
        # legacy support for observation_id
        elif isinstance(trace_or_observation, str):
            parsed_observation_id = trace_or_observation
        elif trace_or_observation is None:
            if trace_id is not None:
                parsed_trace_id = trace_id
                if observation_id is not None:
                    parsed_observation_id = observation_id
            else:
                raise ValueError(
                    "trace_id must be provided if trace_or_observation is None"
                )
        else:
            raise ValueError(
                "trace_or_observation (arg) or trace_id (kwarg) must be provided to link the dataset item"
            )

        self.log.debug(
            f"Creating dataset run item: {run_name} {self.id} {parsed_trace_id} {parsed_observation_id}"
        )
        self.langfuse.client.dataset_run_items.create(
            request=CreateDatasetRunItemRequest(
                runName=run_name,
                datasetItemId=self.id,
                traceId=parsed_trace_id,
                observationId=parsed_observation_id,
                metadata=run_metadata,
                runDescription=run_description,
            )
        )

    def get_langchain_handler(
        self,
        *,
        run_name: str,
        run_description: Optional[str] = None,
        run_metadata: Optional[Any] = None,
    ):
        """Create and get a langchain callback handler linked to this dataset item.

        Args:
            run_name (str): The name of the dataset run to be used in the callback handler.
            run_description (Optional[str]): Description of the dataset run.
            run_metadata (Optional[Any]): Additional metadata to include in dataset run.

        Returns:
            CallbackHandler: An instance of CallbackHandler linked to the dataset item.
        """
        metadata = {
            "dataset_item_id": self.id,
            "run_name": run_name,
            "dataset_id": self.dataset_id,
        }
        trace = self.langfuse.trace(name="dataset-run", metadata=metadata)

        self.link(
            trace, run_name, run_metadata=run_metadata, run_description=run_description
        )

        return trace.get_langchain_handler(update_parent=True)

    @contextmanager
    def observe(
        self,
        *,
        run_name: str,
        run_description: Optional[str] = None,
        run_metadata: Optional[Any] = None,
        trace_id: Optional[str] = None,
    ):
        """Observes a dataset run within the Langfuse client.

        Args:
            run_name (str): The name of the dataset run.
            root_trace (Optional[StatefulTraceClient]): The root trace client to use for the dataset run. If not provided, a new trace client will be created.
            run_description (Optional[str]): The description of the dataset run.
            run_metadata (Optional[Any]): Additional metadata for the dataset run.

        Yields:
            StatefulTraceClient: The trace associated with the dataset run.
        """
        from langfuse.decorators import langfuse_context

        root_trace_id = trace_id or str(uuid.uuid4())

        langfuse_context._set_root_trace_id(root_trace_id)

        try:
            yield root_trace_id

        finally:
            self.link(
                run_name=run_name,
                run_metadata=run_metadata,
                run_description=run_description,
                trace_or_observation=None,
                trace_id=root_trace_id,
            )

    @contextmanager
    def observe_llama_index(
        self,
        *,
        run_name: str,
        run_description: Optional[str] = None,
        run_metadata: Optional[Any] = None,
        llama_index_integration_constructor_kwargs: Optional[Dict[str, Any]] = {},
    ):
        """Context manager for observing LlamaIndex operations linked to this dataset item.

        This method sets up a LlamaIndex callback handler that integrates with Langfuse, allowing detailed logging
        and tracing of LlamaIndex operations within the context of a specific dataset run. It ensures that all
        operations performed within the context are linked to the appropriate dataset item and run in Langfuse.

        Args:
            run_name (str): The name of the dataset run.
            run_description (Optional[str]): Description of the dataset run. Defaults to None.
            run_metadata (Optional[Any]): Additional metadata for the dataset run. Defaults to None.
            llama_index_integration_constructor_kwargs (Optional[Dict[str, Any]]): Keyword arguments to pass
                to the LlamaIndex integration constructor. Defaults to an empty dictionary.

        Yields:
            LlamaIndexCallbackHandler: The callback handler for LlamaIndex operations.

        Example:
            ```python
            dataset_item = dataset.items[0]

            with dataset_item.observe_llama_index(run_name="example-run", run_description="Example LlamaIndex run") as handler:
                # Perform LlamaIndex operations here
                some_llama_index_operation()
            ```

        Raises:
            ImportError: If required modules for LlamaIndex integration are not available.
        """
        metadata = {
            "dataset_item_id": self.id,
            "run_name": run_name,
            "dataset_id": self.dataset_id,
        }
        trace = self.langfuse.trace(name="dataset-run", metadata=metadata)
        self.link(
            trace, run_name, run_metadata=run_metadata, run_description=run_description
        )

        try:
            import llama_index.core
            from llama_index.core import Settings
            from llama_index.core.callbacks import CallbackManager

            from langfuse.llama_index import LlamaIndexCallbackHandler

            callback_handler = LlamaIndexCallbackHandler(
                **llama_index_integration_constructor_kwargs,
            )
            callback_handler.set_root(trace, update_root=True)

            # Temporarily set the global handler to the new handler if previous handler is a LlamaIndexCallbackHandler
            # LlamaIndex does not adding two errors of same type, so if global handler is already a LlamaIndexCallbackHandler, we need to remove it
            prev_global_handler = llama_index.core.global_handler
            prev_langfuse_handler = None

            if isinstance(prev_global_handler, LlamaIndexCallbackHandler):
                llama_index.core.global_handler = None

            if Settings.callback_manager is None:
                Settings.callback_manager = CallbackManager([callback_handler])
            else:
                for handler in Settings.callback_manager.handlers:
                    if isinstance(handler, LlamaIndexCallbackHandler):
                        prev_langfuse_handler = handler
                        Settings.callback_manager.remove_handler(handler)

                Settings.callback_manager.add_handler(callback_handler)

        except Exception as e:
            self.log.exception(e)

        try:
            yield callback_handler
        finally:
            # Reset the handlers
            Settings.callback_manager.remove_handler(callback_handler)
            if prev_langfuse_handler is not None:
                Settings.callback_manager.add_handler(prev_langfuse_handler)

            llama_index.core.global_handler = prev_global_handler

    def get_llama_index_handler(
        self,
        *,
        run_name: str,
        run_description: Optional[str] = None,
        run_metadata: Optional[Any] = None,
        llama_index_integration_constructor_kwargs: Optional[Dict[str, Any]] = {},
    ):
        """Create and get a llama-index callback handler linked to this dataset item.

        Args:
            run_name (str): The name of the dataset run to be used in the callback handler.
            run_description (Optional[str]): Description of the dataset run.
            run_metadata (Optional[Any]): Additional metadata to include in dataset run.
            llama_index_integration_constructor_kwargs (Optional[Dict[str, Any]]): Additional keyword arguments to pass to the LlamaIndex integration constructor.

        Returns:
            LlamaIndexCallbackHandler: An instance of LlamaIndexCallbackHandler linked to the dataset item.
        """
        metadata = {
            "dataset_item_id": self.id,
            "run_name": run_name,
            "dataset_id": self.dataset_id,
        }
        trace = self.langfuse.trace(name="dataset-run", metadata=metadata)

        self.link(
            trace, run_name, run_metadata=run_metadata, run_description=run_description
        )

        try:
            from langfuse.llama_index.llama_index import LlamaIndexCallbackHandler

            callback_handler = LlamaIndexCallbackHandler(
                **llama_index_integration_constructor_kwargs,
            )
            callback_handler.set_root(trace, update_root=True)

            return callback_handler
        except Exception as e:
            self.log.exception(e)


class DatasetClient:
    """Class for managing datasets in Langfuse.

    Attributes:
        id (str): Unique identifier of the dataset.
        name (str): Name of the dataset.
        description (Optional[str]): Description of the dataset.
        metadata (Optional[typing.Any]): Additional metadata of the dataset.
        project_id (str): Identifier of the project to which the dataset belongs.
        dataset_name (str): Name of the dataset.
        created_at (datetime): Timestamp of dataset creation.
        updated_at (datetime): Timestamp of the last update to the dataset.
        items (List[DatasetItemClient]): List of dataset items associated with the dataset.
        runs (List[str]): List of dataset runs associated with the dataset. Deprecated.

    Example:
        Print the input of each dataset item in a dataset.
        ```python
        from langfuse import Langfuse

        langfuse = Langfuse()

        dataset = langfuse.get_dataset("<dataset_name>")

        for item in dataset.items:
            print(item.input)
        ```
    """

    id: str
    name: str
    description: Optional[str]
    project_id: str
    dataset_name: str  # for backward compatibility, to be deprecated
    metadata: Optional[Any]
    created_at: dt.datetime
    updated_at: dt.datetime
    items: typing.List[DatasetItemClient]
    runs: typing.List[str] = []  # deprecated

    def __init__(self, dataset: Dataset, items: typing.List[DatasetItemClient]):
        """Initialize the DatasetClient."""
        self.id = dataset.id
        self.name = dataset.name
        self.description = dataset.description
        self.project_id = dataset.project_id
        self.metadata = dataset.metadata
        self.dataset_name = dataset.name  # for backward compatibility, to be deprecated
        self.created_at = dataset.created_at
        self.updated_at = dataset.updated_at
        self.items = items


def _filter_io_from_event_body(event_body: Dict[str, Any]):
    return {
        k: v for k, v in event_body.items() if k not in ("input", "output", "metadata")
    }
