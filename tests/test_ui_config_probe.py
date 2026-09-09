"""The displayed configuration, not a global default, owns endpoint probes."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
import yaml

from ai_scientist import model_routing
from ai_scientist.ui.configs import Configs


@contextmanager
def endpoint(model, status=200, expected_api_key=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            authorized = expected_api_key is None or self.headers.get("Authorization") == f"Bearer {expected_api_key}"
            self.send_response(status if authorized else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object":"list", "data":[{"id":model,"object":"model","created":0,"owned_by":"test"}]}).encode())
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SelectedConfigProbe(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "ui_data" / "checks" / str(uuid4())
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)
        model_routing._cache.clear()

    def test_validation_uses_selected_endpoint_and_ui_retains_later_results(self):
        with endpoint("global-model") as global_url, endpoint("selected-model") as selected_url, endpoint("error",500) as error_url:
            default = self.root / "ais_roles.yaml"
            selected = self.root / "ais_roles.selected.yaml"
            def config(url, model):
                return {"endpoints":{"local":{"base_url":url,"provides":["text"]}},
                        "roles":{"ideation":{"endpoint":"local","model":model,"requires":["text"]}}}
            default.write_text(yaml.safe_dump(config(global_url,"global-model")),encoding="utf-8")
            selected.write_text(yaml.safe_dump(config(selected_url,"selected-model")),encoding="utf-8")
            with patch.dict("os.environ", {model_routing.ROLE_CONFIG_ENV:str(default)}):
                result = model_routing.validate_roles(path=selected)
                self.assertEqual(result["ideation"]["model"], "selected-model")
                cfg = config(selected_url,"selected-model")
                cfg["endpoints"] = {"broken":{"base_url":error_url}, **cfg["endpoints"]}
                selected.write_text(yaml.safe_dump(cfg),encoding="utf-8")
                model_routing._cache.clear()
                configs = Configs(self.root)
                id = next(x["id"] for x in configs.presets()["role_configs"] if x["label"]==selected.name)
                checked = configs.check(id)
                by_id = {x["id"]:x for x in checked["endpoints"]}
                self.assertFalse(checked["ok"])
                self.assertFalse(by_id["broken"]["ok"])
                self.assertTrue(by_id["local"]["ok"])
                self.assertEqual(by_id["local"]["models"], ["selected-model"])

    def test_passive_model_view_strips_url_credentials(self):
        cfg = {"endpoints":{"private":{"base_url":"https://user:password@example.com/v1?api_key=hidden&safe=1", "api_key_env":"STUDIO_TEST_KEY"}},
               "roles":{"ideation":{"endpoint":"private","model":"chosen"}}}
        (self.root/"ais_roles.yaml").write_text(yaml.safe_dump(cfg),encoding="utf-8")
        configs = Configs(self.root)
        with patch.dict("os.environ", {"STUDIO_TEST_KEY":"secret-value", model_routing.ROLE_CONFIG_ENV:str(self.root/"ais_roles.yaml")}):
            view = configs.models(configs.presets()["selected_role_config_id"])
            serialized = json.dumps(view)
            for secret in ("password", "hidden", "secret-value", "user:"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(view["endpoints"][0]["url"], "https://example.com/v1?safe=1")
            self.assertEqual(view["endpoints"][0]["credential"], {"env":"STUDIO_TEST_KEY","present":True})

    def test_cborg_model_listing_uses_default_credential_and_explicit_compatible_url(self):
        with endpoint("cborg-served-model", expected_api_key="fixture-cborg-key") as url:
            target = self.root / "ais_roles.yaml"
            target.write_text(yaml.safe_dump({
                "endpoints": {"cborg": {"provider": "cborg", "base_url": url, "provides": ["text"]}},
                "roles": {"ideation": {"endpoint": "cborg", "model": "cborg-served-model", "requires": ["text"]}},
            }), encoding="utf-8")
            with patch.dict("os.environ", {model_routing.ROLE_CONFIG_ENV: str(target), "CBORG_API_KEY": "fixture-cborg-key"}):
                configs = Configs(self.root)
                selected = configs.presets()["selected_role_config_id"]
                listed = configs.endpoint_models(selected, "cborg")
                self.assertTrue(listed["ok"], listed["error"])
                self.assertEqual(listed["models"], ["cborg-served-model"])
                checked = configs.check(selected)
                self.assertTrue(checked["ok"], checked)
                self.assertNotIn("fixture-cborg-key", json.dumps(checked))
