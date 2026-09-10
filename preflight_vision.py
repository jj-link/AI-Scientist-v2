"""Pre-flight: cborg-vision via the exact production paths the run will use."""
import sys

sys.path.insert(0, r"C:\Users\josep\Projects\personal\AI-Scientist-v2")

from ai_scientist.llm import create_client, get_response_from_llm
from ai_scientist.treesearch.backend import FunctionSpec, query

MODEL = "cborg/lbl/cborg-vision"

# 1) llm.py path (used by writeup small-model citation rounds)
client, client_model = create_client(MODEL)
content, _ = get_response_from_llm(
    prompt="Reply with exactly: OK",
    client=client,
    model=client_model,
    system_message="You are a helpful assistant.",
    temperature=0.0,
)
print("llm.py vision text ->", repr(content))

# 2) backend path with forced tool call (used by vlm_feedback in the agent)
spec = FunctionSpec(
    name="report_status",
    description="Report status",
    json_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    },
)
out = query(
    system_message="You are a helper.",
    user_message="Report status OK.",
    func_spec=spec,
    model=MODEL,
    temperature=0,
)
print("backend vision tool ->", out)
print("PREFLIGHT:", "PASS" if (content and out) else "FAIL")
