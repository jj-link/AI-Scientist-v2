"""Single-attempt, native SWE-bench study executor (Python 3.10).

This file deliberately does not import the scientist, its model routing, or BFTS.
Only the trusted evaluator subprocess may read evaluator records. Tool processes
run nonroot under Landlock, in resource-limited, networkless Docker containers.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

ARMS = ["direct", "diagnosis", "notes", "locations"]
LABEL = "ai-scientist.fixed-study"
MAX_ARCHIVE = 2 * 1024**3
MAX_FILE = 64 * 1024**2
MAX_RESPONSE = 16 * 1024**2
EVALUATOR_STORAGE = (("/testbed", 2 * 1024**3),
                     ("/opt/miniconda3/envs/testbed", 2 * 1024**3),
                     ("/root", 512 * 1024**2))
EVALUATOR_SCRATCH = 512 * 1024**2
DOCKER_API_TIMEOUT_SECONDS = 60
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
VALID = {"resolved", "unresolved", "empty_patch", "budget_exhausted"}
SMOKES = {
    'sympy/sympy': '''import sympy
from sympy import FiniteSet, symbols, lambdify
assert sympy.__file__.startswith('/testbed/')
assert FiniteSet(1).is_subset(FiniteSet(1, 2)) is True
x = symbols('x')
assert lambdify(x, x + 1, modules='math')(2) == 3
print('PASS: source SymPy set operations and generated numeric function')
''',
    'django/django': '''import django
from django.conf import settings
assert django.__file__.startswith('/testbed/')
settings.configure(DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}, INSTALLED_APPS=[])
django.setup()
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute('SELECT 6 * 7')
    assert cursor.fetchone() == (42,)
connection.close()
print('PASS: source Django configuration and SQLite execution')
''',
    'pylint-dev/pylint': '''import json, pathlib, subprocess, sys, pylint
assert pylint.__file__.startswith('/testbed/')
source = pathlib.Path('/workspace/qualification.py')
source.write_text('print(missing_name)\\n')
result = subprocess.run([sys.executable, '-m', 'pylint', '--rcfile=/dev/null', '--persistent=n', '--disable=all', '--enable=E0602', '--output-format=json', str(source)], capture_output=True, text=True, timeout=30)
assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
messages = json.loads(result.stdout)
assert [(item['message-id'], item['line']) for item in messages] == [('E0602', 1)], messages
print('PASS: source Pylint detects the deliberately undefined variable')
''',
    'sphinx-doc/sphinx': '''from pathlib import Path
import sphinx
from sphinx.application import Sphinx
assert sphinx.__file__.startswith('/testbed/')
root = Path('/workspace/qualification-docs')
source = root / 'source'
source.mkdir(parents=True)
(source / 'conf.py').write_text("project = 'Qualification'\\nmaster_doc = 'index'\\n")
(source / 'index.rst').write_text('Qualification\\n=============\\n\\nVerified public documentation build.\\n')
app = Sphinx(str(source), str(source), str(root / 'html'), str(root / 'doctrees'), 'html')
app.build(force_all=True)
assert app.statuscode == 0
assert 'Verified public documentation build.' in (root / 'html/index.html').read_text()
print('PASS: source Sphinx builds actual HTML documentation')
''',
    'pydata/xarray': '''import numpy as np
import xarray as xr
assert xr.__file__.startswith('/testbed/')
original = xr.DataArray(np.array([1, 2]), dims='x')
copied = original.copy(deep=True)
copied.values[0] = 7
assert original.values.tolist() == [1, 2]
assert copied.values.tolist() == [7, 2]
print('PASS: source Xarray deep copy preserves independent data')
''',
    'matplotlib/matplotlib': '''from pathlib import Path
import matplotlib
assert matplotlib.__file__.startswith('/testbed/')
matplotlib.use('Agg')
from matplotlib.figure import Figure
figure = Figure()
axes = figure.subplots()
axes.plot([0, 1], [0, 1])
output = Path('/workspace/qualified-plot.png')
figure.savefig(str(output))
assert output.read_bytes().startswith(b'\\x89PNG\\r\\n\\x1a\\n')
print('PASS: source Matplotlib renders a real PNG through Agg')
''',
    'astropy/astropy': '''import astropy
from astropy.io import ascii
assert astropy.__file__.startswith('/testbed/')
table = ascii.read('value\\n1\\n2\\n', format='basic', guess=False)
assert list(table['value']) == [1, 2]
print('PASS: source Astropy parses an ASCII table')
''',
}


# Only runtime outputs are overlaid; tracked source is always exported from Git.
# Shared-library suffixes vary with the pinned interpreter, not model selection.
WORKSPACE_RUNTIME_FILES = {
    "matplotlib/matplotlib": [
        *["lib/matplotlib/" + name + ".cpython-*.so" for name in
          ("_contour", "_image", "_path", "_png", "_qhull", "_tri", "ft2font", "ttconv",
           "backends/_backend_agg", "backends/_tkagg")],
        "lib/matplotlib/mpl-data/matplotlibrc",
        *["lib/matplotlib.egg-info/" + name for name in
          ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "namespace_packages.txt",
           "not-zip-safe", "requires.txt", "top_level.txt")],
        *["lib/matplotlib/backends/web_backend/jquery-ui-1.12.1/" + name for name in
          ("AUTHORS.txt", "LICENSE.txt", "external/jquery/jquery.js", "index.html", "package.json",
           "jquery-ui.css", "jquery-ui.js", "jquery-ui.min.css", "jquery-ui.min.js",
           "jquery-ui.structure.css", "jquery-ui.structure.min.css",
           "jquery-ui.theme.css", "jquery-ui.theme.min.css")],
        *["lib/matplotlib/backends/web_backend/jquery-ui-1.12.1/images/ui-icons_" + color + "_256x240.png"
          for color in ("444444", "555555", "777620", "777777", "cc0000", "ffffff")],
    ],
    "astropy/astropy": [
        *["astropy/" + name + ".cpython-*.so" for name in
          ("_erfa/ufunc", "compiler_version", "convolution/_convolve", "cosmology/scalar_inv_efuncs",
           "io/ascii/cparser", "io/fits/_utils", "io/fits/compression", "io/votable/tablewriter",
           "modeling/_projections", "stats/_stats", "table/_column_mixins", "table/_np_utils",
           "timeseries/periodograms/bls/_impl",
           "timeseries/periodograms/lombscargle/implementations/cython_impl",
           "utils/_compiler", "utils/xml/_iterparser", "wcs/_wcs")],
        "astropy/_erfa/core.py", "astropy/version.py", "astropy/cython_version.py",
        *["astropy.egg-info/" + name for name in
          ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
           "not-zip-safe", "requires.txt", "top_level.txt")],
        "astropy/modeling/src/wcsconfig.h", "astropy/wcs/include/astropy_wcs/docstrings.h",
        "astropy/wcs/include/astropy_wcs/wcsconfig.h", "astropy/wcs/include/wcsconfig.h",
        *["astropy/wcs/include/wcslib/" + name + ".h" for name in
          ("cel", "lin", "prj", "spc", "spx", "tab", "wcs", "wcserr", "wcsmath", "wcsprintf")],
        "astropy_helpers/astropy_helpers/version.py",
        *["astropy_helpers/astropy_helpers.egg-info/" + name for name in
          ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "not-zip-safe", "requires.txt", "top_level.txt")],
    ],
}


# Public baseline tests, deliberately unrelated to hidden grading targets.
WORKSPACE_TESTS = {
    "pylint-dev/pylint": (("pyproject.toml", "setup.cfg"), ["tests/checkers/unittest_misc.py::TestFixme::test_fixme_with_message"]),
    "sphinx-doc/sphinx": (("setup.cfg",), ["tests/test_build_text.py::test_lineblock"]),
    "pydata/xarray": (("setup.cfg",), ["xarray/tests/test_dataarray.py::TestDataArray::test_get_index"]),
    "matplotlib/matplotlib": (("pytest.ini",), [
        "lib/matplotlib/tests/test_agg.py::test_repeated_save_with_alpha",
        "lib/matplotlib/tests/test_png.py::test_imread_png_uint16",
        "lib/matplotlib/tests/test_font_manager.py::test_font_priority"]),
    "astropy/astropy": (("setup.cfg",), ["astropy/io/ascii/tests/test_read.py::test_from_string[force]"]),
}
ASSERTION_CONTROL = "WORKSPACE_ASSERTION_CONTROL"
DJANGO_PUBLIC_TEST = "basic.tests.ModelInstanceCreationTests.test_object_is_not_written_to_database_until_save_was_called"
JUNIT_PROBE = """import json, xml.etree.ElementTree as ET
cases = ET.parse('/workspace/public-tests.xml').getroot().findall('.//testcase')
print('WORKSPACE_JUNIT=' + json.dumps([
    {'name': case.get('name'), 'skipped': case.find('skipped') is not None,
     'errors': [node.text or '' for node in case.findall('error')],
     'failures': [node.text or '' for node in case.findall('failure')]}
    for case in cases]))
"""


class InfrastructureError(RuntimeError):
    pass


class Interrupted(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise InfrastructureError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def atomic(path, data):
    """Publish once, never overwrite even when two processes race."""
    path = Path(path)
    pending = path.with_name(path.name + ".writing")
    with open(pending, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(pending, path)
    pending.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    atomic(path, json_bytes(value))


def load(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def progress(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def process_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def bounded_copy(source, target, count):
    remaining = count
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        require(bool(block), "Truncated archive member")
        target.write(block)
        remaining -= len(block)


def extract_archive(path, destination, *, prefix="", skip_git=False):
    """Streaming extraction; no traversal, devices, hard links, or symlink parents."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    total = 0
    seen = set()
    links = []
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts, "Unsafe archive path")
            parts = name.parts
            if prefix:
                require(parts and parts[0] == prefix, "Unexpected archive root")
                parts = parts[1:]
            if not parts:
                continue
            if skip_git and parts[0] == ".git":
                continue
            require(parts[0] != ".git" or not skip_git, "Unexpected git path")
            relative = Path(*parts)
            require(str(relative) not in seen, "Duplicate archive entry")
            seen.add(str(relative))
            target = destination / relative
            require(all(not parent.is_symlink() for parent in target.parents), "Symlink archive parent")
            target.parent.mkdir(parents=True, exist_ok=True)
            total += member.size
            require(0 <= member.size <= MAX_FILE and total <= MAX_ARCHIVE, "Source archive exceeds storage bound")
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.isfile():
                with archive.extractfile(member) as source, open(target, "xb") as out:
                    bounded_copy(source, out, member.size)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym():
                link = PurePosixPath(member.linkname)
                require(not link.is_absolute(), "Absolute source symlink")
                resolved = (target.parent / member.linkname).resolve()
                require(resolved.is_relative_to(destination.resolve()), "Escaping source symlink")
                links.append((target, member.linkname))
            else:
                raise InfrastructureError("Unsupported archive entry type")
    # Deferred links cannot turn a later extraction into a traversal.
    for target, link in links:
        require(not target.exists() and not target.is_symlink(), "Conflicting archive symlink")
        target.symlink_to(link)
    for target, _ in links:
        require(target.resolve().is_relative_to(destination.resolve()), "Escaping chained symlink")


# This trusted launcher is passed as an exec argument, not placed in the model's
# filesystem. Landlock rules survive exec, fork, subprocesses and user namespaces.
# ABI 3 adds truncate; filesystem access not explicitly granted remains denied.
SANDBOX = r'''
import ctypes, os, resource, sys
libc = ctypes.CDLL(None, use_errno=True)
class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]
class Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
def checked(value):
    if value < 0:
        raise OSError(ctypes.get_errno(), "Sandbox enforcement unavailable")
    return value
abi = checked(libc.syscall(444, 0, 0, 1))
if abi < 3:
    raise RuntimeError("Landlock ABI 3 or newer required")
rights = (1 << 15) - 1
ruleset = Ruleset(rights)
fd = checked(libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0))
read_dir = (1 << 0) | (1 << 2) | (1 << 3)
read_file = (1 << 0) | (1 << 2)
for path in ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt/conda", "/opt/miniconda3", "/testbed", "/workspace", "/dev/shm", "/dev/null", "/dev/urandom", "/dev/random", "/dev/zero", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/alternatives", "/etc/localtime", "/etc/passwd", "/etc/group", "/etc/fonts", "/etc/mime.types", "/etc/hosts", "/etc/nsswitch.conf", "/etc/os-release", "/proc/cpuinfo", "/proc/meminfo", "/proc/stat", "/proc/uptime"]:
    if not os.path.exists(path):
        continue
    allowed = read_dir if os.path.isdir(path) else read_file
    if path in ["/workspace", "/dev/shm"] or (path == "/testbed" and sys.argv[1] == "repair"):
        allowed = rights
    if path == "/dev/null":
        allowed = (1 << 1) | (1 << 2)
    parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
    rule = Beneath(allowed, parent)
    checked(libc.syscall(445, fd, 1, ctypes.byref(rule), 0))
    os.close(parent)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
checked(libc.prctl(38, 1, 0, 0, 0))
checked(libc.syscall(446, fd, 0))
os.close(fd)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (67108864, 67108864))
os.chdir("/testbed")
os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "-c", sys.argv[2]], {
    "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/conda/envs/testbed/bin:/opt/miniconda3/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/workspace", "TMPDIR": "/workspace",
    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null", "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_INDEX": "1", "NO_PROXY": "*", "GIT_TERMINAL_PROMPT": "0"
})
'''


