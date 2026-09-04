"""Live smoke test for self-hosted role routing.

Usage:
    python scripts/smoke_selfhosted.py <role>
    python scripts/smoke_selfhosted.py --check-endpoint <endpoint-name>

Exercises, against real endpoints:
1. role resolution + client creation through ai_scientist.llm.create_client
2. plain-text chat completion + JSON extraction (get_response_from_llm)
3. function-calling through the treesearch backend (backend.query)
4. JSONL request logging with role/endpoint/model (no credentials)
5. clear failure when a configured endpoint is unreachable (--check-endpoint)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_scientist import model_routing
from ai_scientist.llm import (
    create_client,
    extract_json_between_markers,
    get_response_from_llm,
)


def check_endpoint_unreachable(name: str) -> None:
    try:
        model_routing.list_endpoint_models(name)
    except model_routing.RoleConfigError as e:
        print(f"UNREACHABLE-OK: clear error raised: {e}")
        return
    raise SystemExit(f"endpoint {name!r} unexpectedly reachable")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--check-endpoint":
        check_endpoint_unreachable(args[1])
        return

    role = args[0] if args else "ideation"
    log_path = os.path.join("logs", "smoke_requests.jsonl")
    os.environ[model_routing.REQUEST_LOG_ENV] = log_path

    model = f"role/{role}"
    info = model_routing.parse_model(model)
    base_url = model_routing.load_role_config()["endpoints"][info["endpoint"]][
        "base_url"
    ]
    print(
        f"resolved: {model} -> endpoint={info['endpoint']} "
        f"model={info['served_model']} role={info['role']} ({base_url})"
    )

    client, client_model = create_client(model)
    text, _ = get_response_from_llm(
        prompt='Reply with exactly this JSON and nothing else: {"ok": true}',
        client=client,
        model=client_model,
        system_message="You are a helpful assistant. Output only valid JSON.",
        temperature=0.0,
    )
    print(f"text: {text[:200]!r}")
    parsed = extract_json_between_markers(text)
    print(f"parsed: {parsed}")
    assert parsed == {"ok": True}, f"JSON parse failed: {text!r}"

    from ai_scientist.treesearch.backend import FunctionSpec
    from ai_scientist.treesearch.backend import query as backend_query

    spec = FunctionSpec(
        name="add_tool",
        json_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        description="Add two numbers",
    )
    out = backend_query(
        system_message="Use the provided tool.",
        user_message="Call add_tool with 3 and 4.",
        model="role/experiment_code",
        temperature=0.0,
        func_spec=spec,
    )
    print(f"tool result: {out}")
    assert out == {"a": 3, "b": 4}, f"function call failed: {out!r}"

    with open(log_path, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    assert records, "no request log written"
    assert all("api_key" not in json.dumps(r) for r in records)
    last = records[-1]
    assert last["ok"] is True, last
    assert last["role"] == "experiment_code", last
    assert last["endpoint"] and last["served_model"], last
    print(f"request log OK: {len(records)} records, last: {last}")
    print("SMOKE OK")


if __name__ == "__main__":
    main()
