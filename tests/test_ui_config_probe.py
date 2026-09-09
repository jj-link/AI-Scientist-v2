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
from ai_scientist.ui import model_settings

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
        with endpoint("selected-model") as selected_url, endpoint("error", 500) as error_url:
            model_settings.save_settings_atomic(self.root, {
                "local": {"api_format": "openai", "address": selected_url, "capabilities": ["text"]},
                "broken": {"api_format": "openai", "address": error_url}},
                {"ideation": {"server": "local", "model": "selected-model", "requires": ["text"]}})
            settings = model_settings.current_settings(self.root)
            result = model_routing.validate_roles(settings)
            self.assertEqual(result["ideation"]["model"], "selected-model")
            checked = Configs(self.root).check()
            by_id = {x["id"]: x for x in checked["endpoints"]}
            self.assertFalse(checked["ok"])
            self.assertFalse(by_id["broken"]["ok"])
            self.assertTrue(by_id["local"]["ok"])
            self.assertEqual(by_id["local"]["models"], ["selected-model"])

    def test_passive_model_view_strips_url_credentials(self):
        model_settings.save_settings_atomic(self.root, {"private": {
            "api_format": "openai",
            "address": "https://user:password@example.com/v1?api_key=hidden&safe=1",
            "credential_env": "STUDIO_TEST_KEY"}},
            {"ideation": {"server": "private", "model": "chosen"}})
        configs = Configs(self.root)
        with patch.dict("os.environ", {"STUDIO_TEST_KEY": "secret-value"}):
            view = configs.models()
            serialized = json.dumps(view)
            for secret in ("password", "hidden", "secret-value", "user:"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(view["endpoints"][0]["url"], "https://example.com/v1?safe=1")
            self.assertEqual(view["endpoints"][0]["credential"], {"env":"STUDIO_TEST_KEY","present":True})

    def test_cborg_model_listing_uses_default_credential_and_explicit_compatible_url(self):
        with endpoint("cborg-served-model", expected_api_key="fixture-cborg-key") as url:
            model_settings.save_settings_atomic(self.root, {"cborg": {
                "api_format": "cborg", "address": url, "capabilities": ["text"]}},
                {"ideation": {"server": "cborg", "model": "cborg-served-model", "requires": ["text"]}})
            with patch.dict("os.environ", {"CBORG_API_KEY": "fixture-cborg-key"}):
                configs = Configs(self.root)
                listed = configs.endpoint_models("cborg")
                self.assertTrue(listed["ok"], listed["error"])
                self.assertEqual(listed["models"], ["cborg-served-model"])
                checked = configs.check()
                self.assertTrue(checked["endpoints"][0]["ok"], checked)
                self.assertNotIn("fixture-cborg-key", json.dumps(checked))