class Control:
    def __init__(self, directory, client, execution_id):
        self.directory = directory
        self.client = client
        self.execution_id = execution_id
        self.stopped = threading.Event()
        self.closed = threading.Event()
        self.children = set()
        self.responses = set()
        self.containers = set()
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def watch(self):
        while not self.closed.wait(0.1):
            if (self.directory / "STOP").exists() or self.stopped.is_set():
                self.stopped.set()
                with self.lock:
                    for response in list(self.responses):
                        with contextlib.suppress(Exception):
                            response.raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                        with contextlib.suppress(Exception):
                            response.close()
                    for child in list(self.children):
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                    for container in list(self.containers):
                        with contextlib.suppress(Exception):
                            self.client.containers.get(container).remove(force=True, v=True)
                return

    def check(self, deadline=None):
        if self.stopped.is_set() or (self.directory / "STOP").exists():
            raise Interrupted("Stop requested")
        if deadline is not None and time.monotonic() >= deadline:
            raise BudgetExhausted("Wall deadline reached")

    def command(self, argv, *, deadline, output_path=None, limit=MAX_RESPONSE, env=None, cwd=None, merge_stderr=False):
        self.check(deadline)
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                                 start_new_session=True, env=env, cwd=cwd)
        with self.lock:
            self.children.add(child)
        commands = self.directory / "commands"
        commands.mkdir(exist_ok=True)
        save(commands / f"{child.pid}-{process_identity(child.pid)}.json",
             {"pid": child.pid, "start": process_identity(child.pid), "pgid": child.pid})
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        if child.stderr is not None:
            selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        out = bytearray()
        err = bytearray()
        truncated = False
        count = 0
        error_count = 0
        target = open(output_path, "xb") if output_path else None
        try:
            while selector.get_map():
                self.check(deadline)
                for key, _ in selector.select(0.1):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        error_count += len(block)
                        truncated |= error_count > 65536
                        err.extend(block[:max(0, 65536 - len(err))])
                    else:
                        count += len(block)
                        if target:
                            require(count <= limit, "Command output exceeds archive/storage limit")
                            target.write(block)
                        else:
                            out.extend(block[:max(0, limit - len(out))])
                            truncated |= count > limit
            code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
            return code, bytes(out), bytes(err), truncated
        finally:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            with self.lock:
                self.children.discard(child)
            selector.close()
            child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()
            if target:
                target.flush()
                os.fsync(target.fileno())
                target.close()

    def remove(self, container):
        with contextlib.suppress(Exception):
            container.remove(force=True, v=True)
        self.containers.discard(container.id)


