"""Throwaway probe of uncertain editor behaviors; delete after use."""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:/Users/josep/Projects/personal/AI-Scientist-v2")
sys.path.insert(0, str(REPO))

from ai_scientist import model_routing
from ai_scientist.ui import model_settings
from ai_scientist.ui.configs import Configs, EditorConflict, InvalidConfiguration

root = Path(tempfile.mkdtemp(prefix="probe-"))
os.environ["AI_SCIENTIST_ROOT"] = str(root)
model_routing._cache.clear()

model_settings.ensure_schema(root)
model_settings.save_settings_atomic(
    root,
    {"alpha": {"api_format": "openai", "address": "http://127.0.0.1:9/v1",
               "credential_env": "ALPHA_KEY", "timeout": 600,
               "capabilities": ["text"], "requires_user_message": False}},
    {"ideation": {"server": "alpha", "model": "m1", "max_tokens": None,
                  "temperature": None, "timeout": None, "credential_env": None,
                  "requires": ["text"]}},
)
configs = Configs(root)
view = configs.editor()
print("revision:", view["revision"], len(view["revision"]))
print("ideation:", json.dumps(view["roles"]["ideation"]))
print("alpha:", json.dumps(view["endpoints"]["alpha"]))

# 1. task-level numeric validation
try:
    configs.save_editor(view["revision"], {"ideation": {"temperature": 5, "max_tokens": 0, "timeout": -1}}, {})
    after = configs.editor()
    print("NUMERIC: accepted ->", json.dumps(after["roles"]["ideation"]))
except InvalidConfiguration as exc:
    print("NUMERIC: rejected ->", exc.errors)

# 2. no-change save -> revision?
rev1 = model_settings.revision(root)
try:
    saved = configs.save_editor(model_settings.revision(root), {"ideation": {"model": "m1"}}, {})
    print("NOOP: saved revision == before:", saved["revision"] == rev1)
except InvalidConfiguration as exc:
    print("NOOP: rejected ->", exc.errors)

# 3. unknown provider row
model_settings.save_settings_atomic(
    root,
    {"beta": {"api_format": "unknown-fixture", "address": "http://127.0.0.1:10/v1",
              "credential_env": None, "timeout": None,
              "capabilities": [], "requires_user_message": False}},
    {},
)
ed = configs.editor()
print("EDITOR unknown provider row:", json.dumps(ed["endpoints"]["beta"]))
try:
    models = configs.models()
    print("MODELS unknown provider: ok", json.dumps([r for r in models["endpoints"] if r["id"] == "beta"]))
except ValueError as exc:
    print("MODELS unknown provider raises:", type(exc).__name__, exc)

# 4. empty-dict patch entries
try:
    saved = configs.save_editor(model_settings.revision(root), {"ideation": {}}, {})
    print("EMPTY DICT ENTRIES: accepted", saved["revision"] == model_settings.revision(root))
except InvalidConfiguration as exc:
    print("EMPTY DICT ENTRIES: rejected ->", exc.errors)

# 5. save_editor with only delete_tasks
try:
    saved = configs.save_editor(model_settings.revision(root), {}, {}, delete_tasks=["nonexistent"])
    print("DELETE unknown task: accepted")
except InvalidConfiguration as exc:
    print("DELETE unknown task: rejected ->", exc.errors)

shutil = __import__("shutil")
os.environ.pop("AI_SCIENTIST_ROOT", None)
shutil.rmtree(root, ignore_errors=True)
