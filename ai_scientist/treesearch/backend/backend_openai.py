import json
import logging
import time
import os

from ai_scientist import model_routing

from .utils import FunctionSpec, OutputType, opt_messages_to_list, backoff_create
from funcy import notnone, once, select_values
import openai
from rich import print

logger = logging.getLogger("ai-scientist")


OPENAI_TIMEOUT_EXCEPTIONS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)

def get_ai_client(model: str, max_retries=2) -> openai.OpenAI:
    if model_routing.is_selfhosted(model):
        return model_routing.create_selfhosted_client(model, max_retries=max_retries)
    if model.startswith("cborg/"):
        return openai.OpenAI(
            api_key=os.environ["CBORG_API_KEY"],
            base_url=os.environ.get("CBORG_API_BASE", "https://api.cborg.lbl.gov/v1"),
            max_retries=max_retries,
        )
    if model.startswith("spark/"):
        return openai.OpenAI(
            api_key=os.environ.get("SPARK_API_KEY") or "unused",
            base_url=os.environ.get("SPARK_API_BASE", "http://100.92.139.82:8888/v1"),
            max_retries=max_retries,
        )
    if model.startswith("ollama/"):
        client = openai.OpenAI(
            base_url="http://localhost:11434/v1", 
            max_retries=max_retries
        )
    else:
        client = openai.OpenAI(max_retries=max_retries)
    return client


def query(
    system_message: str | None,
    user_message: str | None,
    func_spec: FunctionSpec | None = None,
    **model_kwargs,
) -> tuple[OutputType, float, int, int, dict]:
    client = get_ai_client(model_kwargs.get("model"), max_retries=0)
    filtered_kwargs: dict = select_values(notnone, model_kwargs)  # type: ignore

    routed_model = filtered_kwargs.get("model", "")
    messages = opt_messages_to_list(system_message, user_message)

    if model_routing.is_selfhosted(routed_model) and model_routing.endpoint_settings(
        routed_model
    ).get("requires_user_message"):
        if not system_message and not user_message:
            raise ValueError("Self-hosted request requires task content.")
        if system_message and not user_message:
            # Endpoints with requires_user_message reject a system-only
            # conversation: move the compiled task content into the user turn
            # verbatim. Requests with both messages are left unchanged.
            messages = [{"role": "user", "content": system_message}]

    if func_spec is not None:
        filtered_kwargs["tools"] = [func_spec.as_openai_tool_dict]
        # force the model to use the function
        filtered_kwargs["tool_choice"] = func_spec.openai_tool_choice_dict

    if model_routing.is_selfhosted(routed_model):
        # 'role/<name>' / 'selfhosted/<endpoint>/<model>' -> served model id
        filtered_kwargs["model"] = model_routing.served_model_for(routed_model)
        if filtered_kwargs.get("max_tokens") is None:
            # Roles declare an output budget. Without it a vLLM server fills
            # the remaining context and reports 'requested 0 output tokens'
            # once the prompt approaches the model limit.
            role_max_tokens = model_routing.role_settings(routed_model).get(
                "max_tokens"
            )
            if role_max_tokens:
                filtered_kwargs["max_tokens"] = role_max_tokens
    else:
        for prefix in ("ollama/", "cborg/", "spark/"):
            if routed_model.startswith(prefix):
                filtered_kwargs["model"] = routed_model[len(prefix):]
                break

    t0 = time.time()
    max_attempts = 3 if func_spec is not None else 1
    output = None
    for attempt in range(1, max_attempts + 1):
        try:
            completion = backoff_create(
                client.chat.completions.create,
                OPENAI_TIMEOUT_EXCEPTIONS,
                messages=messages,
                **filtered_kwargs,
            )
        except Exception as e:
            model_routing.log_request(routed_model, ok=False, error=e)
            raise
        req_time = time.time() - t0

        if func_spec is None:
            choice = completion.choices[0]
            output = choice.message.content
            if model_routing.is_selfhosted(routed_model) and (
                output is None or output == ""
            ):
                # Self-hosted invariant: fail loudly with request metadata
                # only; never log response text, reasoning text, prompts, or
                # credentials, and never translate/retry it here.
                reasoning = getattr(choice.message, "reasoning_content", None) or ""
                choice_tool_calls = getattr(choice.message, "tool_calls", None)
                detail = (
                    f"Self-hosted completion returned empty content for "
                    f"{routed_model} (served model "
                    f"{filtered_kwargs.get('model')}): "
                    f"finish_reason={choice.finish_reason!r}, "
                    f"reasoning_content_chars={len(reasoning)}, "
                    f"tool_calls={'yes' if choice_tool_calls else 'no'}."
                )
                model_routing.log_request(routed_model, ok=False, error=detail)
                raise ValueError(detail)
            model_routing.log_request(
                routed_model, ok=True, latency_ms=req_time * 1000
            )
            break

        model_routing.log_request(routed_model, ok=True, latency_ms=req_time * 1000)
        choice = completion.choices[0]

        tool_calls = choice.message.tool_calls
        if tool_calls and tool_calls[0].function.name == func_spec.name:
            try:
                print(f"[cyan]Raw func call response: {choice}[/cyan]")
                output = json.loads(tool_calls[0].function.arguments)
                break
            except json.JSONDecodeError as e:
                logger.error(
                    f"Error decoding the function arguments: {tool_calls[0].function.arguments}"
                )
                output = None  # retry with a fresh completion

        logger.warning(
            f"Attempt {attempt}/{max_attempts}: expected tool call to "
            f"{func_spec.name!r}, got text response."
        )

        # Final attempt: fall back to parsing JSON out of the text reply
        if attempt == max_attempts and choice.message.content:
            content = choice.message.content
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0]
            try:
                output = json.loads(content)
                logger.warning("Parsed function output from text content (fallback).")
            except json.JSONDecodeError:
                pass

    if func_spec is not None and output is None:
        raise AssertionError(
            f"no valid function call after {max_attempts} attempts: {choice.message}"
        )

    in_tokens = completion.usage.prompt_tokens
    out_tokens = completion.usage.completion_tokens

    info = {
        "system_fingerprint": completion.system_fingerprint,
        "model": completion.model,
        "created": completion.created,
    }

    return output, req_time, in_tokens, out_tokens, info