class Endpoint:
    def __init__(self, protocol, control):
        import requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.target = protocol["target"]
        self.base = self.target["base_url"].rstrip("/")
        self.control = control
        self.server_identity = None
        self.runner_sha256 = file_digest(__file__)

    def request(self, path, data=None, deadline=None, events_path=None):
        deadline = deadline or time.monotonic() + 30
        self.control.check(deadline)
        require(file_digest(__file__) == self.runner_sha256, "Runner source changed during execution")
        if self.server_identity is not None:
            require(process_identity(self.server_identity[0]) == self.server_identity[1], "Native server restarted during execution")
        response = self.session.request("GET" if data is None else "POST", self.base + path,
                                        json=data, timeout=(3, max(0.1, deadline - time.monotonic())), stream=True)
        with self.control.lock:
            self.control.responses.add(response)
        try:
            response.raise_for_status()
            if events_path is not None:
                return self.chat_events(response, events_path, deadline)
            chunks = bytearray()
            for block in response.iter_content(65536):
                self.control.check(deadline)
                chunks.extend(block)
                require(len(chunks) <= MAX_RESPONSE, "Endpoint response exceeds bound")
            return json.loads(chunks)
        finally:
            response.close()
            self.control.responses.discard(response)

    def chat_events(self, response, events_path, deadline):
        message = {"role": "assistant", "content": "", "reasoning_content": ""}
        tools = {}
        result = {"choices": [{"message": message, "finish_reason": None}]}
        pending = bytearray()
        received = 0
        done = False
        with open(events_path, "xb") as evidence:
            for block in response.iter_content(1024):
                self.control.check(deadline)
                received += len(block)
                require(received <= MAX_RESPONSE, "Streaming completion exceeds bound")
                evidence.write(block)
                evidence.flush()
                pending.extend(block)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        done = True
                        continue
                    chunk = json.loads(payload)
                    require("error" not in chunk, "Streaming model request failed")
                    if "system_fingerprint" in chunk:
                        require(chunk["system_fingerprint"] == self.target["system_fingerprint"], "Streaming runtime fingerprint mismatch")
                        result["system_fingerprint"] = chunk["system_fingerprint"]
                    for key in ("usage", "timings", "model"):
                        if chunk.get(key) is not None:
                            result[key] = chunk[key]
                    for choice in chunk.get("choices", []):
                        require(choice.get("index", 0) == 0, "Unexpected multiple completions")
                        delta = choice.get("delta", {})
                        for key in ("content", "reasoning_content"):
                            message[key] += delta.get(key) or ""
                        for update in delta.get("tool_calls", []):
                            item = tools.setdefault(update["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if update.get("id"):
                                item["id"] = update["id"]
                            for key in ("name", "arguments"):
                                item["function"][key] += update.get("function", {}).get(key) or ""
                        if choice.get("finish_reason") is not None:
                            result["choices"][0]["finish_reason"] = choice["finish_reason"]
            os.fsync(evidence.fileno())
        require(done and "usage" in result, "Incomplete streaming response; usage remains unknown")
        if tools:
            message["tool_calls"] = [tools[index] for index in sorted(tools)]
        return result

    def tokens(self, text, deadline=None):
        return self.request("/tokenize", {"content": text, "add_special": False, "parse_special": False}, deadline)["tokens"]

    def bounded(self, text, cap, deadline, truncated=False):
        marker = "\n[tool output truncated]"
        if not truncated and len(self.tokens(text, deadline)) <= cap:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if len(self.tokens(text[:middle] + marker, deadline)) <= cap:
                low = middle
            else:
                high = middle - 1
        result = text[:low] + marker
        require(len(self.tokens(result, deadline)) <= cap, "Tool output token cap failure")
        return result

    def prompt_count(self, body, deadline):
        rendered = self.request("/apply-template", body, deadline)["prompt"]
        tokens = self.request("/tokenize", {"content": rendered, "add_special": True, "parse_special": True}, deadline)["tokens"]
        counted = self.request("/v1/chat/completions/input_tokens", body, deadline)["input_tokens"]
        require(len(tokens) == counted, "Template/tokenizer and authoritative prompt count disagree")
        return counted


def validate_protocol(protocol, raw):
    require(protocol.get("schema_version") == 1 and protocol.get("kind") == "fixed_swebench_repair", "Unsupported protocol")
    require(protocol.get("purpose") in {"comparative_study", "runtime_smoke"}, "Missing study purpose")
    require(protocol.get("arms") == ARMS and protocol.get("repetitions") == 1, "Four fixed arms and one repetition required")
    require(protocol["dataset"]["name"] == "SWE-bench/SWE-bench_Verified" and protocol["dataset"]["revision"] == "78f471bf655a3137b2e8a75af1501690ec009ec3", "Dataset pin mismatch")
    require(HEX.fullmatch(protocol["dataset"]["records_sha256"]), "Invalid records hash")
    cohort = protocol["cohort"]
    require(bool(cohort) and (len(cohort) == 20 or protocol["purpose"] == "runtime_smoke"), "Comparative cohort must have twenty issues")
    require(len({row["instance_id"] for row in cohort}) == len(cohort), "Duplicate cohort issue")
    for row in cohort:
        require(SAFE_ID.fullmatch(row["instance_id"]) and re.fullmatch(r"[0-9a-f]{40}", row["base_commit"]), "Invalid source identity")
        require("@sha256:" in row["image"] and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"]), "Images must be immutable")
    for name in ARMS + ["repair"]:
        require(isinstance(protocol["prompts"].get(name), str) and protocol["prompts"][name].strip(), "Missing frozen prompt")
    b = protocol["budgets"]
    for field in ("output_tokens", "input_tokens", "tool_calls", "seconds"):
        require(all(isinstance(b[phase][field], int) and b[phase][field] > 0 for phase in ("direct", "preparation", "repair")), "Invalid phase budget")
        require(b["direct"][field] == b["preparation"][field] + b["repair"][field], "Direct budget must equal split total")
    require(all(type(b.get(key)) is int and b[key] > 0 for key in ("handoff_tokens", "terminal_output_reserve", "tool_timeout_seconds", "tool_output_tokens", "evaluation_timeout_seconds")), "Invalid fixed cap")
    require("handoff_output_reserve" not in b, "New executions require terminal_output_reserve; archived handoff-only budgets cannot be reinterpreted")
    require(all(b["terminal_output_reserve"] < b[name]["output_tokens"] for name in ("direct", "preparation", "repair")), "Terminal reserve consumes a phase's action allowance")
    if protocol["purpose"] == "comparative_study":
        require(b["direct"] == dict(output_tokens=12288, input_tokens=786432, tool_calls=36, seconds=900), "Comparative direct budget differs from frozen design")
        require(b["preparation"] == dict(output_tokens=4096, input_tokens=262144, tool_calls=12, seconds=300), "Comparative preparation budget differs")
        require(b["repair"] == dict(output_tokens=8192, input_tokens=524288, tool_calls=24, seconds=600), "Comparative repair budget differs")
        require(b["handoff_tokens"] == 1024 and b["terminal_output_reserve"] == 1024 and b["tool_output_tokens"] == 2048, "Comparative token caps differ")
    target = protocol["target"]
    require(target["model"] == "unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL" and target["system_fingerprint"] == "b1-5266f24", "Unexpected tested runtime")
    require(target["context_tokens"] == 262144 and target["temperature"] == 0 and target["seed"] == 7 and target["thinking"] is True, "Sampling/context mismatch")
    require(target["mtp"] == {"type": "draft-mtp", "max_draft_tokens": 4} and target["cache_k"] == target["cache_v"] == "q8_0", "MTP/cache mismatch")
    parsed = urllib.parse.urlparse(target["base_url"])
    require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and not parsed.username and parsed.path in {"", "/"}, "Target must be native loopback")
    require(protocol["resources"]["docker_host"] == "unix:///run/docker.sock", "Unexpected Docker backend")
    require(protocol["resources"]["evaluator_writable_limits_bytes"] ==
            dict((*EVALUATOR_STORAGE, ("/workspace", EVALUATOR_SCRATCH))),
            "Evaluator storage limits differ from the frozen protocol")
    require(protocol["resources"]["evaluator_read_only_root"] is True
            and protocol["resources"]["evaluator_cap_sys_admin"] is False
            and protocol["resources"]["evaluator_command_output_bytes"] == MAX_RESPONSE,
            "Evaluator isolation/transport policy mismatch")
    require(Path(protocol["native"]["run_root"]) == Path("/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime/runs"), "Unexpected native ownership root")
    require(Path(sys.executable).resolve() == Path(protocol["native"]["python"]).resolve(), "Pinned Python environment required")
    require(sys.version_info[:2] == (3, 10) and importlib.metadata.version("swebench") == "5.0.2", "Pinned Python/harness version required")
    return digest(raw)


def runtime_check(protocol, endpoint, directory):
    props = endpoint.request("/props")
    require(props["total_slots"] == 1 and props["default_generation_settings"]["n_ctx"] == protocol["target"]["context_tokens"], "Server slot/context mismatch")
    require(not props.get("is_sleeping") and "5266f24" in str(props["build_info"]), "Server build mismatch or sleeping")
    expected_exe = Path(protocol["native"]["root"]) / "build-cuda128/bin/llama-server"
    candidates = []
    port = urllib.parse.urlparse(protocol["target"]["base_url"]).port or 80
    inodes = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A":
            inodes.add(fields[9])
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "exe").resolve() != expected_exe.resolve():
                continue
            if not any(os.readlink(fd) in {f"socket:[{inode}]" for inode in inodes} for fd in (entry / "fd").iterdir()):
                continue
            candidates.append(entry)
        except (OSError, PermissionError):
            continue
    require(len(candidates) == 1, "Cannot identify exclusive native server process")
    process = candidates[0]
    argv = (process / "cmdline").read_bytes().decode().strip("\0").split("\0")
    required = {"-hf": protocol["target"]["model"], "--spec-type": "draft-mtp", "--spec-draft-n-max": "4", "--cache-type-k": "q8_0", "--cache-type-v": "q8_0", "--cache-type-k-draft": "q8_0", "--cache-type-v-draft": "q8_0", "--parallel": "1", "--ctx-size": "262144", "--fit": "off", "--reasoning": "on"}
    for flag, value in required.items():
        require(argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value, "Live server argument mismatch: " + flag)
    require(all(flag in argv for flag in ("--offline", "--no-context-shift", "--jinja")), "Live server safety flags missing")
    environment = (process / "environ").read_bytes().split(b"\0")
    require(not any(item.startswith(b"GGML_CUDA_ENABLE_UNIFIED_MEMORY=") for item in environment), "Unified memory fallback not permitted")
    save(directory / "runtime-private.json", {"props": props, "argv": argv, "server_pid": int(process.name), "server_start": process_identity(process.name), "server_sha256": file_digest(expected_exe)})
    endpoint.server_identity = (int(process.name), process_identity(process.name))


def image_check(client, row):
    image = client.images.get(row["image"])
    require(image.id == row["image_id"], "Local image identity mismatch")
    require(row["image"] in image.attrs.get("RepoDigests", []), "Pinned digest not present locally")
    require(not image.attrs["Config"].get("Volumes"), "Image-declared anonymous volumes are not permitted")
    return image


def container_options(protocol, execution_id):
    resources = protocol["resources"]
    return dict(network_mode="none", cap_drop=["ALL"], cap_add=["SETUID", "SETGID"],
                security_opt=["no-new-privileges:true"], privileged=False,
                nano_cpus=int(resources["container_cpus"] * 1_000_000_000),
                mem_limit=resources["container_memory_bytes"], memswap_limit=resources["container_memory_bytes"],
                pids_limit=resources["container_pids"], labels={LABEL: execution_id},
                entrypoint=["/bin/sleep"], command=["infinity"], user="0:0",
                working_dir="/", environment={"HOME": "/workspace", "TMPDIR": "/workspace"},
                log_config={"Type": "none"})


def docker_command(control, container, command, deadline, **kwargs):
    return control.command(["docker", "--host", "unix:///run/docker.sock", "exec", "--user", "0:0", container.id, *command], deadline=deadline, **kwargs)


def checked_command(control, argv, deadline, **kwargs):
    code, out, err, truncated = control.command(argv, deadline=deadline, **kwargs)
    require(code == 0 and not truncated, "Trusted command failed: " + err.decode(errors="replace")[:2000])
    return out


def _export_source_tree(source, image_path, commit, base, directory, control, deadline, env, exports, verify_runtime):
    """Verify each original Git tree before flattening its pinned submodules."""
    number = len(exports)
    record = {"path": str(PurePosixPath(image_path).relative_to("/testbed")), "commit": commit}
    exports.append(record)
    code, root, _, cut = docker_command(control, source, ["git", "-C", image_path, "rev-parse", "--show-toplevel"], deadline)
    require(code == 0 and not cut and root.decode().strip() == image_path, "Pinned source repository is not initialized")
    code, tree, _, cut = docker_command(control, source, ["git", "-C", image_path, "rev-parse", commit + "^{tree}"], deadline)
    require(code == 0 and not cut and re.fullmatch(rb"[0-9a-f]{40}\n", tree), "Image does not contain exact source commit")
    if verify_runtime:
        # Check the image before neutralizing export attributes: eol=crlf test
        # fixtures are clean checkouts, not edits to their canonical Git blobs.
        prefix = ["git", "-C", image_path]
        code, image_tree, _, cut = docker_command(control, source, prefix + ["rev-parse", "HEAD^{tree}"], deadline)
        require(code == 0 and not cut and image_tree == tree,
                "Runtime artifact image source differs from pinned source")
        for extra in ([], ["--cached"]):
            code, out, err, cut = docker_command(control, source,
                prefix + ["diff", "--exit-code", "--no-ext-diff", "--no-textconv",
                          "--ignore-submodules=none", *extra, commit, "--"], deadline)
            require(code == 0 and not cut, "Runtime artifact image has modified tracked source at "
                    + image_path + ": " + (out + err).decode(errors="replace")[:2000])
        record["runtime_source_tree_verified"] = True
    code, listing, _, cut = docker_command(control, source, ["git", "-C", image_path, "ls-tree", "-r", "-z", commit], deadline)
    require(code == 0 and not cut, "Cannot enumerate pinned source tree")
    links = []
    for entry in listing.split(b"\0"):
        if entry.startswith(b"160000 "):
            metadata, name = entry.split(b"\t", 1)
            fields = metadata.split()
            path = PurePosixPath(name.decode("utf-8"))
            require(len(fields) == 3 and fields[1] == b"commit" and re.fullmatch(rb"[0-9a-f]{40}", fields[2]), "Invalid Gitlink identity")
            require(path.parts and not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts, "Unsafe Gitlink path")
            links.append((str(path), fields[2].decode()))
    # Submodule .git entries are files; resolve their actual attributes path.
    # This trusted disposable layer never becomes a model workspace.
    attributes = 'cd "$1" && printf \'* -export-ignore -export-subst -filter -working-tree-encoding -text -eol -ident\\n\' > "$(git rev-parse --git-path info/attributes)"'
    code, _, _, cut = docker_command(control, source, ["/bin/bash", "-c", attributes, "source-export", image_path], deadline)
    require(code == 0 and not cut, "Cannot neutralize source export attributes")
    archive = directory / ("base-source.tar" if number == 0 else f"submodule-source-{number:03d}.tar")
    code, _, _, cut = docker_command(control, source, ["git", "-C", image_path, "archive", "--format=tar", commit], deadline, output_path=archive, limit=MAX_ARCHIVE)
    require(code == 0 and not cut, "Exact-source archive failed")
    extract_archive(archive, base)
    require(not (base / ".git").exists() and not (base / ".git").is_symlink(), "Unexpected git metadata in source archive")
    checked_command(control, ["git", "-C", str(base), "init", "--initial-branch=baseline"], deadline, env=env)
    atomic(base / ".git/info/attributes", b"* -filter -working-tree-encoding -text -eol -ident\n")
    # Detached maintenance must not mutate objects during archive or patch capture.
    for args in (["config", "core.autocrlf", "false"], ["config", "gc.auto", "0"],
                 ["config", "maintenance.auto", "false"], ["add", "--force", "--all"]):
        checked_command(control, ["git", "-C", str(base), *args], deadline, env=env)
    for path, child_commit in links:
        checked_command(control, ["git", "-C", str(base), "update-index", "--add", "--cacheinfo", "160000", child_commit, path], deadline, env=env)
    checked_command(control, ["git", "-C", str(base), "commit", "--allow-empty", "--no-gpg-sign", "-m", "Pristine source baseline"], deadline, env=env)
    regenerated = checked_command(control, ["git", "-C", str(base), "rev-parse", "HEAD^{tree}"], deadline, env=env)
    require(regenerated == tree, "Exported source tree does not exactly match pinned commit")
    for path, child_commit in links:
        child = base / path
        if child.exists():
            require(child.is_dir() and not child.is_symlink(), "Gitlink export is not an empty directory")
            child.rmdir()
        _export_source_tree(source, image_path + "/" + path, child_commit, child, directory, control, deadline, env, exports, verify_runtime)
        shutil.rmtree(child / ".git")
        checked_command(control, ["git", "-C", str(base), "update-index", "--force-remove", "--", path], deadline, env=env)
        checked_command(control, ["git", "-C", str(base), "add", "--force", "--all", "--", path], deadline, env=env)
    if links:
        checked_command(control, ["git", "-C", str(base), "commit", "--amend", "--allow-empty", "--no-edit", "--no-gpg-sign"], deadline, env=env)
        regenerated = checked_command(control, ["git", "-C", str(base), "rev-parse", "HEAD^{tree}"], deadline, env=env)
    record.update(tree=tree.decode().strip(), materialized_tree=regenerated.decode().strip(),
                  baseline_commit=checked_command(control, ["git", "-C", str(base), "rev-parse", "HEAD"], deadline, env=env).decode().strip(),
                  archive_sha256=file_digest(archive))
    return record


def _artifact_path(root, name):
    path = PurePosixPath(name)
    require(path.parts and str(path) == name and not path.is_absolute()
            and ".." not in path.parts and ".git" not in path.parts, "Unsafe runtime artifact path")
    target = root.joinpath(*path.parts)
    require(not target.is_symlink() and all(not parent.is_symlink() for parent in target.parents),
            "Symlink runtime artifact path")
    return target


def _restore_runtime_files(source, base, patterns, directory, control, deadline):
    """Reuse only declared build outputs from an image with identical tracked inputs."""
    if not patterns:
        return []
    discover = """import json, pathlib, sys
root = pathlib.Path('/testbed')
names = set()
for pattern in json.loads(sys.argv[1]):
    matches = [path for path in root.glob(pattern) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError('Missing or ambiguous declared runtime artifact: ' + pattern)
    names.add(str(matches[0].relative_to(root)))
print(json.dumps(sorted(names)))
"""
    code, raw, _, cut = docker_command(control, source,
        ["/opt/miniconda3/bin/python", "-I", "-c", discover, json.dumps(patterns)], deadline)
    require(code == 0 and not cut, "Cannot enumerate declared runtime artifacts")
    names = []
    for name in json.loads(raw):
        target = _artifact_path(base, name)
        # Canonical source always wins; runtime provisioning never overwrites it.
        if not target.exists():
            names.append(name)
    if not names:
        return []
    export = """import json, pathlib, sys, tarfile
root = pathlib.Path('/testbed')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for name in json.loads(sys.argv[1]):
        path = root / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise RuntimeError('Symlink runtime artifact')
        info = archive.gettarinfo(str(path), arcname=name)
        if not info.isfile():
            raise RuntimeError('Non-file runtime artifact')
        with path.open('rb') as stream:
            archive.addfile(info, stream)
"""
    archive = directory / "runtime-artifacts.tar"
    code, _, _, cut = docker_command(control, source,
        ["/opt/miniconda3/bin/python", "-I", "-c", export, json.dumps(names)],
        deadline, output_path=archive, limit=MAX_ARCHIVE)
    require(code == 0 and not cut, "Runtime artifact export failed")
    extracted = directory / "runtime-artifacts"
    extract_archive(archive, extracted)
    artifacts = []
    for name in names:
        artifact = _artifact_path(extracted, name)
        require(artifact.is_file(), "Missing declared runtime artifact")
        target = _artifact_path(base, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, target)
        artifacts.append({"path": name, "sha256": file_digest(target), "bytes": target.stat().st_size})
    return artifacts


def _strip_runtime_files(source, artifacts):
    """Reject altered build outputs; never submit environment binaries as repairs."""
    paths = []
    for artifact in artifacts:
        path = _artifact_path(source, artifact["path"])
        require(path.is_file() and path.stat().st_size == artifact["bytes"]
                and file_digest(path) == artifact["sha256"],
                "Model workspace altered a pinned runtime artifact: " + artifact["path"])
        paths.append(path)
    for path in paths:
        path.unlink()


def snapshot(protocol, row, directory, control):
    require(row["repo"] in SMOKES, "No public workspace qualification exists for " + row["repo"])
    image_check(control.client, row)
    source = control.client.containers.create(row["image_id"], read_only=False, **container_options(protocol, control.execution_id))
    control.containers.add(source.id)
    source.start()
    deadline = time.monotonic() + 180
    try:
        base = directory / "base"
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="Frozen baseline", GIT_AUTHOR_EMAIL="baseline@invalid", GIT_COMMITTER_NAME="Frozen baseline", GIT_COMMITTER_EMAIL="baseline@invalid", GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
        exports = []
        patterns = WORKSPACE_RUNTIME_FILES.get(row["repo"], [])
        root = _export_source_tree(source, "/testbed", row["base_commit"], base, directory, control, deadline, env, exports, bool(patterns))
        artifacts = _restore_runtime_files(source, base, patterns, directory, control, deadline)
        upload = directory / "pristine.tar"
        with tarfile.open(upload, "w|", dereference=False) as tar:
            def ownership(info):
                info.uid = info.gid = 1000
                info.uname = info.gname = ""
                return info
            tar.add(base, arcname=".", filter=ownership)
        require(upload.stat().st_size <= MAX_ARCHIVE, "Pristine source exceeds storage bound")
        save(directory / "source-identity.json", {"instance_id": row["instance_id"], "base_commit": row["base_commit"],
             "tree": root["tree"], "materialized_tree": root["materialized_tree"],
             "baseline_commit": root["baseline_commit"], "image_id": row["image_id"],
             "archive_sha256": root["archive_sha256"], "submodules": exports[1:],
             "runtime_source_tree_verified": root.get("runtime_source_tree_verified", False),
             "runtime_artifacts": artifacts, "pristine_sha256": file_digest(upload)})
        return base, upload
    finally:
        control.remove(source)


def _workspace_test_command(repo, negative):
    if repo == "sympy/sympy":
        if not negative:
            return ("PYTHONHASHSEED=0 python -B bin/test --no-colors --verbose --no-subprocess "
                    "--seed=0 sympy/core/tests/test_sympify.py -k test_sympify3")
        probe = """import importlib
from pathlib import Path
path = next(path for path in ('sympy/testing/runtests.py', 'sympy/utilities/runtests.py')
            if Path('/testbed', path).is_file())
runner = importlib.import_module(path[:-3].replace('/', '.'))
reporter = runner.PyTestReporter(verbose=True, colors=False)
tests = runner.SymPyTests(reporter, kw=('test_workspace_assertion_control',), seed=0)
tests._testfiles.append('/workspace/test_workspace_assertion_control.py')
raise SystemExit(0 if tests.test(sort=True) else 1)
"""
        return "PYTHONHASHSEED=0 python -B -c " + shlex.quote(probe)
    if repo == "django/django":
        tests = [DJANGO_PUBLIC_TEST]
        if negative:
            tests.append("test_workspace_assertion_control.WorkspaceAssertionControl.test_assertion_control")
        return ("PYTHONPATH=/testbed:/workspace python -B tests/runtests.py --settings=test_sqlite "
                "--parallel=1 --noinput --verbosity=2 " + shlex.join(tests))
    configs, tests = WORKSPACE_TESTS[repo]
    paths = list(tests)
    if negative:
        paths.append("/workspace/test_workspace_assertion_control.py::test_workspace_assertion_control")
    source = "/testbed/lib" if repo == "matplotlib/matplotlib" else "/testbed"
    # Repository versions move their pytest configuration. Select only a
    # declared existing configuration, never silently run without one.
    select = ("config=; for path in " + shlex.join(["/testbed/" + name for name in configs])
              + '; do if test -f "$path"; then config="$path"; break; fi; done; '
              + 'test -n "$config" || exit 2; ')
    command = select + "PYTHONPATH=" + source + ' MPLCONFIGDIR=/workspace/matplotlib python -B -m pytest -c "$config" ' + shlex.join([
        "--rootdir=/testbed",
        "-q", "-rA", "-p", "no:cacheprovider", "--basetemp=/workspace/public-test-work",
        "--junitxml=/workspace/public-tests.xml", *paths])
    return command + "; status=$?; python -c " + shlex.quote(JUNIT_PROBE) + '; exit "$status"'


def _workspace_test_passed(repo, negative, code, stdout, stderr, truncated):
    if truncated or code != int(negative):
        return False
    text = (stdout + stderr).decode(errors="replace")
    if repo == "sympy/sympy":
        summary = "0 passed, 1 failed" if negative else "1 passed"
        name = "test_workspace_assertion_control" if negative else "test_sympify3"
        return (name in text and re.search(r"tests finished: " + summary + r", in ", text) is not None
                and (not negative or ASSERTION_CONTROL in text))
    if repo == "django/django":
        count = "2 tests" if negative else "1 test"
        summary = "\nFAILED (failures=1)\n" if negative else "\nOK\n"
        return (re.search(r"Ran " + count + r" in [\d.]+s", text) is not None and summary in text
                and (not negative or (ASSERTION_CONTROL in text and "FAIL: test_assertion_control " in text)))
    lines = [line.removeprefix("WORKSPACE_JUNIT=") for line in text.splitlines()
             if line.startswith("WORKSPACE_JUNIT=")]
    if len(lines) != 1:
        return False
    try:
        cases = json.loads(lines[0])
        expected = [path.rsplit("::", 1)[-1] for path in WORKSPACE_TESTS[repo][1]]
        if negative:
            expected.append("test_workspace_assertion_control")
        if sorted(case["name"] for case in cases) != sorted(expected):
            return False
        for case in cases:
            if case["skipped"] or case["errors"]:
                return False
            if negative and case["name"] == "test_workspace_assertion_control":
                if len(case["failures"]) != 1 or ASSERTION_CONTROL not in case["failures"][0]:
                    return False
            elif case["failures"]:
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


class Workspace:
    def __init__(self, protocol, row, upload, directory, mode, control):
        self.control = control
        self.mode = mode
        self.container = self.staging = self.volume = None
        self.closed = False
        self.source_identity = load(upload.parent / "source-identity.json")
        require(mode in {"preparation", "repair"}, "Unknown workspace mode")
        require(all(self.source_identity[key] == row[key] for key in ("instance_id", "base_commit", "image_id")),
                "Workspace source/image identity mismatch")
        require(file_digest(upload) == self.source_identity["pristine_sha256"], "Prepared workspace archive changed")
        try:
            image_check(control.client, row)
            self._start(protocol, row, upload, directory, mode, control)
            self._qualify(row, directory)
        except BaseException:
            self.close()
            raise

    def _start(self, protocol, row, upload, directory, mode, control):
        from docker.types import Mount
        self.volume = control.client.volumes.create(driver="local",
            driver_opts={"type": "tmpfs", "device": "tmpfs", "o": f"size={MAX_ARCHIVE},uid=1000,gid=1000"},
            labels={LABEL: control.execution_id})
        self.qualification = None
        options = container_options(protocol, control.execution_id)
        # Populate an owned volume with only the verified export, never a host bind.
        self.staging = control.client.containers.create(row["image_id"], read_only=True,
            mounts=[Mount("/source", self.volume.name, type="volume", no_copy=True)], **options)
        control.containers.add(self.staging.id)
        self.staging.start()
        with open(upload, "rb") as data:
            require(self.staging.put_archive("/source", data), "Source volume upload failed")
        # Keep this mount alive through patch capture: the local tmpfs volume
        # loses its contents when its last container mount is released.
        self.container = control.client.containers.create(row["image_id"], read_only=True,
            mounts=[Mount("/testbed", self.volume.name, type="volume", read_only=mode == "preparation", no_copy=True)],
            tmpfs={"/workspace": "rw,nosuid,nodev,size=320m,uid=1000,gid=1000,mode=0700"}, **options)
        control.containers.add(self.container.id)
        self.container.start()
        self.container.reload()
        attrs = self.container.attrs
        require(attrs["HostConfig"]["ReadonlyRootfs"] and attrs["HostConfig"]["NetworkMode"] == "none", "Tool sandbox configuration mismatch")
        mounts = attrs["Mounts"]
        volumes = [mount for mount in mounts if mount["Type"] == "volume"]
        require(len(volumes) == 1 and volumes[0]["Name"] == self.volume.name and
                all(mount["Type"] == "volume" or (mount["Type"] == "tmpfs" and mount["Destination"] == "/workspace") for mount in mounts),
                "Unexpected tool container mount")
        code, python, err, cut = docker_command(control, self.container,
            ["/bin/bash", "-c", "for p in /opt/miniconda3/bin/python /opt/conda/bin/python /usr/bin/python3; do if test -x \"$p\"; then printf '%s' \"$p\"; exit 0; fi; done; exit 1"],
            time.monotonic() + 30)
        require(code == 0 and not cut, "No trusted image Python interpreter")
        self.python = python.decode()
        code, head, err, cut = self.execute("git rev-parse HEAD", time.monotonic() + 30)
        expected = self.source_identity["baseline_commit"]
        require(code == 0 and not cut and head.decode().strip() == expected,
                "Model workspace does not contain the exact fresh baseline: " + err.decode(errors="replace")[:1000])
        # access(2) does not enforce Landlock: exercise actual opens instead.
        probe = """import os
assert os.getuid() == 1000
for path in ['/proc/self/environ', '/etc/hostname', '/root', '/eval.sh']:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        pass
    else:
        os.close(fd)
        raise AssertionError('Readable forbidden path: ' + path)
try:
    fd = os.open('/testbed/.sandbox-write-probe', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except OSError:
    assert PREPARATION
else:
    os.close(fd)
    os.unlink('/testbed/.sandbox-write-probe')
    assert not PREPARATION
""".replace("PREPARATION", repr(mode == "preparation"))
        import shlex
        code, out, err, cut = self.execute("python -c " + shlex.quote(probe), time.monotonic() + 30)
        require(code == 0 and not cut, "Landlock/read-only sandbox probe failed: " + err.decode(errors="replace")[:1000])
        save(directory / (mode + "-sandbox.json"), {"container_id": self.container.id, "image_id": row["image_id"], "volume": self.volume.name, "read_only_source": mode == "preparation", "kernel": os.uname().release, "probe_exit_code": code})

    def _qualify(self, row, directory):
        repo = row["repo"]
        report = {"instance_id": row["instance_id"], "repo": repo, "mode": self.mode,
                  "image_id": row["image_id"], "pristine_sha256": self.source_identity["pristine_sha256"],
                  "passed": False, "checks": []}
        report_path = directory / (self.mode + "-qualification.json")
        fixture = ("from django.test import SimpleTestCase\n"
                   "class WorkspaceAssertionControl(SimpleTestCase):\n"
                   "    def test_assertion_control(self):\n"
                   "        self.assertEqual(6 * 7, 43, 'WORKSPACE_ASSERTION_CONTROL')\n"
                   if repo == "django/django" else
                   "def test_workspace_assertion_control():\n"
                   "    assert 6 * 7 == 43, 'WORKSPACE_ASSERTION_CONTROL'\n")
        api = ("from pathlib import Path\n"
               "Path('/workspace/test_workspace_assertion_control.py').write_text(" + repr(fixture) + ")\n" + SMOKES[repo])
        commands = [("api", "python -B -c " + shlex.quote(api)),
                    ("public_tests", _workspace_test_command(repo, False)),
                    ("assertion_control", _workspace_test_command(repo, True))]
        for name, command in commands:
            code, out, err, cut = self.execute(command, time.monotonic() + 180)
            prefix = self.mode + "-qualification-" + name
            atomic(directory / (prefix + "-stdout.txt"), out)
            atomic(directory / (prefix + "-stderr.txt"), err)
            passed = (code == 0 and not cut) if name == "api" else _workspace_test_passed(
                repo, name == "assertion_control", code, out, err, cut)
            report["checks"].append({"name": name, "command": command, "exit_code": code,
                                     "output_truncated": cut, "passed": passed})
            receipt = directory / (prefix + ".json")
            save(receipt, report)
            require(passed, f"Workspace qualification failed for {row['instance_id']} ({self.mode}/{name}); see {receipt}")
        # Public checks must not change source, and their scratch evidence must
        # not be handed to any arm. Every actual model workspace starts clean.
        code, out, err, cut = self.execute("git status --porcelain --untracked-files=all", time.monotonic() + 30)
        require(code == 0 and not cut and not out.strip(), "Workspace qualification modified the source tree")
        cleanup = """from pathlib import Path
import shutil
for path in Path('/workspace').iterdir():
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
"""
        code, _, err, cut = self.execute("python -B -c " + shlex.quote(cleanup), time.monotonic() + 30)
        require(code == 0 and not cut, "Workspace qualification scratch cleanup failed")
        report["passed"] = True
        save(report_path, report)
        self.qualification = report

    def execute(self, command, deadline):
        try:
            return docker_command(self.control, self.container, [self.python, "-I", "-c", SANDBOX, self.mode, command], deadline, limit=262144)
        except BudgetExhausted:
            # Kill every background descendant but retain the owned source volume
            # so already-produced work can still be frozen and evaluated.
            with contextlib.suppress(Exception):
                self.container.kill()
            raise
        except Interrupted:
            self.control.remove(self.container)
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.container:
            self.control.remove(self.container)
        if self.staging:
            self.control.remove(self.staging)
        if self.volume:
            with contextlib.suppress(Exception):
                self.volume.remove(force=True)

    def freeze(self, directory, base):
        # Never trust the model's git config, index, hooks, attributes or history.
        # Pause also prevents background jobs racing the immutable patch capture.
        self.container.reload()
        if self.container.attrs["State"]["Running"]:
            self.container.pause()
        archive = directory / "repair-source.tar"
        stream, _ = self.container.get_archive("/testbed")
        total = 0
        with open(archive, "xb") as output:
            for block in stream:
                self.control.check()
                total += len(block)
                require(total <= MAX_ARCHIVE, "Repair source exceeds storage bound")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        source = directory / "repair-source"
        extract_archive(archive, source, prefix="testbed", skip_git=True)
        _strip_runtime_files(source, self.source_identity["runtime_artifacts"])
        # Reconstruct using a trusted, pristine git database and no model hooks.
        shutil.copytree(base / ".git", source / ".git", symlinks=False)
        deadline = time.monotonic() + 60
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_EXTERNAL_DIFF="", GIT_ATTR_NOSYSTEM="1")
        checked_command(self.control, ["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", "add", "--all"], deadline, env=env)
        patch = checked_command(self.control, ["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", "HEAD"], deadline, env=env).decode("utf-8")
        names = checked_command(self.control, ["git", "-C", str(source), "diff", "--cached", "--name-only", "-z", "HEAD"], deadline, env=env)
        atomic(directory / "patch.diff", patch.encode())
        save(directory / "patch-identity.json", {"sha256": digest(patch.encode()), "changed_files": len([name for name in names.split(b"\0") if name])})
        return patch, len([name for name in names.split(b"\0") if name])


