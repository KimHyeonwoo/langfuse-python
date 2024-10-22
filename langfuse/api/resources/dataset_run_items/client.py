# This file was auto-generated by Fern from our API Definition.

import typing
from json.decoder import JSONDecodeError

from ...core.api_error import ApiError
from ...core.client_wrapper import AsyncClientWrapper, SyncClientWrapper
from ...core.pydantic_utilities import pydantic_v1
from ...core.request_options import RequestOptions
from ..commons.errors.access_denied_error import AccessDeniedError
from ..commons.errors.error import Error
from ..commons.errors.method_not_allowed_error import MethodNotAllowedError
from ..commons.errors.not_found_error import NotFoundError
from ..commons.errors.unauthorized_error import UnauthorizedError
from ..commons.types.dataset_run_item import DatasetRunItem
from .types.create_dataset_run_item_request import CreateDatasetRunItemRequest

# this is used as the default value for optional parameters
OMIT = typing.cast(typing.Any, ...)


class DatasetRunItemsClient:
    def __init__(self, *, client_wrapper: SyncClientWrapper):
        self._client_wrapper = client_wrapper

    def create(
        self, *, request: CreateDatasetRunItemRequest, request_options: typing.Optional[RequestOptions] = None
    ) -> DatasetRunItem:
        """
        Create a dataset run item

        Parameters
        ----------
        request : CreateDatasetRunItemRequest

        request_options : typing.Optional[RequestOptions]
            Request-specific configuration.

        Returns
        -------
        DatasetRunItem

        Examples
        --------
        from finto import CreateDatasetRunItemRequest
        from finto.client import FernLangfuse

        client = FernLangfuse(
            x_langfuse_sdk_name="YOUR_X_LANGFUSE_SDK_NAME",
            x_langfuse_sdk_version="YOUR_X_LANGFUSE_SDK_VERSION",
            x_langfuse_public_key="YOUR_X_LANGFUSE_PUBLIC_KEY",
            username="YOUR_USERNAME",
            password="YOUR_PASSWORD",
            base_url="https://yourhost.com/path/to/api",
        )
        client.dataset_run_items.create(
            request=CreateDatasetRunItemRequest(
                run_name="string",
                run_description="string",
                metadata={"key": "value"},
                dataset_item_id="string",
                observation_id="string",
                trace_id="string",
            ),
        )
        """
        _response = self._client_wrapper.httpx_client.request(
            "api/public/dataset-run-items", method="POST", json=request, request_options=request_options, omit=OMIT
        )
        try:
            if 200 <= _response.status_code < 300:
                return pydantic_v1.parse_obj_as(DatasetRunItem, _response.json())  # type: ignore
            if _response.status_code == 400:
                raise Error(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 401:
                raise UnauthorizedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 403:
                raise AccessDeniedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 405:
                raise MethodNotAllowedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 404:
                raise NotFoundError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            _response_json = _response.json()
        except JSONDecodeError:
            raise ApiError(status_code=_response.status_code, body=_response.text)
        raise ApiError(status_code=_response.status_code, body=_response_json)


class AsyncDatasetRunItemsClient:
    def __init__(self, *, client_wrapper: AsyncClientWrapper):
        self._client_wrapper = client_wrapper

    async def create(
        self, *, request: CreateDatasetRunItemRequest, request_options: typing.Optional[RequestOptions] = None
    ) -> DatasetRunItem:
        """
        Create a dataset run item

        Parameters
        ----------
        request : CreateDatasetRunItemRequest

        request_options : typing.Optional[RequestOptions]
            Request-specific configuration.

        Returns
        -------
        DatasetRunItem

        Examples
        --------
        import asyncio

        from finto import CreateDatasetRunItemRequest
        from finto.client import AsyncFernLangfuse

        client = AsyncFernLangfuse(
            x_langfuse_sdk_name="YOUR_X_LANGFUSE_SDK_NAME",
            x_langfuse_sdk_version="YOUR_X_LANGFUSE_SDK_VERSION",
            x_langfuse_public_key="YOUR_X_LANGFUSE_PUBLIC_KEY",
            username="YOUR_USERNAME",
            password="YOUR_PASSWORD",
            base_url="https://yourhost.com/path/to/api",
        )


        async def main() -> None:
            await client.dataset_run_items.create(
                request=CreateDatasetRunItemRequest(
                    run_name="string",
                    run_description="string",
                    metadata={"key": "value"},
                    dataset_item_id="string",
                    observation_id="string",
                    trace_id="string",
                ),
            )


        asyncio.run(main())
        """
        _response = await self._client_wrapper.httpx_client.request(
            "api/public/dataset-run-items", method="POST", json=request, request_options=request_options, omit=OMIT
        )
        try:
            if 200 <= _response.status_code < 300:
                return pydantic_v1.parse_obj_as(DatasetRunItem, _response.json())  # type: ignore
            if _response.status_code == 400:
                raise Error(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 401:
                raise UnauthorizedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 403:
                raise AccessDeniedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 405:
                raise MethodNotAllowedError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            if _response.status_code == 404:
                raise NotFoundError(pydantic_v1.parse_obj_as(typing.Any, _response.json()))  # type: ignore
            _response_json = _response.json()
        except JSONDecodeError:
            raise ApiError(status_code=_response.status_code, body=_response.text)
        raise ApiError(status_code=_response.status_code, body=_response_json)
