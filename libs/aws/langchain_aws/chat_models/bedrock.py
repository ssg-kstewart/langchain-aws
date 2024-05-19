import re
from collections import defaultdict
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from langchain_core._api.deprecation import deprecated
from langchain_core.callbacks import (
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.messages import (
    system as langchain_system,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.pydantic_v1 import BaseModel, Extra
from langchain_core.tools import BaseTool

from langchain_aws.function_calling import convert_to_anthropic_tool, get_system_message
from langchain_aws.llms.bedrock import BedrockBase
from langchain_aws.utils import (
    get_num_tokens_anthropic,
    get_token_ids_anthropic,
)


def _convert_one_message_to_text_llama(message: BaseMessage) -> str:
    if isinstance(message, ChatMessage):
        message_text = f"\n\n{message.role.capitalize()}: {message.content}"
    elif isinstance(message, HumanMessage):
        message_text = f"[INST] {message.content} [/INST]"
    elif isinstance(message, AIMessage):
        message_text = f"{message.content}"
    elif isinstance(message, SystemMessage):
        message_text = f"<<SYS>> {message.content} <</SYS>>"
    else:
        raise ValueError(f"Got unknown type {message}")
    return message_text


def convert_messages_to_prompt_llama(messages: List[BaseMessage]) -> str:
    """Convert a list of messages to a prompt for llama."""

    return "\n".join(
        [_convert_one_message_to_text_llama(message) for message in messages]
    )


def _convert_one_message_to_text_llama3(message: BaseMessage) -> str:
    if isinstance(message, ChatMessage):
        message_text = (
            f"<|start_header_id|>{message.role}"
            f"<|end_header_id|>{message.content}<|eot_id|>"
        )
    elif isinstance(message, HumanMessage):
        message_text = (
            f"<|start_header_id|>user" f"<|end_header_id|>{message.content}<|eot_id|>"
        )
    elif isinstance(message, AIMessage):
        message_text = (
            f"<|start_header_id|>assistant"
            f"<|end_header_id|>{message.content}<|eot_id|>"
        )
    elif isinstance(message, SystemMessage):
        message_text = (
            f"<|start_header_id|>system" f"<|end_header_id|>{message.content}<|eot_id|>"
        )
    else:
        raise ValueError(f"Got unknown type {message}")

    return message_text


def convert_messages_to_prompt_llama3(messages: List[BaseMessage]) -> str:
    """Convert a list of messages to a prompt for llama."""

    return "\n".join(
        ["<|begin_of_text|>"]
        + [_convert_one_message_to_text_llama3(message) for message in messages]
        + ["<|start_header_id|>assistant<|end_header_id|>\n\n"]
    )


def _convert_one_message_to_text_anthropic(
    message: BaseMessage,
    human_prompt: str,
    ai_prompt: str,
) -> str:
    content = cast(str, message.content)
    if isinstance(message, ChatMessage):
        message_text = f"\n\n{message.role.capitalize()}: {content}"
    elif isinstance(message, HumanMessage):
        message_text = f"{human_prompt} {content}"
    elif isinstance(message, AIMessage):
        message_text = f"{ai_prompt} {content}"
    elif isinstance(message, SystemMessage):
        message_text = content
    else:
        raise ValueError(f"Got unknown type {message}")
    return message_text


def convert_messages_to_prompt_anthropic(
    messages: List[BaseMessage],
    *,
    human_prompt: str = "\n\nHuman:",
    ai_prompt: str = "\n\nAssistant:",
) -> str:
    """Format a list of messages into a full prompt for the Anthropic model
    Args:
        messages (List[BaseMessage]): List of BaseMessage to combine.
        human_prompt (str, optional): Human prompt tag. Defaults to "\n\nHuman:".
        ai_prompt (str, optional): AI prompt tag. Defaults to "\n\nAssistant:".
    Returns:
        str: Combined string with necessary human_prompt and ai_prompt tags.
    """

    messages = messages.copy()  # don't mutate the original list
    if not isinstance(messages[-1], AIMessage):
        messages.append(AIMessage(content=""))

    text = "".join(
        _convert_one_message_to_text_anthropic(message, human_prompt, ai_prompt)
        for message in messages
    )

    # trim off the trailing ' ' that might come from the "Assistant: "
    return text.rstrip()


def _convert_one_message_to_text_mistral(message: BaseMessage) -> str:
    if isinstance(message, ChatMessage):
        message_text = f"\n\n{message.role.capitalize()}: {message.content}"
    elif isinstance(message, HumanMessage):
        message_text = f"[INST] {message.content} [/INST]"
    elif isinstance(message, AIMessage):
        message_text = f"{message.content}"
    elif isinstance(message, SystemMessage):
        message_text = f"<<SYS>> {message.content} <</SYS>>"
    else:
        raise ValueError(f"Got unknown type {message}")
    return message_text


def convert_messages_to_prompt_mistral(messages: List[BaseMessage]) -> str:
    """Convert a list of messages to a prompt for mistral."""
    return "\n".join(
        [_convert_one_message_to_text_mistral(message) for message in messages]
    )


def _format_image(image_url: str) -> Dict:
    """
    Formats an image of format data:image/jpeg;base64,{b64_string}
    to a dict for anthropic api

    {
      "type": "base64",
      "media_type": "image/jpeg",
      "data": "/9j/4AAQSkZJRg...",
    }

    And throws an error if it's not a b64 image
    """
    regex = r"^data:(?P<media_type>image/.+);base64,(?P<data>.+)$"
    match = re.match(regex, image_url)
    if match is None:
        raise ValueError(
            "Anthropic only supports base64-encoded images currently."
            " Example: data:image/png;base64,'/9j/4AAQSk'..."
        )
    return {
        "type": "base64",
        "media_type": match.group("media_type"),
        "data": match.group("data"),
    }


def _format_anthropic_messages(
    messages: List[BaseMessage],
) -> Tuple[Optional[str], List[Dict]]:
    """Format messages for anthropic."""

    """
    [
        {
            "role": _message_type_lookups[m.type],
            "content": [_AnthropicMessageContent(text=m.content).dict()],
        }
        for m in messages
    ]
    """
    system: Optional[str] = None
    formatted_messages: List[Dict] = []

    # do not enumerate when calling generate()
    if isinstance(messages, langchain_system.BaseMessage):
        if messages.type == "system":
            if not isinstance(messages.content, str):
                raise ValueError(
                    "System message must be a string, "
                    f"instead was: {type(messages.content)}"
                )
            system = messages.content
            role = _message_type_lookups["human"]
        else:
            role = _message_type_lookups[messages.type]
        content: Union[str, List[Dict]]

        if not isinstance(messages.content, str):
            # parse as dict
            assert isinstance(
                messages.content, list
            ), "Anthropic message content must be str or list of dicts"

            # populate content
            content = []
            for item in messages.content:
                if isinstance(item, str):
                    content.append(
                        {
                            "type": "text",
                            "text": item,
                        }
                    )
                elif isinstance(item, dict):
                    if "type" not in item:
                        raise ValueError("Dict content item must have a type key")
                    if item["type"] == "image_url":
                        # convert format
                        source = _format_image(item["image_url"]["url"])
                        content.append(
                            {
                                "type": "image",
                                "source": source,
                            }
                        )
                    else:
                        content.append(item)
                else:
                    raise ValueError(
                        f"Content items must be str or dict, instead was: {type(item)}"
                    )
        else:
            content = messages.content

        formatted_messages.append(
            {
                "role": role,
                "content": content,
            }
        )
    else:
        for i, message in enumerate(messages):
            if message.type == "system":
                if i != 0:
                    raise ValueError(
                        "System message must be at beginning of message list."
                    )
                if not isinstance(message.content, str):
                    raise ValueError(
                        "System message must be a string, "
                        f"instead was: {type(message.content)}"
                    )
                system = message.content
                continue

            role = _message_type_lookups[message.type]
            message_content: Union[str, List[Dict]]

            if not isinstance(message.content, str):
                # parse as dict
                assert isinstance(
                    message.content, list
                ), "Anthropic message content must be str or list of dicts"

                # populate content
                message_content = []
                for item in message.content:
                    if isinstance(item, str):
                        message_content.append(
                            {
                                "type": "text",
                                "text": item,
                            }
                        )
                    elif isinstance(item, dict):
                        if "type" not in item:
                            raise ValueError("Dict content item must have a type key")
                        if item["type"] == "image_url":
                            # convert format
                            source = _format_image(item["image_url"]["url"])
                            message_content.append(
                                {
                                    "type": "image",
                                    "source": source,
                                }
                            )
                        else:
                            message_content.append(item)
                    else:
                        raise ValueError(
                            f"""Content items 
                                must be str or dict, instead was: {type(item)}"""
                        )
            else:
                message_content = message.content

            formatted_messages.append(
                {
                    "role": role,
                    "content": message_content,
                }
            )
    return system, formatted_messages


class ChatPromptAdapter:
    """Adapter class to prepare the inputs from Langchain to prompt format
    that Chat model expects.
    """

    @classmethod
    def convert_messages_to_prompt(
        cls, provider: str, messages: List[BaseMessage], model: str
    ) -> str:
        if provider == "anthropic":
            prompt = convert_messages_to_prompt_anthropic(messages=messages)
        elif provider == "meta":
            if "llama3" in model:
                prompt = convert_messages_to_prompt_llama3(messages=messages)
            else:
                prompt = convert_messages_to_prompt_llama(messages=messages)
        elif provider == "mistral":
            prompt = convert_messages_to_prompt_mistral(messages=messages)
        elif provider == "amazon":
            prompt = convert_messages_to_prompt_anthropic(
                messages=messages,
                human_prompt="\n\nUser:",
                ai_prompt="\n\nBot:",
            )
        else:
            raise NotImplementedError(
                f"Provider {provider} model does not support chat."
            )
        return prompt

    @classmethod
    def format_messages(
        cls, provider: str, messages: List[BaseMessage]
    ) -> Tuple[Optional[str], List[Dict]]:
        if provider == "anthropic":
            return _format_anthropic_messages(messages)

        raise NotImplementedError(
            f"Provider {provider} not supported for format_messages"
        )


_message_type_lookups = {"human": "user", "ai": "assistant"}


class ChatBedrock(BaseChatModel, BedrockBase):
    """A chat model that uses the Bedrock API."""

    system_prompt_with_tools: str = ""

    @property
    def _llm_type(self) -> str:
        """Return type of chat model."""
        return "amazon_bedrock_chat"

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether this model can be serialized by Langchain."""
        return True

    @classmethod
    def get_lc_namespace(cls) -> List[str]:
        """Get the namespace of the langchain object."""
        return ["langchain", "chat_models", "bedrock"]

    @property
    def lc_attributes(self) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {}

        if self.region_name:
            attributes["region_name"] = self.region_name

        return attributes

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        provider = self._get_provider()
        prompt, system, formatted_messages = None, None, None

        if provider == "anthropic":
            system, formatted_messages = ChatPromptAdapter.format_messages(
                provider, messages
            )
            if self.system_prompt_with_tools:
                if system:
                    system = self.system_prompt_with_tools + f"\n{system}"
                else:
                    system = self.system_prompt_with_tools
        else:
            prompt = ChatPromptAdapter.convert_messages_to_prompt(
                provider=provider, messages=messages, model=self._get_model()
            )

        for chunk in self._prepare_input_and_invoke_stream(
            prompt=prompt,
            system=system,
            messages=formatted_messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            delta = chunk.text
            yield ChatGenerationChunk(message=AIMessageChunk(content=delta))

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        completion = ""
        llm_output: Dict[str, Any] = {"model_id": self.model_id}
        usage_info: Dict[str, Any] = {}
        if self.streaming:
            for chunk in self._stream(messages, stop, run_manager, **kwargs):
                completion += chunk.text
        else:
            provider = self._get_provider()
            prompt, system, formatted_messages = None, None, None
            params: Dict[str, Any] = {**kwargs}

            if provider == "anthropic":
                system, formatted_messages = ChatPromptAdapter.format_messages(
                    provider, messages
                )
                if self.system_prompt_with_tools:
                    if system:
                        system = self.system_prompt_with_tools + f"\n{system}"
                    else:
                        system = self.system_prompt_with_tools
            else:
                prompt = ChatPromptAdapter.convert_messages_to_prompt(
                    provider=provider, messages=messages, model=self._get_model()
                )

            if stop:
                params["stop_sequences"] = stop

            completion, usage_info = self._prepare_input_and_invoke(
                prompt=prompt,
                stop=stop,
                run_manager=run_manager,
                system=system,
                messages=formatted_messages,
                **params,
            )

            llm_output["usage"] = usage_info

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=completion, additional_kwargs={"usage": usage_info}
                    )
                )
            ],
            llm_output=llm_output,
        )

    def _combine_llm_outputs(self, llm_outputs: List[Optional[dict]]) -> dict:
        final_usage: Dict[str, int] = defaultdict(int)
        final_output = {}
        for output in llm_outputs:
            output = output or {}
            usage = output.pop("usage", {})
            for token_type, token_count in usage.items():
                final_usage[token_type] += token_count
            final_output.update(output)
        final_output["usage"] = final_usage
        return final_output

    def get_num_tokens(self, text: str) -> int:
        if self._model_is_anthropic:
            return get_num_tokens_anthropic(text)
        else:
            return super().get_num_tokens(text)

    def get_token_ids(self, text: str) -> List[int]:
        if self._model_is_anthropic:
            return get_token_ids_anthropic(text)
        else:
            return super().get_token_ids(text)

    def set_system_prompt_with_tools(self, xml_tools_system_prompt: str) -> None:
        """Workaround to bind. Sets the system prompt with tools"""
        self.system_prompt_with_tools = xml_tools_system_prompt

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        *,
        tool_choice: Optional[Union[dict, str, Literal["auto", "none"], bool]] = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """Bind tool-like objects to this chat model.

        Assumes model has a tool calling API.

        Args:
            tools: A list of tool definitions to bind to this chat model.
                Can be  a dictionary, pydantic model, callable, or BaseTool. Pydantic
                models, callables, and BaseTools will be automatically converted to
                their schema dictionary representation.
            tool_choice: Which tool to require the model to call.
                Must be the name of the single provided function or
                "auto" to automatically determine which function to call
                (if any), or a dict of the form:
                {"type": "function", "function": {"name": <<tool_name>>}}.
            **kwargs: Any additional parameters to pass to the
                :class:`~langchain.runnable.Runnable` constructor.
        """
        provider = self._get_provider()

        if provider == "anthropic":
            formatted_tools = [convert_to_anthropic_tool(tool) for tool in tools]
            system_formatted_tools = get_system_message(formatted_tools)
            self.set_system_prompt_with_tools(system_formatted_tools)
        return self


@deprecated(since="0.1.0", removal="0.2.0", alternative="ChatBedrock")
class BedrockChat(ChatBedrock):
    pass