def prepare_workspaces(protocol, directory, control):
    """Admit the entire cohort before any runtime/model request is reachable."""
    root = directory / "workspaces"
    root.mkdir()
    report = {"passed": False, "runner_sha256": file_digest(__file__), "checks": []}
    prepared = {}
    try:
        for row in protocol["cohort"]:
            control.check()
            require(file_digest(__file__) == report["runner_sha256"], "Runner changed during workspace qualification")
            require(shutil.disk_usage(root).free >= protocol["resources"]["minimum_free_bytes"] + 4 * MAX_ARCHIVE,
                    "Insufficient space for workspace qualification")
            case = root / row["instance_id"]
            case.mkdir()
            progress("workspace_qualification_started", instance_id=row["instance_id"])
            base, upload = snapshot(protocol, row, case, control)
            for mode in ("preparation", "repair"):
                workspace = Workspace(protocol, row, upload, case, mode, control)
                try:
                    report["checks"].append(workspace.qualification)
                finally:
                    workspace.close()
            prepared[row["instance_id"]] = (base, upload)
            progress("workspace_qualified", instance_id=row["instance_id"])
        report["passed"] = True
        return prepared
    finally:
        save(root / "qualification.json", report)


def tools_for(arm, preparation):
    execute = {"type": "function", "function": {"name": "execute", "description": "Run one bounded shell command with cwd /testbed. Source is read-only during preparation and writable during repair. HOME and TMPDIR are /workspace, the only persistent scratch directory within this phase; it is discarded between phases. No network, host filesystem, original git history or evaluator access.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}}
    if not preparation:
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        name, description = "finish", "Freeze the current filesystem patch. Call with exactly {}: no description, summary or other arguments. No further edits or turns."
    elif arm == "locations":
        parameters = {"type": "object", "properties": {"locations": {"type": "array", "minItems": 0, "maxItems": 64, "items": {"type": "object", "properties": {"path": {"type": "string"}, "symbol": {"type": "string"}}, "required": ["path", "symbol"], "additionalProperties": False}}}, "required": ["locations"], "additionalProperties": False}
        name, description = "handoff", 'Transfer only existing relative source paths and exact Python qualified function/class names. Methods require dotted Class.method names (including enclosing scopes), not bare method names. Use symbol "" for a file-only location, or locations [] if no location is established. No prose, patches, implementation or copied source code, transcripts, or fenced code/reproductions.'
    elif arm == "diagnosis":
        keys = ["root_cause", "evidence", "cross_file_coordination", "intended_behavior", "uncertainties"]
        parameters = {"type": "object", "properties": {key: {"type": "string"} for key in keys}, "required": keys, "additionalProperties": False}
        name, description = "handoff", "Transfer a code-free diagnosis covering all five fields. Do not include patches, implementation or copied source code, transcripts, hidden reasoning, or fenced code/reproductions. Describe evidence and reproduction observations in prose only."
    else:
        parameters = {"type": "object", "properties": {"notes": {"type": "string"}}, "required": ["notes"], "additionalProperties": False}
        name, description = "handoff", "Transfer concise code-free visible investigation notes. No patches, implementation or copied source code, transcripts, hidden reasoning, or fenced code/reproductions; describe observations in prose only. Diagnosis is optional, not required."
    return [execute, {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}]


def valid_handoff(arm, value, base):
    if arm == "locations":
        if not isinstance(value, dict) or set(value) != {"locations"} or not isinstance(value["locations"], list) or len(value["locations"]) > 64:
            return None
        output = []
        for item in value["locations"]:
            if not isinstance(item, dict) or set(item) != {"path", "symbol"}:
                return None
            path, symbol = item["path"], item["symbol"]
            if not isinstance(path, str) or not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
                return None
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts or str(relative) != path:
                return None
            source = base / path
            if not source.resolve().is_relative_to(base.resolve()) or not source.is_file():
                return None
            if symbol:
                if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", symbol) or source.suffix != ".py":
                    return None
                try:
                    tree = ast.parse(source.read_text(encoding="utf-8"))
                except (UnicodeError, SyntaxError):
                    return None
                names = set()
                def visit(node, parents):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            names.add(".".join(parents + [child.name]))
                            visit(child, parents + [child.name])
                        else:
                            visit(child, parents)
                visit(tree, [])
                if symbol not in names:
                    return None
            output.append({"path": path, "symbol": symbol})
        return json.dumps({"locations": output}, ensure_ascii=False, separators=(",", ":"))
    keys = {"notes"} if arm == "notes" else {"root_cause", "evidence", "cross_file_coordination", "intended_behavior", "uncertainties"}
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(text, str) or not text.strip() for text in value.values()):
        return None
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    # A structured, code-free handoff is the only cross-phase channel. Reject
    # recognizable patch/program syntax rather than silently editing its content.
    if any(re.search(r"```|~~~|diff --git|@@|(?:^|\n)\s*(?:[+-]{3}|def |class |import |from \S+ import |return |if .*:|for .*:)", field) for field in value.values()):
        return None
    return text


def metrics():
    return dict(input_tokens=0, output_tokens=0, tool_calls=0, elapsed_seconds=0.0,
                model_seconds=0.0, prompt_seconds=None, generation_seconds=None,
                tool_seconds=0.0, evaluation_seconds=0.0, max_context_tokens=0,
                peak_gpu_memory_mib=None, draft_tokens=None, accepted_draft_tokens=None,
                changed_files=0, handoff_tokens=0)


class GPUSampler:
    def __init__(self, directory):
        self.path = directory / "gpu-samples.jsonl"
        self.done = threading.Event()
        self.peak = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        with open(self.path, "x", encoding="utf-8") as output:
            while not self.done.is_set():
                try:
                    sample = subprocess.run(["nvidia-smi", "--query-gpu=uuid,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2, check=True)
                    rows = []
                    for line in sample.stdout.splitlines():
                        uuid, memory = line.split(",")
                        rows.append({"uuid": uuid.strip(), "memory_used_mib": int(memory.strip())})
                    value = sum(row["memory_used_mib"] for row in rows)
                    self.peak = value if self.peak is None else max(self.peak, value)
                    output.write(json.dumps({"monotonic": time.monotonic(), "gpus": rows}) + "\n")
                    output.flush()
                except (OSError, ValueError, subprocess.SubprocessError):
                    output.write(json.dumps({"monotonic": time.monotonic(), "available": False}) + "\n")
                    output.flush()
                self.done.wait(0.5)

    def close(self):
        self.done.set()
        self.thread.join(timeout=3)
        return self.peak


def phase(protocol, arm, name, issue, handoff, base, workspace, directory, endpoint, totals):
    preparation = name == "preparation"
    budget = protocol["budgets"][name]
    spent = metrics()
    unavailable = set()
    start = time.monotonic()
    deadline = start + budget["seconds"]
    prompt = protocol["prompts"][arm if preparation or arm == "direct" else "repair"]
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(issue, ensure_ascii=False)}]
    if handoff:
        messages.append({"role": "user", "content": "Visible preparation handoff (the only prior-phase material):\n" + handoff})
    tools = tools_for(arm, preparation)
    result = "budget_exhausted"
    visible_handoff = ""
    trace = open(directory / (name + "-trace.jsonl"), "x", encoding="utf-8")
    request_number = 0
    pending_generation = False
    reserve = protocol["budgets"]["terminal_output_reserve"]
    finalizing = False
    finalization_reason = ""
    terminal = "handoff" if preparation else "finish"
    terminal_tools = [item for item in tools if item["function"]["name"] == terminal]
    terminal_choice = {"type": "function", "function": {"name": terminal}}

    def terminal_notice(remaining, reason):
        instruction = ("Use existing observations; state uncertainty rather than inventing evidence."
                       if preparation else "Use exactly {} as arguments; the current filesystem patch will be frozen.")
        return (f"\nController budget: {remaining} total output tokens remain. {reason} "
                f"No further repository commands are permitted. Call {terminal} now. {instruction}")

    try:
        while True:
            endpoint.control.check(deadline)
            remaining = budget["output_tokens"] - spent["output_tokens"]
            if remaining <= 0:
                break
            if spent["tool_calls"] >= budget["tool_calls"] or remaining <= reserve:
                finalizing = True
                finalization_reason = "Action allowance reached."
            active_tools = terminal_tools if finalizing else tools
            allowance = remaining if finalizing else remaining - reserve
            notice = (terminal_notice(remaining, finalization_reason) if finalizing else
                      f"\nController budget: {remaining} total output tokens and "
                      f"{budget['tool_calls'] - spent['tool_calls']} execute calls remain. "
                      f"At most {allowance} action-output tokens remain; {reserve} output tokens "
                      f"are reserved for the final {terminal}.")
            messages[0]["content"] = prompt + notice
            body = {"model": protocol["target"]["model"], "messages": messages, "tools": active_tools,
                    "tool_choice": terminal_choice if finalizing else "auto",
                    "parallel_tool_calls": False,
                    "temperature": protocol["target"]["temperature"], "seed": protocol["target"]["seed"],
                    "max_tokens": allowance,
                    "stream": True, "stream_options": {"include_usage": True},
                    "cache_prompt": False, "return_tokens": True,
                    "chat_template_kwargs": {"enable_thinking": protocol["target"]["thinking"]}}
            count = endpoint.prompt_count(body, deadline)
            if not finalizing:
                # Count the complete terminal prompt, including its own schema
                # and controller notice, before allowing history to grow.
                terminal_body = dict(body, tools=terminal_tools, tool_choice=terminal_choice, max_tokens=remaining)
                terminal_body["messages"] = [
                    {"role": "system", "content": prompt + terminal_notice(
                        remaining, "Remaining input/context capacity is reserved for finalization.")}
                ] + messages[1:]
                terminal_count = endpoint.prompt_count(terminal_body, deadline)
                # Bound the next assistant/tool turn plus template framing.
                # Output not spent on that turn remains available at finalization.
                terminal_bound = terminal_count + allowance + protocol["budgets"]["tool_output_tokens"] + 1024
                if (spent["input_tokens"] + count + terminal_bound > budget["input_tokens"]
                        or terminal_bound + reserve > protocol["target"]["context_tokens"]):
                    finalizing = True
                    finalization_reason = "Remaining input/context capacity is reserved for finalization."
                    continue
            if count + spent["input_tokens"] > budget["input_tokens"]:
                break
            if count + body["max_tokens"] > protocol["target"]["context_tokens"]:
                break
            reservation = {"phase": name, "number": request_number, "input_tokens": count, "output_token_cap": body["max_tokens"], "body_sha256": digest(json_bytes(body))}
            save(directory / f"{name}-request-{request_number:03d}-reserved.json", reservation)
            trace.write(json.dumps({"request": reservation, "body": body}, ensure_ascii=False) + "\n")
            trace.flush()
            os.fsync(trace.fileno())
            started = time.monotonic()
            pending_generation = True
            try:
                response = endpoint.request("/v1/chat/completions", body, deadline,
                    directory / f"{name}-request-{request_number:03d}-events.sse")
            finally:
                duration = time.monotonic() - started
                spent["model_seconds"] += duration
            save(directory / f"{name}-request-{request_number:03d}-response.json", response)
            require(response.get("system_fingerprint") == protocol["target"]["system_fingerprint"], "Response runtime fingerprint mismatch")
            usage = response["usage"]
            require(usage["prompt_tokens"] == count, "Actual prompt token usage differs from pre-request count")
            generated = usage["completion_tokens"]
            require(isinstance(generated, int) and 0 <= generated <= body["max_tokens"], "Invalid delivered target usage")
            spent["input_tokens"] += count
            spent["output_tokens"] += generated
            spent["max_context_tokens"] = max(spent["max_context_tokens"], count + generated)
            pending_generation = False
            timings = response.get("timings", {})
            for source, target, divisor in (("prompt_ms", "prompt_seconds", 1000), ("predicted_ms", "generation_seconds", 1000), ("draft_n", "draft_tokens", 1), ("draft_n_accepted", "accepted_draft_tokens", 1)):
                if source in timings:
                    require(isinstance(timings[source], (int, float)) and timings[source] >= 0, "Invalid server timing/draft measurement")
                    spent[target] = (spent[target] or 0) + (timings[source] if divisor == 1 else timings[source] / divisor)
                else:
                    unavailable.add(target)
            choice = response["choices"][0]
            message = choice["message"]
            reasoning = message.get("reasoning_content", "")
            visible = message.get("content") or ""
            # These are exact standalone text tokenizations, not falsely labelled
            # server reasoning-token usage (the current server exposes only total).
            trace.write(json.dumps({"standalone_reasoning_text_tokens": len(endpoint.tokens(reasoning, deadline)),
                                    "standalone_visible_text_tokens": len(endpoint.tokens(visible, deadline)),
                                    "delivered_target_tokens": generated,
                                    "reasoning_usage_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens")}) + "\n")
            trace.write(json.dumps({"response": response, "model_seconds": duration}, ensure_ascii=False) + "\n")
            trace.flush()
            request_number += 1
            calls = message.get("tool_calls") or []
            invalid = "invalid_finalization" if len(calls) != 1 else ""
            if not invalid:
                call = calls[0]
                function = call["function"]
                try:
                    arguments = json.loads(function["arguments"])
                except (ValueError, TypeError):
                    invalid = "invalid_tool_arguments"
            if invalid:
                if not finalizing:
                    # Follow an incomplete action with one explicit terminal
                    # request, never a retry or execution of the partial action.
                    messages.append({key: value for key, value in message.items()
                                     if key in {"role", "content", "reasoning_content"}})
                    finalizing = True
                    finalization_reason = f"Action ended with {invalid}; no incomplete tool action was executed."
                    continue
                result = "budget_exhausted" if generated == body["max_tokens"] else invalid
                break
            if function["name"] == "handoff" and preparation:
                visible_handoff = valid_handoff(arm, arguments, base) or ""
                if not visible_handoff:
                    result = "invalid_handoff"
                    break
                handoff_tokens = len(endpoint.tokens(visible_handoff, deadline))
                if handoff_tokens > protocol["budgets"]["handoff_tokens"]:
                    visible_handoff = ""
                    result = "invalid_handoff"
                    break
                spent["handoff_tokens"] = handoff_tokens
                result = "handoff"
                break
            if function["name"] == "finish" and not preparation and arguments == {}:
                result = "finish"
                break
            if finalizing:
                result = "invalid_finalization"
                break
            if function["name"] != "execute" or not isinstance(arguments, dict) or set(arguments) != {"command"} or not isinstance(arguments["command"], str):
                result = "invalid_tool_arguments"
                break
            if spent["tool_calls"] >= budget["tool_calls"]:
                break
            spent["tool_calls"] += 1
            tool_start = time.monotonic()
            try:
                code, out, err, truncated = workspace.execute(arguments["command"], min(deadline, tool_start + protocol["budgets"]["tool_timeout_seconds"]))
            finally:
                spent["tool_seconds"] += time.monotonic() - tool_start
            text = f"exit_code={code}\nstdout:\n" + out.decode(errors="replace") + "\nstderr:\n" + err.decode(errors="replace")
            bounded = endpoint.bounded(text, protocol["budgets"]["tool_output_tokens"], deadline, truncated)
            trace.write(json.dumps({"tool": function, "exit_code": code, "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"), "byte_truncated": truncated, "delivered": bounded}) + "\n")
            trace.flush()
            # Preserve the current tool conversation, including visible reasoning.
            # A new phase creates new messages; only its visible handoff crosses.
            messages.append({key: value for key, value in message.items()
                             if key in {"role", "content", "reasoning_content", "tool_calls"}})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": bounded})
    except BudgetExhausted:
        result = "budget_exhausted"
        if pending_generation:
            result = "infrastructure_failure"
            raise InfrastructureError("Generation deadline left unknown final usage; reserved request cannot be retried")
    except Interrupted:
        result = "interrupted"
        raise
    except Exception:
        result = "interrupted" if endpoint.control.stopped.is_set() else "infrastructure_failure"
        raise
    finally:
        spent["elapsed_seconds"] = time.monotonic() - start
        trace.close()
        for key in unavailable:
            spent[key] = None
        save(directory / (name + "-accounting.json"), {"metrics": spent, "termination_reason": result, "last_request_number": request_number})
        for key in ("input_tokens", "output_tokens", "tool_calls", "model_seconds", "tool_seconds", "handoff_tokens"):
            totals[key] += spent[key]
        totals["max_context_tokens"] = max(totals["max_context_tokens"], spent["max_context_tokens"])
        for key in ("prompt_seconds", "generation_seconds", "draft_tokens", "accepted_draft_tokens"):
            if spent[key] is not None:
                totals[key] = (totals[key] or 0) + spent[key]
    return result, visible_handoff


