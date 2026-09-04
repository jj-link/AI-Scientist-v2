import json
import logging
import time
import os

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

    messages = opt_messages_to_list(system_message, user_message)

    if func_spec is not None:
        filtered_kwargs["tools"] = [func_spec.as_openai_tool_dict]
        # force the model to use the function
        filtered_kwargs["tool_choice"] = func_spec.openai_tool_choice_dict

    for prefix in ("ollama/", "cborg/", "spark/"):
        if filtered_kwargs.get("model", "").startswith(prefix):
            filtered_kwargs["model"] = filtered_kwargs["model"][len(prefix):]
            break

    t0 = time.time()
    max_attempts = 3 if func_spec is not None else 1
    output = None
    for attempt in range(1, max_attempts + 1):
        completion = backoff_create(
            client.chat.completions.create,
            OPENAI_TIMEOUT_EXCEPTIONS,
            messages=messages,
            **filtered_kwargs,
        )
        req_time = time.time() - t0
        choice = completion.choices[0]

        if func_spec is None:
            output = choice.message.content
            break

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
