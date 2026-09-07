"""Codex browser OAuth, with credentials confined to the operating-system vault.

No Codex CLI credentials are read. The JWT payload is used only for account
metadata; trust comes from the HTTPS OAuth exchange, not local JWT decoding.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from filelock import FileLock, Timeout

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
_INSTALLATION = Path(__file__).resolve().parents[1]
# Separate checkouts must not share a rotating grant while using different locks.
DEFAULT_NAMESPACE = "ai-scientist.codex.oauth." + hashlib.sha256(str(_INSTALLATION).encode()).hexdigest()[:16]
_VAULT_USER = "oauth-session"
_VAULT_ERROR = "Codex requires an available secure OS credential vault. Unlock or configure your system vault and retry."
_VAULT_CHUNK_SIZE = 512  # ASCII characters; safely below Windows CredentialBlob limits.
_MAX_VAULT_PARTS = 512


class CodexAuthError(RuntimeError):
    """A credential-safe error suitable for display to a local user."""


class _RefreshExpired(CodexAuthError):
    pass


@dataclass
class _Attempt:
    state: str
    verifier: str
    expires_at: float
    deadline: float
    consumed: bool = False
    server: _CallbackServer | None = None
    timer: threading.Timer | None = None


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def handle_error(self, request, client_address):
        # The standard implementation prints exception/request details to stderr.
        pass


class CodexAuth:
    """A process-local login controller sharing one cross-process vault record.

    ``store`` accepts the keyring get/set/delete_password interface and
    ``transport`` an httpx transport, exclusively for isolated fixtures.
    ``namespace`` and ``lock_path`` must agree in processes sharing credentials.
    """

    def __init__(self, *, store=None, transport=None,
                 namespace: str = DEFAULT_NAMESPACE, lock_path=None,
                 login_timeout: float = 300, http_timeout: float = 30):
        if not 0 < login_timeout <= 600 or not 0 < http_timeout <= 60:
            raise ValueError("OAuth timeouts must be positive and bounded.")
        self._store = store
        self._transport = transport
        self._namespace = namespace
        self._lock_path = Path(lock_path) if lock_path is not None else (
            _INSTALLATION / "ui_data" / "codex" /
            (hashlib.sha256(namespace.encode()).hexdigest() + ".lock")
        )
        self._login_timeout = login_timeout
        self._http_timeout = http_timeout
        self._mutex = threading.RLock()
        self._attempt: _Attempt | None = None
        self._error: str | None = None
        self._generation = 0

    def _vault(self):
        if self._store is None:
            try:
                import keyring
                backend = keyring.get_keyring()
                # Refuse null, fail, chained and third-party file backends even
                # when their priority claims they are usable/secure.
                secure = {
                    ("keyring.backends.Windows", "WinVaultKeyring"),
                    ("keyring.backends.macOS", "Keyring"),
                    ("keyring.backends.SecretService", "Keyring"),
                    ("keyring.backends.libsecret", "Keyring"),
                    ("keyring.backends.kwallet", "DBusKeyring"),
                }
                if (type(backend).__module__, type(backend).__name__) not in secure:
                    raise CodexAuthError(_VAULT_ERROR)
                self._store = backend
            except Exception:
                raise CodexAuthError(_VAULT_ERROR) from None
        return self._store

    @contextmanager
    def _locked(self):
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(self._lock_path), timeout=self._http_timeout + 5)
            lock.acquire()
        except Timeout:
            raise CodexAuthError("Codex authentication is busy in another process. Retry shortly.") from None
        except Exception:
            raise CodexAuthError("Codex authentication lock is unavailable. Check access to ui_data/codex.") from None
        try:
            yield
        finally:
            lock.release()

    def _read_record(self, key=""):
        try:
            service = self._namespace + ("." + key if key else "")
            return self._vault().get_password(service, _VAULT_USER)
        except Exception:
            raise CodexAuthError(_VAULT_ERROR) from None

    def _write_record(self, key, value):
        try:
            service = self._namespace + ("." + key if key else "")
            self._vault().set_password(service, _VAULT_USER, value)
        except Exception:
            raise CodexAuthError(_VAULT_ERROR) from None

    def _delete_record(self, key=""):
        try:
            service = self._namespace + ("." + key if key else "")
            vault = self._vault()
            if vault.get_password(service, _VAULT_USER) is not None:
                vault.delete_password(service, _VAULT_USER)
        except Exception:
            raise CodexAuthError(_VAULT_ERROR) from None

    @staticmethod
    def _manifest(value):
        if not isinstance(value, dict) or set(value) != {"generation", "parts", "digest"}:
            raise ValueError()
        for name, length in (("generation", 32), ("digest", 64)):
            text = value[name]
            if not isinstance(text, str) or len(text) != length or any(c not in "0123456789abcdef" for c in text):
                raise ValueError()
        if type(value["parts"]) is not int or not 1 <= value["parts"] <= _MAX_VAULT_PARTS:
            raise ValueError()
        return value

    def _read_manifest(self):
        raw = self._read_record()
        return self._manifest(json.loads(raw)) if raw is not None else None

    def _remove_chunks(self, manifest):
        if manifest:
            for index in range(manifest["parts"]):
                self._delete_record(f'{manifest["generation"]}.{index}')

    def _recover_pending(self):
        raw = self._read_record("pending")
        if raw is None:
            return
        pending = json.loads(raw)
        old = self._manifest(pending["old"]) if pending["old"] is not None else None
        new = self._manifest(pending["new"])
        current = self._read_manifest()
        if current not in (old, new):
            raise ValueError()
        # The small root record is the commit point. A journal makes interrupted
        # writes/cleanup recoverable without ever exposing partially rotated tokens.
        self._remove_chunks(old if current == new else new)
        self._delete_record("pending")

    def _load(self):
        try:
            self._recover_pending()
            manifest = self._read_manifest()
            if manifest is None:
                return None
            parts = []
            for index in range(manifest["parts"]):
                part = self._read_record(f'{manifest["generation"]}.{index}')
                if not isinstance(part, str) or not 0 < len(part) <= _VAULT_CHUNK_SIZE:
                    raise ValueError()
                parts.append(part)
            raw = "".join(parts)
            if not secrets.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), manifest["digest"]):
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            for name in ("access_token", "refresh_token", "account_id"):
                if not self._safe_token(value.get(name)):
                    raise ValueError()
            expiry = value.get("expires_at")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise ValueError()
            return value
        except CodexAuthError:
            raise
        except Exception:
            raise CodexAuthError("The saved Codex session is invalid. Disconnect and sign in again.") from None

    def _save(self, value):
        try:
            self._recover_pending()
            old = self._read_manifest()
            raw = json.dumps(value, separators=(",", ":"))
            count = (len(raw) + _VAULT_CHUNK_SIZE - 1) // _VAULT_CHUNK_SIZE
            if not 1 <= count <= _MAX_VAULT_PARTS:
                raise ValueError()
            new = {"generation": secrets.token_hex(16), "parts": count,
                   "digest": hashlib.sha256(raw.encode()).hexdigest()}
            self._write_record("pending", json.dumps({"old": old, "new": new}, separators=(",", ":")))
            for index in range(count):
                self._write_record(f'{new["generation"]}.{index}',
                                   raw[index * _VAULT_CHUNK_SIZE:(index + 1) * _VAULT_CHUNK_SIZE])
            self._write_record("", json.dumps(new, separators=(",", ":")))
            self._recover_pending()
        except Exception:
            try:
                self._recover_pending()
            except Exception:
                pass  # Keep the journal for recovery when the vault becomes available.
            raise CodexAuthError(_VAULT_ERROR) from None

    def _delete(self):
        try:
            self._recover_pending()
            self._remove_chunks(self._read_manifest())
            self._delete_record()
        except Exception:
            raise CodexAuthError(_VAULT_ERROR) from None

    @staticmethod
    def _safe_token(value):
        return isinstance(value, str) and 0 < len(value) <= 65536 and all(32 < ord(c) < 127 for c in value)

    @staticmethod
    def _account_id(token):
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            account = payload["https://api.openai.com/auth"]["chatgpt_account_id"]
            if not CodexAuth._safe_token(account):
                raise ValueError()
            return account
        except Exception:
            raise CodexAuthError("Codex did not return usable account metadata. Sign in again.") from None

    def _exchange(self, fields, previous=None):
        try:
            # No environment proxies, redirects or arbitrary token destinations.
            with httpx.Client(timeout=self._http_timeout, follow_redirects=False,
                              trust_env=False, transport=self._transport) as client:
                response = client.post(TOKEN_URL, data={"client_id": CLIENT_ID, **fields})
                if response.status_code != 200:
                    if previous is not None and response.status_code in (400, 401, 403):
                        raise _RefreshExpired("The Codex session has expired or been revoked. Sign in again.")
                    raise CodexAuthError("Codex authorization could not be completed. Retry sign-in.")
                value = response.json()
                access = value.get("access_token")
                refresh = value.get("refresh_token")
                expiry = value.get("expires_in")
                if (not self._safe_token(access) or not self._safe_token(refresh)
                        or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                        or not math.isfinite(expiry) or not 0 < expiry <= 366 * 86400):
                    raise CodexAuthError("Codex returned an invalid authorization response. Sign in again.")
                return {"access_token": access, "refresh_token": refresh,
                        "account_id": self._account_id(access), "expires_at": time.time() + expiry}
        except CodexAuthError:
            raise
        except Exception:
            raise CodexAuthError("Codex authorization service is unavailable. Check your connection and retry.") from None

    def status(self):
        with self._mutex:
            pending = self._attempt is not None
            error = self._error
        try:
            with self._locked():
                value = self._load()
            # An expired access token remains connected while a refresh grant
            # exists; credentials() is the sole network/refresh entry point.
            return {"connected": value is not None, "pending": pending,
                    "error": error, "expires_at": value["expires_at"] if value else None}
        except CodexAuthError as exc:
            return {"connected": False, "pending": pending, "error": str(exc), "expires_at": None}

    @staticmethod
    def _stop(attempt):
        if attempt is not None:
            if attempt.timer is not None:
                attempt.timer.cancel()
            if attempt.server is not None:
                attempt.server.shutdown()
                attempt.server.server_close()

    def _finish(self, attempt, error=None):
        with self._mutex:
            if self._attempt is not attempt:
                return
            self._attempt = None
            self._error = error
        self._stop(attempt)

    def begin_login(self):
        # Fail before opening a listener if the vault cannot even be read.
        with self._locked():
            self._load()
        with self._mutex:
            if self._attempt is not None:
                raise CodexAuthError("A Codex sign-in is already pending. Finish or cancel it first.")
            attempt = _Attempt(secrets.token_urlsafe(32), secrets.token_urlsafe(64),
                               time.time() + self._login_timeout,
                               time.monotonic() + self._login_timeout)
            owner = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass

                def send_error(self, code, message=None, explain=None):
                    self.respond(code, "Request rejected.")

                def respond(self, code, text):
                    body = text.encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    code, text = owner._callback(attempt, self.path,
                                                self.headers.get_all("Host", []),
                                                self.headers.get_all("Origin", []))
                    self.respond(code, text)

            try:
                attempt.server = _CallbackServer(("127.0.0.1", 1455), Callback)
            except OSError:
                raise CodexAuthError("Cannot listen on localhost:1455. Close another OAuth sign-in and retry.") from None
            self._attempt = attempt
            self._error = None
            threading.Thread(target=attempt.server.serve_forever,
                             kwargs={"poll_interval": 0.05}, daemon=True,
                             name="codex-oauth-callback").start()
            attempt.timer = threading.Timer(self._login_timeout, self._finish,
                                            args=(attempt, "Codex sign-in timed out. Start a new sign-in."))
            attempt.timer.daemon = True
            attempt.timer.start()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": CLIENT_ID,
                  "redirect_uri": REDIRECT_URI, "scope": "openid profile email offline_access",
                  "code_challenge": challenge, "code_challenge_method": "S256",
                  "state": attempt.state, "id_token_add_organizations": "true",
                  "codex_cli_simplified_flow": "true", "originator": "ai-scientist"}
        return {"authorization_url": AUTHORIZE_URL + "?" + urlencode(params),
                "expires_at": attempt.expires_at}

    def _callback(self, attempt, path, hosts, origins):
        if (len(hosts) != 1 or hosts[0] not in ("localhost:1455", "127.0.0.1:1455")
                or (origins and origins != ["https://auth.openai.com"])):
            return 403, "Request rejected."
        try:
            url = urlsplit(path)
            if url.scheme or url.netloc or url.fragment or url.path != "/auth/callback":
                return 404, "Request rejected."
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
            state = query.get("state", [])
            code = query.get("code", [])
            errors = query.get("error", [])
            if (len(state) != 1 or not state[0].isascii()
                    or not secrets.compare_digest(state[0], attempt.state)):
                return 400, "Invalid OAuth state."
            if not ((len(code) == 1 and self._safe_token(code[0]) and not errors)
                    or (len(errors) == 1 and not code)):
                return 400, "Invalid OAuth response."
        except (ValueError, UnicodeError):
            return 400, "Invalid OAuth response."
        with self._mutex:
            if self._attempt is not attempt or attempt.consumed or time.monotonic() >= attempt.deadline:
                return 409, "This sign-in is no longer active."
            attempt.consumed = True
        if errors:
            self._finish(attempt, "Codex sign-in was denied. Start a new sign-in to retry.")
            return 400, "Sign-in was not completed."
        try:
            value = self._exchange({"grant_type": "authorization_code", "code": code[0],
                                    "code_verifier": attempt.verifier, "redirect_uri": REDIRECT_URI})
            with self._locked():
                with self._mutex:
                    if self._attempt is not attempt or time.monotonic() >= attempt.deadline:
                        return 409, "This sign-in is no longer active."
                    self._save(value)
                    # Commit and cancellation are serialized by the same mutex.
                    self._attempt = None
                    self._error = None
            self._stop(attempt)
            return 200, "Codex sign-in completed. You can close this window."
        except CodexAuthError as exc:
            self._finish(attempt, str(exc))
            return 503, "Sign-in could not be completed. Return to Studio for details."

    def cancel_login(self):
        with self._mutex:
            attempt = self._attempt
            self._attempt = None
            self._error = None
        self._stop(attempt)
        return self.status()

    def logout(self):
        with self._mutex:
            self._generation += 1
            attempt = self._attempt
            self._attempt = None
            self._error = None
        self._stop(attempt)
        try:
            with self._locked():
                self._delete()
        except CodexAuthError as exc:
            with self._mutex:
                self._error = str(exc)
            raise
        return self.status()

    def close(self):
        """Stop current work without destroying the reusable process singleton."""
        with self._mutex:
            self._generation += 1
            attempt = self._attempt
            self._attempt = None
        self._stop(attempt)

    def credentials(self):
        with self._mutex:
            generation = self._generation
        try:
            with self._locked():
                # Read only after acquiring the shared lock: another worker may
                # already have replaced a single-use refresh token.
                value = self._load()
                if value is None:
                    raise CodexAuthError("Codex is not connected. Sign in through Studio first.")
                if value["expires_at"] <= time.time() + 60:
                    try:
                        refreshed = self._exchange({"grant_type": "refresh_token",
                                                    "refresh_token": value["refresh_token"]}, previous=value)
                    except _RefreshExpired:
                        self._delete()
                        raise
                    with self._mutex:
                        if generation != self._generation:
                            raise CodexAuthError("Codex authentication was cancelled.")
                        self._save(refreshed)
                        value = refreshed
                with self._mutex:
                    if generation != self._generation:
                        raise CodexAuthError("Codex authentication was cancelled.")
                    self._error = None
                return {"access_token": value["access_token"], "account_id": value["account_id"]}
        except CodexAuthError as exc:
            with self._mutex:
                self._error = str(exc)
            raise


_singleton: CodexAuth | None = None
_singleton_lock = threading.Lock()


def get_auth() -> CodexAuth:
    """Return the Studio/CLI authentication controller for this process."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CodexAuth()
            atexit.register(_singleton.close)
        return _singleton