@contextlib.contextmanager
def bounded_evaluator(protocol, identity, directory, control):
    """Keep official tests/scoring; bound their filesystem and command transport."""
    import docker
    from docker.types import Mount
    import swebench.harness.run_evaluation as official

    client = control.client
    image_check(client, identity)
    writable = (*EVALUATOR_STORAGE, ("/workspace", EVALUATOR_SCRATCH))
    volumes = []
    containers = []
    original_copy = official.copy_to_container
    original_exec = official.exec_run_with_timeout
    original_patch = official.CONTAINER_PATCH_FILE
    original_pull = client.api.pull
    try:
        for _, size in writable:
            control.check()
            volumes.append(client.volumes.create(driver="local",
                driver_opts={"type": "tmpfs", "device": "tmpfs", "o": f"size={size},nosuid,nodev"},
                labels={LABEL: control.execution_id}))
        staging = client.containers.create(identity["image_id"], read_only=True,
            mounts=[Mount(f"/storage{index}", volume.name, type="volume", no_copy=True)
                    for index, volume in enumerate(volumes)],
            **container_options(protocol, control.execution_id))
        containers.append(staging)
        control.containers.add(staging.id)
        staging.start()
        deadline = time.monotonic() + 180
        for index, (source, _) in enumerate(EVALUATOR_STORAGE):
            code, _, err, cut = docker_command(control, staging,
                ["cp", "-a", "--no-preserve=ownership", source + "/.", f"/storage{index}/"],
                deadline)
            require(code == 0 and not cut, "Bounded evaluator filesystem copy failed: " + err.decode(errors="replace"))
        probe = f"import json,os; print(json.dumps([os.statvfs('/storage'+str(i)).f_blocks*os.statvfs('/storage'+str(i)).f_frsize for i in range({len(writable)})]))"
        code, out, _, cut = docker_command(control, staging,
            ["/opt/miniconda3/bin/python", "-I", "-c", probe], deadline)
        require(code == 0 and not cut and json.loads(out) == [size for _, size in writable],
                "Evaluator tmpfs capacity is not enforced")

        class EvaluatorContainer:
            def __init__(self, container):
                self.container = container

            def __getattr__(self, name):
                return getattr(self.container, name)

            def start(self):
                self.container.start()
                self.container.reload()
                host = self.container.attrs["HostConfig"]
                require(host["ReadonlyRootfs"] and host["NetworkMode"] == "none",
                        "Evaluator root/network isolation mismatch")
                mounted = self.container.attrs["Mounts"]
                expected = {volume.name: path for volume, (path, _) in zip(volumes, writable)}
                require({item["Name"]: item["Destination"] for item in mounted if item["Type"] == "volume"} == expected
                        and all(item["Type"] == "volume" for item in mounted),
                        "Unexpected evaluator mount")
                # Keep the official image's dependency/setup commits. Replacing
                # them with base_commit would change the benchmark environment.
                # Only the separate model-visible source export is canonicalized.
                save(directory / "evaluation-sandbox.json", {
                    "container_id": self.id, "image_id": identity["image_id"],
                    "read_only_root": True, "network": "none",
                    "writable_bytes": dict(EVALUATOR_STORAGE),
                    "scratch_bytes": EVALUATOR_SCRATCH,
                    "source_origin": "pinned_official_image_including_setup_commits",
                })

            def exec_run(self, command, *, workdir=None, user=None):
                require(workdir in {None, "/testbed"} and user in {None, "root", "0", "0:0"},
                        "Unexpected official command identity")
                argv = shlex.split(command) if isinstance(command, str) else command
                code, out, err, cut = docker_command(control, self.container, argv,
                    time.monotonic() + 60, limit=MAX_RESPONSE, merge_stderr=True)
                require(not cut, "Official command output exceeded bounded transport")
                return SimpleNamespace(exit_code=code, output=out + err)

        # Collection properties return new objects; adapt explicit instances.
        image_collection = client.images
        container_collection = client.containers
        real_get = image_collection.get
        real_create = container_collection.create

        def no_pull(*args, **kwargs):
            raise InfrastructureError("Implicit evaluator image pull forbidden")

        def guarded_get(image):
            require(image == identity["image"], "Unexpected evaluator image")
            result = real_get(image)
            require(result.id == identity["image_id"], "Evaluator runtime image mismatch")
            return result

        def guarded_create(*args, **kwargs):
            control.check()
            require(not args and kwargs.get("image") == identity["image"], "Unexpected evaluator container image")
            require(not kwargs.get("volumes") and not kwargs.get("mounts") and not kwargs.get("privileged"),
                    "Unexpected evaluator mount/privilege")
            resources = protocol["resources"]
            kwargs.update(image=identity["image_id"], network_mode="none", read_only=True,
                privileged=False, cap_add=[], cap_drop=["SYS_ADMIN"],
                security_opt=["no-new-privileges:true"], labels={LABEL: control.execution_id},
                nano_cpus=int(resources["container_cpus"] * 1_000_000_000),
                mem_limit=resources["container_memory_bytes"], memswap_limit=resources["container_memory_bytes"],
                pids_limit=resources["container_pids"], working_dir="/testbed",
                environment={"HOME": "/root", "TMPDIR": "/workspace"}, log_config={"Type": "none"},
                mounts=[Mount(path, volume.name, type="volume", no_copy=True)
                        for volume, (path, _) in zip(volumes, writable)])
            try:
                client.containers.get(kwargs["name"])
            except docker.errors.NotFound:
                pass
            else:
                raise InfrastructureError("Official evaluator container identity already exists")
            container = real_create(**kwargs)
            containers.append(container)
            control.containers.add(container.id)
            return EvaluatorContainer(container)

        def bounded_copy_to_container(container, source, destination):
            require(str(destination) in {"/eval.sh", "/workspace/patch.diff"},
                    "Unexpected official evaluator file destination")
            destination = PurePosixPath("/workspace") / destination.name
            original_copy(container, source, destination)

        def bounded_evaluation_exec(container, command, timeout=None):
            require(command == "/bin/bash /eval.sh", "Unexpected official evaluation entry point")
            started = time.monotonic()
            try:
                code, out, err, cut = docker_command(control, container,
                    ["/bin/bash", "/workspace/eval.sh"],
                    started + (timeout or protocol["budgets"]["evaluation_timeout_seconds"]),
                    limit=MAX_RESPONSE, merge_stderr=True)
                require(not cut, "Official test output exceeded bounded transport")
                return (out + err).decode(errors="replace"), False, time.monotonic() - started
            except BudgetExhausted:
                with contextlib.suppress(Exception):
                    container.kill()
                raise InfrastructureError("Official test command deadline")

        image_collection.pull = no_pull
        client.api.pull = no_pull
        image_collection.get = guarded_get
        container_collection.create = guarded_create
        official.CONTAINER_PATCH_FILE = "/workspace/patch.diff"
        official.copy_to_container = bounded_copy_to_container
        official.exec_run_with_timeout = bounded_evaluation_exec
        yield SimpleNamespace(images=image_collection, containers=container_collection, api=client.api)
    finally:
        official.copy_to_container = original_copy
        official.exec_run_with_timeout = original_exec
        official.CONTAINER_PATCH_FILE = original_patch
        client.api.pull = original_pull
        for container in reversed(containers):
            control.remove(container)
        for volume in volumes:
            volume.remove(force=True)


