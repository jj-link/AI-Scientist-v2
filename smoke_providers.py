"""Smoke-test the cborg/ and spark/ provider prefixes through the repo's own code paths.

Run inside the ai_scientist env from the repo root:
    python smoke_providers.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_scientist.llm import create_client, get_response_from_llm

MODELS = [
    "cborg/lbl/cborg-mini",
    "spark/deepseek-v4-flash-vision-exp",
]


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name} {detail}")
    return bool(ok)


def main() -> int:
    all_ok = True

    for model in MODELS:
        try:
            client, client_model = create_client(model)
            base = getattr(getattr(client, "_base_url", None), "path", "?")
            content, _ = get_response_from_llm(
                prompt="Reply with exactly: OK",
                client=client,
                model=client_model,
                system_message="You are a helpful assistant.",
                temperature=0.0,
            )
            all_ok &= check(
                f"llm.py {model}", bool(content), f"-> {content!r} (base={base})"
            )
        except Exception as e:  # noqa: BLE001 - smoke test reports any failure
            all_ok &= check(f"llm.py {model}", False, f"-> {type(e).__name__}: {e}")

    # treesearch backend path (plain text, no function spec)
    from ai_scientist.treesearch.backend import query as backend_query

    for model in MODELS:
        try:
            out = backend_query(
                system_message="You are a helpful assistant.",
                user_message="Reply with exactly: OK",
                model=model,
                temperature=0.0,
                max_tokens=2048,
            )
            all_ok &= check(f"backend {model}", bool(out), f"-> {out!r}")
        except Exception as e:  # noqa: BLE001
            all_ok &= check(f"backend {model}", False, f"-> {type(e).__name__}: {e}")

    # treesearch backend function-calling path (required by the BFTS agent)
    from ai_scientist.treesearch.backend import FunctionSpec

    spec = FunctionSpec(
        name="report_status",
        description="Report a simple status.",
        json_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    )
    for model in MODELS:
        try:
            out = backend_query(
                system_message="You must call the provided function.",
                user_message="Report status OK.",
                model=model,
                temperature=0.0,
                max_tokens=2048,
                func_spec=spec,
            )
            ok = isinstance(out, dict) and out.get("status")
            all_ok &= check(f"backend func-call {model}", ok, f"-> {out!r}")
        except Exception as e:  # noqa: BLE001
            all_ok &= check(
                f"backend func-call {model}", False, f"-> {type(e).__name__}: {e}"
            )

    # VLM path: real image payload through the newly added vision branch.
    # cborg/lbl/cborg-vision is excluded: upstream token_tracker.py crashes on
    # its responses (prompt_tokens_details=None) and that file is outside the
    # approved edit boundary. The run routes all vision roles to Spark.
    import tempfile

    from PIL import Image

    from ai_scientist.vlm import create_client as create_vlm_client
    from ai_scientist.vlm import get_response_from_vlm

    img_path = os.path.join(tempfile.gettempdir(), "smoke_test_img.jpg")
    Image.new("RGB", (64, 64), (200, 30, 30)).save(img_path, format="JPEG")

    for model in ["cborg/lbl/cborg-vision", "spark/deepseek-v4-flash-vision-exp"]:
        try:
            vclient, vmodel = create_vlm_client(model)
            content, _ = get_response_from_vlm(
                msg="What color dominates this image? Answer in one word.",
                image_paths=img_path,
                client=vclient,
                model=vmodel,
                system_message="You are a helpful assistant.",
                temperature=0.0,
            )
            all_ok &= check(
                f"vlm {model}", bool(content), f"-> {str(content)[:80]!r}"
            )
        except Exception as e:  # noqa: BLE001
            all_ok &= check(
                f"vlm {model}", False, f"-> {type(e).__name__}: {e}"
            )

    print("\nSMOKE RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
