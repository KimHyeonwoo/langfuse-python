from typing import Optional, Any, Union, Dict, Mapping

from langfuse.client import (
    Langfuse,
)
from langfuse.model import ModelUsage


try:
    from llama_index.core.base.llms.types import (
        ChatResponse,
        CompletionResponse,
    )
    from llama_index.core.instrumentation.events import BaseEvent
    from llama_index.core.instrumentation.events.embedding import (
        EmbeddingStartEvent,
        EmbeddingEndEvent,
    )
    from llama_index.core.instrumentation.event_handlers import BaseEventHandler
    from llama_index.core.instrumentation.events.llm import (
        LLMCompletionEndEvent,
        LLMCompletionStartEvent,
        LLMChatEndEvent,
        LLMChatStartEvent,
    )
    from llama_index.core.utilities.token_counting import TokenCounter

except ImportError:
    raise ModuleNotFoundError(
        "Please install llama-index to use the Langfuse llama-index integration: 'pip install llama-index'"
    )

from logging import getLogger

logger = getLogger(__name__)


class LlamaIndexEventHandler(BaseEventHandler, extra="allow"):
    def __init__(
        self,
        *,
        langfuse_client: Langfuse,
        observation_updates: Dict[str, Dict[str, Any]],
    ):
        super().__init__()

        self._langfuse = langfuse_client
        self._observation_updates = observation_updates
        self._token_counter = TokenCounter()

    @classmethod
    def class_name(cls) -> str:
        """Class name."""
        return "LlamaIndexEventHandler"

    def handle(self, event: BaseEvent) -> None:
        logger.debug(f"Event {type(event).__name__} received: {event}")

        if isinstance(
            event, (LLMCompletionStartEvent, LLMChatStartEvent, EmbeddingStartEvent)
        ):
            self.update_generation_from_start_event(event)
        elif isinstance(
            event, (LLMCompletionEndEvent, LLMChatEndEvent, EmbeddingEndEvent)
        ):
            self.update_generation_from_end_event(event)

    def update_generation_from_start_event(
        self,
        event: Union[LLMCompletionStartEvent, LLMChatStartEvent, EmbeddingStartEvent],
    ) -> None:
        if event.span_id is None:
            logger.warning("Span ID is not set")
            return

        model_data = event.model_dict
        model = model_data.pop("model", None) or model_data.pop("model_name", None)
        traced_model_data = {
            k: str(v)
            for k, v in model_data.items()
            if v is not None
            and k
            in [
                "max_tokens",
                "max_retries",
                "temperature",
                "timeout",
                "strict",
                "top_logprobs",
                "logprobs",
                "embed_batch_size",
            ]
        }

        self._update_observation_updates(
            event.span_id, model=model, model_parameters=traced_model_data
        )

    def update_generation_from_end_event(
        self, event: Union[LLMCompletionEndEvent, LLMChatEndEvent, EmbeddingEndEvent]
    ) -> None:
        if event.span_id is None:
            logger.warning("Span ID is not set")
            return

        usage = None

        if isinstance(event, (LLMCompletionEndEvent, LLMChatEndEvent)):
            usage = self._parse_token_usage(event.response) if event.response else None

        if isinstance(event, EmbeddingEndEvent):
            token_count = sum(
                self._token_counter.get_string_tokens(chunk) for chunk in event.chunks
            )

            usage = {
                "input": 0,
                "output": 0,
                "total": token_count or None,
            }

        self._update_observation_updates(event.span_id, usage=usage)

    def _update_observation_updates(self, id_: str, **kwargs) -> None:
        if id_ not in self._observation_updates:
            return

        self._observation_updates[id_].update(kwargs)

    def _parse_token_usage(
        self, response: Union[ChatResponse, CompletionResponse]
    ) -> Optional[ModelUsage]:
        if (
            (raw := getattr(response, "raw", None))
            and hasattr(raw, "get")
            and (usage := raw.get("usage"))
        ):
            return _parse_usage_from_mapping(usage)

        if additional_kwargs := getattr(response, "additional_kwargs", None):
            return _parse_usage_from_mapping(additional_kwargs)


def _parse_usage_from_mapping(
    usage: Union[object, Mapping[str, Any]],
) -> ModelUsage:
    if isinstance(usage, Mapping):
        return _get_token_counts_from_mapping(usage)

    return _parse_usage_from_object(usage)


def _parse_usage_from_object(usage: object) -> ModelUsage:
    model_usage: ModelUsage = {
        "unit": None,
        "input": None,
        "output": None,
        "total": None,
        "input_cost": None,
        "output_cost": None,
        "total_cost": None,
    }

    if (prompt_tokens := getattr(usage, "prompt_tokens", None)) is not None:
        model_usage["input"] = prompt_tokens
    if (completion_tokens := getattr(usage, "completion_tokens", None)) is not None:
        model_usage["output"] = completion_tokens
    if (total_tokens := getattr(usage, "total_tokens", None)) is not None:
        model_usage["total"] = total_tokens

    return model_usage


def _get_token_counts_from_mapping(
    usage_mapping: Mapping[str, Any],
) -> ModelUsage:
    model_usage: ModelUsage = {
        "unit": None,
        "input": None,
        "output": None,
        "total": None,
        "input_cost": None,
        "output_cost": None,
        "total_cost": None,
    }
    if (prompt_tokens := usage_mapping.get("prompt_tokens")) is not None:
        model_usage["input"] = prompt_tokens
    if (completion_tokens := usage_mapping.get("completion_tokens")) is not None:
        model_usage["output"] = completion_tokens
    if (total_tokens := usage_mapping.get("total_tokens")) is not None:
        model_usage["total"] = total_tokens

    return model_usage