def evaluate_child(protocol_path, trial_path):
    """Official 5.0.2 evaluation with pull/runtime guards; no model imports."""
    import docker
    from swebench.harness.utils import make_test_spec
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.grading import get_eval_report
    protocol = load(protocol_path)
    trial = load(Path(trial_path) / "identity.json")
    require(file_digest(protocol_path) == trial["protocol_sha256"] and file_digest(__file__) == trial["runner_sha256"],
            "Evaluator protocol/runner identity changed")
    patch = (Path(trial_path) / "patch.diff").read_text()
    frozen = load(Path(trial_path) / "patch-identity.json")
    require(digest(patch.encode()) == frozen["sha256"], "Patch changed before evaluation")
    require(file_digest(protocol["dataset"]["records_path"]) == protocol["dataset"]["records_sha256"], "Evaluator export changed")
    records = load(protocol["dataset"]["records_path"])
    rows = [row for row in records if row["instance_id"] == trial["instance_id"]]
    require(len(rows) == 1, "Evaluator row identity mismatch")
    row = dict(rows[0])
    row["image"] = trial["image"]
    require(not row.get("image_assets"), "Network/image assets not part of this frozen Python study")
    client = docker.DockerClient(base_url=protocol["resources"]["docker_host"], timeout=DOCKER_API_TIMEOUT_SECONDS)
    image_check(client, trial)
    spec = make_test_spec(row)
    pred = {"instance_id": trial["instance_id"], "model_name_or_path": trial["arm"], "model_patch": patch}
    run_id = trial["evaluation_id"]
    report_dir = Path("logs/run_evaluation") / run_id / trial["arm"] / trial["instance_id"]
    require(not report_dir.exists(), "Official report reuse forbidden without a completed matching trial ledger")
    save(Path(trial_path) / "prediction-private.json", pred)
    if not patch.strip():
        # Ordinary harness dataset filtering skips empty diffs. Do not execute a
        # baseline evaluation and accidentally count a naturally passing issue.
        empty = dict(pred, model_patch=None)
        report = get_eval_report(spec, empty, "", include_tests_status=True)
        save(Path(trial_path) / "official-report.json", report)
        save(Path(trial_path) / "evaluation-result.json", {"status": "empty_patch", "resolved": False})
        return
    expected_name = f"sweb.eval.{trial['instance_id'].lower()}.{run_id}"
    try:
        client.containers.get(expected_name)
    except docker.errors.NotFound:
        pass
    else:
        raise InfrastructureError("Refusing official evaluator's existing-container removal semantics")
    evaluation_control = Control(Path(trial_path).parents[1], client, trial["execution_id"])
    try:
        with bounded_evaluator(protocol, trial, Path(trial_path), evaluation_control) as evaluator_client:
            result = run_instance(spec, pred, evaluator_client, run_id, timeout=protocol["budgets"]["evaluation_timeout_seconds"])
    finally:
        evaluation_control.closed.set()
    if result is None:
        # run_instance returns None for patch application and infrastructure errors.
        # Use its own grading/classification logic, never a homegrown test scorer.
        log = report_dir / "run_instance.log"
        text = log.read_text() if log.exists() else ""
        from swebench.harness.constants import APPLY_PATCH_FAIL
        if APPLY_PATCH_FAIL in text and "EvaluationError" in text:
            save(Path(trial_path) / "evaluation-result.json", {"status": "unresolved", "resolved": False, "reason": "patch_application_failed"})
            return
        raise InfrastructureError("Official evaluator failed without a complete report")
    report = result[1]
    save(Path(trial_path) / "official-report.json", report)
    observation = report[trial["instance_id"]]
    # SWE-bench's infra flags are advisory, not a change to its denominator.
    # Keep the official verdict and unmodified report; controller exceptions
    # still abort above instead of being converted into negative model results.
    require(isinstance(observation.get("resolved"), bool), "Official resolved result missing")
    save(Path(trial_path) / "evaluation-result.json", {"status": "resolved" if observation["resolved"] else "unresolved", "resolved": observation["resolved"]})


def run_trial(protocol, protocol_path, protocol_sha, execution_id, row, issue, arm, directory, endpoint, control, prepared_source):
    identity = {"schema_version": 1, "protocol_sha256": protocol_sha, "runner_sha256": file_digest(__file__), "execution_id": execution_id, "instance_id": row["instance_id"], "repo": row["repo"], "base_commit": row["base_commit"], "image": row["image"], "image_id": row["image_id"], "arm": arm, "repetition": 0, "evaluation_id": digest((execution_id + "/" + row["instance_id"] + "/" + arm).encode())[:32]}
    require(file_digest(protocol_path) == protocol_sha, "Frozen protocol changed during execution")
    require(min(shutil.disk_usage(directory.parent).free, shutil.disk_usage("/mnt/c").free)
            >= protocol["resources"]["minimum_free_bytes"] + 8 * MAX_ARCHIVE,
            "Insufficient free space for bounded trial evidence")
    if directory.exists():
        require((directory / "identity.json").exists() and load(directory / "identity.json") == identity, "Existing attempt immutable identity mismatch")
        require((directory / "completed.json").exists(), "Partial attempt cannot be retried; preserve interrupted accounting")
        completed = load(directory / "completed.json")
        require(completed["identity"] == identity and completed["result_sha256"] == file_digest(directory / "result.json"), "Completed ledger identity mismatch")
        result = load(directory / "result.json")
        require(result["status"] in VALID and file_digest(directory / "patch.diff") == result["patch_sha256"] and result["patch"] == (directory / "patch.diff").read_text(), "Completed patch identity mismatch")
        return result
    directory.mkdir(mode=0o700)
    save(directory / "identity.json", identity)
    start = time.monotonic()
    values = metrics()
    sampler = GPUSampler(directory)
    workspace = None
    handoff = ""
    patch = ""
    reason = "not_started"
    status = "infrastructure_failure"
    resolved = None
    try:
        base, upload = prepared_source
        save(directory / "source-identity.json", load(upload.parent / "source-identity.json"))
        if arm != "direct":
            workspace = Workspace(protocol, row, upload, directory, "preparation", control)
            reason, handoff = phase(protocol, arm, "preparation", issue, "", base, workspace, directory, endpoint, values)
            workspace.close()
            workspace = None
            atomic(directory / "handoff.txt", handoff.encode())
            if reason != "handoff":
                atomic(directory / "patch.diff", b"")
                save(directory / "patch-identity.json", {"sha256": digest(b""), "changed_files": 0})
            else:
                workspace = Workspace(protocol, row, upload, directory, "repair", control)
                reason, _ = phase(protocol, arm, "repair", issue, handoff, base, workspace, directory, endpoint, values)
                patch, values["changed_files"] = workspace.freeze(directory, base)
        else:
            workspace = Workspace(protocol, row, upload, directory, "repair", control)
            reason, _ = phase(protocol, arm, "direct", issue, "", base, workspace, directory, endpoint, values)
            patch, values["changed_files"] = workspace.freeze(directory, base)
        if workspace:
            workspace.close()
            workspace = None
        # No model request is reachable below this immutable patch boundary.
        save(directory / "evaluation-reserved.json", {"evaluation_id": identity["evaluation_id"], "patch_sha256": digest(patch.encode())})
        evaluation_start = time.monotonic()
        try:
            code, _, evaluator_stderr, _ = control.command([sys.executable, str(Path(__file__).resolve()), "--protocol", str(control.directory / "frozen-protocol.json"), "--execution-id", execution_id, "--evaluate-trial", str(directory)], deadline=evaluation_start + protocol["budgets"]["evaluation_timeout_seconds"] + 60, output_path=directory / "evaluator-stdout.log", limit=MAX_RESPONSE, cwd=control.directory)
            atomic(directory / "evaluator-stderr.log", evaluator_stderr)
            require(code == 0, "Official evaluator subprocess failed")
            evaluated = load(directory / "evaluation-result.json")
        except BudgetExhausted as error:
            raise InfrastructureError("Official evaluator wall deadline") from error
        finally:
            values["evaluation_seconds"] = time.monotonic() - evaluation_start
        status = "budget_exhausted" if reason == "budget_exhausted" else evaluated["status"]
        resolved = evaluated["resolved"]
    except Interrupted:
        status, reason = "interrupted", "stop_requested"
        atomic(directory / "failure-private.txt", traceback.format_exc().encode())
    except Exception:
        status, reason = ("interrupted", "stop_requested") if control.stopped.is_set() or (control.directory / "STOP").exists() else ("infrastructure_failure", "native_or_evaluator_failure")
        atomic(directory / "failure-private.txt", traceback.format_exc().encode())
    finally:
        phase_records = [load(path)["metrics"] for path in directory.glob("*-accounting.json")]
        for key in ("prompt_seconds", "generation_seconds", "draft_tokens", "accepted_draft_tokens"):
            measurements = [record[key] for record in phase_records]
            values[key] = sum(measurements) if measurements and all(value is not None for value in measurements) else None
        if workspace:
            workspace.close()
        values["peak_gpu_memory_mib"] = sampler.close()
        values["elapsed_seconds"] = time.monotonic() - start
    result = {"instance_id": row["instance_id"], "repo": row["repo"], "arm": arm, "repetition": 0,
              "status": status, "resolved": resolved, "termination_reason": reason,
              "patch_sha256": digest(patch.encode()), "patch": patch, "handoff": handoff, "metrics": values}
    save(directory / "result.json", result)
    if status in VALID:
        save(directory / "completed.json", {"identity": identity, "result_sha256": file_digest(directory / "result.json"), "patch_sha256": digest(patch.encode())})
    return result


def inherit_continuation(protocol, directory):
    """Import only an audited completed prefix, never an interrupted model call."""
    if "continuation" not in protocol:
        return [], None
    pins = protocol["continuation"]
    require(isinstance(pins, dict) and set(pins) ==
            {"execution_id", "safe_results_sha256", "protocol_sha256", "runner_sha256"},
            "Continuation requires exactly four predecessor pins")
    require(isinstance(pins["execution_id"], str) and SAFE_ID.fullmatch(pins["execution_id"]),
            "Invalid continuation execution ID")
    require(all(isinstance(pins[key], str) and HEX.fullmatch(pins[key])
                for key in ("safe_results_sha256", "protocol_sha256", "runner_sha256")),
            "Invalid continuation digest")
    require(pins["runner_sha256"] == file_digest(__file__),
            "Continuation runner changed; a new study is required instead of mixing workspace policies")
    root = Path(protocol["native"]["run_root"]).resolve()
    native_root = Path(protocol["native"]["root"]).resolve()
    directory = Path(directory).resolve()
    source = root / pins["execution_id"]
    require(root.is_relative_to(native_root) and directory.parent == root
            and source != directory and not source.is_symlink()
            and source.resolve().parent == root and source.is_dir(),
            "Continuation source must be a distinct contained native execution")

    def artifact(parent, name):
        path = parent / name
        require(not path.is_symlink() and path.is_file()
                and path.resolve().parent == parent.resolve(),
                "Missing or escaped continuation artifact: " + name)
        return path

    owner = load(artifact(source, "owner.json"))
    require(type(owner.get("pid")) is int and owner["pid"] > 0
            and isinstance(owner.get("start"), str) and owner["start"]
            and process_identity(owner["pid"]) != owner["start"],
            "Continuation predecessor has a live or invalid owner")
    execution = {key: pins[key] for key in ("execution_id", "protocol_sha256", "runner_sha256")}
    require(load(artifact(source, "execution.json")) == execution,
            "Continuation execution identity mismatch")
    frozen_path = artifact(source, "frozen-protocol.json")
    safe_path = artifact(source, "safe_results.json")
    require(file_digest(frozen_path) == pins["protocol_sha256"]
            and file_digest(safe_path) == pins["safe_results_sha256"],
            "Continuation publication pins mismatch")
    frozen, safe = load(frozen_path), load(safe_path)
    science = ("kind", "purpose", "dataset", "target", "cohort", "arms", "repetitions",
               "budgets", "resources", "prompts", "interpretation", "conversation_policy")
    require(all(key in frozen and key in protocol and frozen[key] == protocol[key]
                for key in science), "Continuation changes frozen scientific fields")
    require(all(frozen["native"][key] == protocol["native"][key] for key in ("root", "run_root")),
            "Continuation native root mismatch")
    require(safe.get("schema_version") == 1 and safe.get("completed") is False
            and not (source / "completed.json").exists()
            and safe.get("execution_id") == pins["execution_id"]
            and safe.get("protocol_sha256") == pins["protocol_sha256"]
            and safe.get("protocol_id") == frozen["protocol_id"]
            and safe.get("runtime", {}).get("runner_sha256") == pins["runner_sha256"],
            "Continuation requires an incomplete pinned publication")
    require(safe.get("cohort") == [{"instance_id": row["instance_id"], "repo": row["repo"]}
                                  for row in protocol["cohort"]]
            and safe.get("arms") == ARMS and safe.get("repetitions") == 1,
            "Continuation publication cohort mismatch")
    schedule = [(row, arm) for index, row in enumerate(protocol["cohort"])
                for arm in ARMS[index % 4:] + ARMS[:index % 4]]
    results = safe.get("trials")
    require(isinstance(results, list) and 1 < len(results) <= len(schedule),
            "Continuation requires a completed prefix and one failed tail")
    trials = source / "trials"
    require(not trials.is_symlink() and trials.is_dir() and trials.resolve().parent == source,
            "Continuation trial root escaped")
    keys = [row["instance_id"] + "--" + arm for row, arm in schedule[:len(results)]]
    require({path.name for path in trials.iterdir()} == set(keys),
            "Continuation contains attempts outside the exact schedule prefix")
    destination = directory / "trials"
    require(not destination.is_symlink() and destination.is_dir()
            and not any(destination.iterdir()), "Continuation destination trials must be empty")
    core = ("identity.json", "result.json", "patch.diff", "completed.json")
    copies = []
    for index, (result, (row, arm)) in enumerate(zip(results, schedule)):
        trial = trials / keys[index]
        require(not trial.is_symlink() and trial.is_dir() and trial.resolve().parent == trials,
                "Continuation trial path escaped")
        identity = {"schema_version": 1, **execution, "instance_id": row["instance_id"],
                    "repo": row["repo"], "base_commit": row["base_commit"], "image": row["image"],
                    "image_id": row["image_id"], "arm": arm, "repetition": 0,
                    "evaluation_id": digest((pins["execution_id"] + "/" + row["instance_id"] + "/" + arm).encode())[:32]}
        require(load(artifact(trial, "identity.json")) == identity
                and load(artifact(trial, "result.json")) == result
                and all(result.get(key) == identity[key] for key in ("instance_id", "repo", "arm", "repetition")),
                "Continuation trial identity or ordered result mismatch")
        if index < len(results) - 1:
            contents = {name: artifact(trial, name).read_bytes() for name in core}
            ledger = json.loads(contents["completed.json"])
            require(result.get("status") in VALID and ledger.get("identity") == identity
                    and json.loads(contents["identity.json"]) == identity
                    and json.loads(contents["result.json"]) == result
                    and ledger.get("result_sha256") == digest(contents["result.json"])
                    and ledger.get("patch_sha256") == result.get("patch_sha256") == digest(contents["patch.diff"])
                    and contents["patch.diff"].decode("utf-8") == result.get("patch"),
                    "Continuation completed ledger or patch mismatch")
            copies.append((trial, contents))
        else:
            allowed = {"identity.json", "result.json", "base-source.tar", "base",
                       "source-identity.json", "pristine.tar", "gpu-samples.jsonl", "failure-private.txt"}
            require(all(path.name in allowed and not path.is_symlink()
                        and (path.is_dir() if path.name == "base" else path.is_file())
                        for path in trial.iterdir()),
                    "Continuation failed tail contains phase or unknown evidence")
            values = result.get("metrics", {})
            nullable = {"prompt_seconds", "generation_seconds", "draft_tokens", "accepted_draft_tokens"}
            require(set(values) == set(metrics())
                    and all(values[key] == 0 for key in values
                            if key not in nullable | {"elapsed_seconds", "peak_gpu_memory_mib"})
                    and all(values[key] is None or values[key] == 0 for key in nullable)
                    and result.get("status") == "infrastructure_failure"
                    and result.get("resolved") is None and result.get("patch") == ""
                    and result.get("patch_sha256") == digest(b"") and result.get("handoff") == "",
                    "Continuation failed tail has model, tool, handoff or patch accounting")
    # Validate every source before publishing any inherited trial. Preserve bytes
    # and original execution identities; workspaces and partial evidence stay put.
    require(file_digest(safe_path) == pins["safe_results_sha256"]
            and file_digest(frozen_path) == pins["protocol_sha256"]
            and load(source / "execution.json") == execution
            and load(source / "owner.json") == owner
            and process_identity(owner["pid"]) != owner["start"],
            "Continuation predecessor changed during audit")
    provenance = {**pins, "inherited_trials": len(copies),
                  "failed_pre_model_trial": {key: results[-1][key] for key in ("instance_id", "arm", "repetition")},
                  "model_generations_repeated": 0}
    for trial, contents in copies:
        target = destination / trial.name
        target.mkdir(mode=0o700)
        for name, content in contents.items():
            atomic(target / name, content)
        save(target / "source-provenance.json", {**pins, "trial": trial.name})
    return results[:-1], provenance


def stop_execution(protocol, execution_id):
    import docker
    directory = Path("/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime/runs") / execution_id
    if directory.is_dir():
        if (directory / "execution.json").exists():
            require(load(directory / "execution.json")["execution_id"] == execution_id, "Execution ownership mismatch")
        with open(directory / "STOP", "a"):
            pass
    client = docker.DockerClient(base_url="unix:///run/docker.sock", timeout=2)
    label = {"label": f"{LABEL}={execution_id}"}
    started = time.monotonic()
    quiet_since = None
    while time.monotonic() - started < 30:
        # Refresh ownership: a command may have been starting when STOP arrived.
        paths = list((directory / "commands").glob("*.json"))
        if (directory / "owner.json").exists():
            paths.append(directory / "owner.json")
        owners = [load(path) for path in paths]
        groups = {item["pgid"]: item["start"] for item in owners
                  if item.get("start") and item.get("pgid") == item.get("pid")}
        processes = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = (entry / "stat").read_text()
                fields = text[text.rindex(")") + 2:].split()
                processes[int(entry.name)] = (int(fields[2]), fields[19], fields[0])
            except (OSError, ValueError, IndexError):
                continue
        # An extant process group retains its numeric identity even if its
        # leader has exited. Never signal a reused leader PID.
        groups = {pgid for pgid, start in groups.items()
                  if pgid not in processes or processes[pgid][1] == start}
        live = {pgid for pgid, _, state in processes.values()
                if pgid in groups and state not in {"Z", "X"}}
        for pgid in live:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL if time.monotonic() - started >= 2 else signal.SIGTERM)
        for container in client.containers.list(all=True, filters=label):
            with contextlib.suppress(docker.errors.APIError):
                container.remove(force=True, v=True)
        for volume in client.volumes.list(filters=label):
            with contextlib.suppress(docker.errors.APIError):
                volume.remove(force=True)
        containers = client.containers.list(all=True, filters=label)
        volumes = client.volumes.list(filters=label)
        if not live and not containers and not volumes:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= 1:
                progress("stopped", execution_id=execution_id)
                return
        else:
            quiet_since = None
        time.sleep(0.1)
    raise InfrastructureError("Stop could not verify termination and owned-resource removal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--evaluate-trial", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(sys.platform == "linux" and SAFE_ID.fullmatch(args.execution_id), "Native Linux and safe execution ID required")
    os.umask(0o077)
    if args.stop:
        stop_execution(None, args.execution_id)
        return 0
    protocol_path = Path(args.protocol).resolve()
    raw = protocol_path.read_bytes()
    protocol = json.loads(raw)
    protocol_sha = validate_protocol(protocol, raw)
    if args.evaluate_trial:
        trial_path = Path(args.evaluate_trial).resolve()
        expected_root = Path(protocol["native"]["run_root"]).resolve() / args.execution_id / "trials"
        require(trial_path.is_relative_to(expected_root), "Evaluator trial path outside owned execution")
        evaluate_child(protocol_path, trial_path)
        return 0
    # Continuation is audited before Docker/model imports or runtime requests.
    import fcntl
    root = Path(protocol["native"]["run_root"]).resolve()
    require(root.is_relative_to(Path(protocol["native"]["root"]).resolve()), "Run root outside native runtime")
    root.mkdir(parents=True, exist_ok=True)
    model_lock = open(root / "gemma-study.lock", "a+b")
    fcntl.flock(model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    directory = root / args.execution_id
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = open(directory / "execution.lock", "a+b")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # The complete native process tree has an identifiable, isolated kill group.
    if os.getpgrp() != os.getpid():
        os.setsid()
    identity = {"execution_id": args.execution_id, "protocol_sha256": protocol_sha, "runner_sha256": file_digest(__file__)}
    if (directory / "execution.json").exists():
        require(load(directory / "execution.json") == identity, "Execution identity changed")
    else:
        save(directory / "execution.json", identity)
        atomic(directory / "frozen-protocol.json", raw)
    # owner.json is immutable for an attempt; partial restarts never gain ownership.
    if (directory / "owner.json").exists():
        require((directory / "safe_results.json").exists(), "Partial execution cannot silently resume")
        existing = load(directory / "safe_results.json")
        require(existing["completed"], "Incomplete study cannot silently retry")
        require((directory / "completed.json").exists() and
                load(directory / "completed.json")["safe_results_sha256"] == file_digest(directory / "safe_results.json"),
                "Completed study publication identity mismatch")
        require(len(existing["trials"]) == len(protocol["cohort"]) * 4 and
                {(r["instance_id"], r["arm"], r["repetition"]) for r in existing["trials"]} ==
                {(r["instance_id"], arm, 0) for r in protocol["cohort"] for arm in ARMS},
                "Completed study trial key mismatch")
        for result in existing["trials"]:
            trial = directory / "trials" / (result["instance_id"] + "--" + result["arm"])
            ledger = load(trial / "completed.json")
            require(ledger["result_sha256"] == file_digest(trial / "result.json") and load(trial / "result.json") == result and file_digest(trial / "patch.diff") == result["patch_sha256"], "Completed replay evidence mismatch")
        progress("completed_replay", execution_id=args.execution_id, result=str(directory / "safe_results.json"))
        return 0
    save(directory / "owner.json", {"pid": os.getpid(), "pgid": os.getpgrp(), "start": process_identity(os.getpid())})
    (directory / "trials").mkdir()
    results, continuation = inherit_continuation(protocol, directory)
    inherited_trials = len(results)
    import docker
    client = docker.DockerClient(base_url=protocol["resources"]["docker_host"], timeout=DOCKER_API_TIMEOUT_SECONDS)
    control = Control(directory, client, args.execution_id)
    signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
    endpoint = Endpoint(protocol, control)
    complete = False
    notes = ["Target output usage includes reasoning and tool-call syntax; speculative draft work is separate. Null telemetry means unavailable, not zero.", "GPU memory is sampled device-wide across native visible GPUs, not attributed exclusively to this trial; peaks between samples can be missed.", "Private request reservations retain unknown usage for interrupted requests. Such runs are never scientifically complete.", "Notes-arm diagnosis is allowed; visible notes are retained verbatim for downstream qualitative coding, not automatically claimed as classified.", "Source tool access is kernel-restricted with Landlock; official evaluation uses the installed SWE-bench 5.0.2 scorer after immutable patch freezing."]
    if continuation is not None:
        notes.append("Audited continuation inherits an unchanged completed schedule prefix with original execution identities; only the remaining slots run. The predecessor failed before any model phase or request; no model generations are repeated.")
    try:
        require(min(shutil.disk_usage(root).free, shutil.disk_usage("/mnt/c").free)
                >= protocol["resources"]["minimum_free_bytes"], "Insufficient native/host disk capacity")
        for row in protocol["cohort"]:
            image_check(client, row)
        # Acquisition owns incremental image-growth accounting against the frozen
        # daemon baseline. This runner neither pulls nor builds any image.
        require(file_digest(protocol["dataset"]["records_path"]) == protocol["dataset"]["records_sha256"], "Frozen evaluator export mismatch")
        # Load evaluator records only in trusted parent; project exactly four issue
        # fields. Nothing from rows except this projection reaches Gemma/tools.
        records = load(protocol["dataset"]["records_path"])
        by_id = {row["instance_id"]: row for row in records}
        require(len(by_id) == len(records), "Duplicate evaluator export identity")
        issues = {}
        for row in protocol["cohort"]:
            record = by_id[row["instance_id"]]
            require(record["repo"] == row["repo"] and record["base_commit"] == row["base_commit"], "Cohort/export mismatch")
            issues[row["instance_id"]] = {key: record[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")}
        del records, by_id
        prepared = prepare_workspaces(protocol, directory, control)
        runtime_check(protocol, endpoint, directory)
        slot = 0
        for index, row in enumerate(protocol["cohort"]):
            for arm in ARMS[index % 4:] + ARMS[:index % 4]:
                slot += 1
                if slot <= inherited_trials:
                    continue
                control.check()
                require(file_digest(__file__) == identity["runner_sha256"], "Runner source changed during execution")
                progress("trial_started", instance_id=row["instance_id"], arm=arm, repetition=0)
                trial_dir = directory / "trials" / (row["instance_id"] + "--" + arm)
                result = run_trial(protocol, protocol_path, protocol_sha, args.execution_id, row, issues[row["instance_id"]], arm, trial_dir, endpoint, control, prepared[row["instance_id"]])
                results.append(result)
                progress("trial_finished", instance_id=row["instance_id"], arm=arm, status=result["status"])
                if result["status"] not in VALID:
                    raise InfrastructureError("Trial interrupted or infrastructure failed; remaining trials not attempted")
        complete = len(results) == len(protocol["cohort"]) * 4
    except Exception:
        atomic(directory / "failure-private.txt", traceback.format_exc().encode())
        progress("study_failed", execution_id=args.execution_id, completed_trials=len(results))
    finally:
        with contextlib.suppress(Exception):
            for container in client.containers.list(all=True, filters={"label": f"{LABEL}={args.execution_id}"}):
                control.remove(container)
        with contextlib.suppress(Exception):
            for volume in client.volumes.list(filters={"label": f"{LABEL}={args.execution_id}"}):
                with contextlib.suppress(Exception):
                    volume.remove(force=True)
        control.closed.set()
        endpoint.session.close()
    safe = {"schema_version": 1, "protocol_id": protocol["protocol_id"], "protocol_sha256": protocol_sha, "execution_id": args.execution_id, "completed": complete,
            "cohort": [{"instance_id": row["instance_id"], "repo": row["repo"]} for row in protocol["cohort"]], "arms": ARMS, "repetitions": 1, "trials": results,
            "runtime": {"harness_version": "5.0.2", "runner_sha256": identity["runner_sha256"], "target": protocol["target"],
                        "budgets": protocol["budgets"], "resources": protocol["resources"],
                        "dataset": {key: protocol["dataset"][key] for key in ("name", "revision")},
                        "purpose": protocol["purpose"], "images": [{key: row[key] for key in
                            ("instance_id", "image", "image_id")} for row in protocol["cohort"]]}, "notes": notes}
    qualification = directory / "workspaces/qualification.json"
    if qualification.exists():
        safe["runtime"]["workspace_qualification"] = {
            "passed": load(qualification)["passed"], "sha256": file_digest(qualification)}
    if continuation is not None:
        safe["runtime"]["continuation"] = continuation
    save(directory / "safe_results.json", safe)
    if complete:
        save(directory / "completed.json", {"identity": identity, "safe_results_sha256": file_digest(directory / "safe_results.json")})
    progress("completed" if complete else "incomplete", execution_id=args.execution_id, result=str(directory / "safe_results.json"))
    return 0 if complete else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
