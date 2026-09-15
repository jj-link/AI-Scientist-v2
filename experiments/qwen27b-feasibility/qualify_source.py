"""Exercise public APIs inside the real repair sandbox, without any model calls."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import signal
import time
import traceback

import docker

ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')
PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--instance-id', help='Recheck one preselected issue without altering the cohort')
    args = parser.parse_args()
    output = (ROOT / 'evidence' / args.output).resolve()
    if not output.is_relative_to(ROOT / 'evidence'):
        raise ValueError('Qualification output must remain in the owned evidence tree')
    output.mkdir(parents=True, exist_ok=False)
    source = PROJECT / 'ai_scientist/swebench_study.py'
    spec = importlib.util.spec_from_file_location('qualification_study', source)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    runner_sha = study.file_digest(source)
    cohort_path = PROJECT / 'experiments/qwen27b-feasibility/cohort-selection.json'
    selection = json.loads(cohort_path.read_text())
    rows = [row for row in selection['cohort'] if args.instance_id is None or row['instance_id'] == args.instance_id]
    if not rows:
        raise ValueError('Requested issue is outside the preselected development cohort')
    protocol_path = PROJECT / 'experiments/gemma12b-paper-comparison/source-frozen-protocol.json'
    raw = protocol_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == selection['source_protocol_sha256']
    protocol = json.loads(raw)
    client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
    engine = client.info()
    assert not any(text in (engine['Name'] + engine['OperatingSystem']).lower()
                   for text in ('docker-desktop', 'docker desktop'))
    report = {'purpose': 'model_workspace_public_api_smoke_not_benchmark_scoring',
              'runner_sha256': runner_sha, 'cohort_sha256': study.file_digest(cohort_path),
              'qualification_script_sha256': study.file_digest(__file__),
              'resource_policy_source_sha256': selection['source_protocol_sha256'],
              'model_generations': 0, 'reference_material_used': False,
              'checks': [], 'completed': False}
    report_path = output / 'source-qualification.json'
    print(json.dumps({'event': 'source_qualification_started', 'issues': len(rows)}), flush=True)
    try:
        for row in rows:
            assert study.file_digest(source) == runner_sha, 'Runner changed during qualification'
            assert min(shutil.disk_usage(ROOT).free, shutil.disk_usage('/mnt/c').free) >= protocol['resources']['minimum_free_bytes']
            directory = output / row['instance_id']
            directory.mkdir()
            execution_id = 'qwen-source-' + hashlib.sha256((str(output) + row['instance_id']).encode()).hexdigest()[:20]
            control = study.Control(directory, client, execution_id)
            signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
            signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
            workspace = None
            result = {'instance_id': row['instance_id'], 'repo': row['repo'], 'passed': False}
            started = time.monotonic()
            try:
                base, upload = study.snapshot(protocol, row, directory, control)
                workspace = study.Workspace(protocol, row, upload, directory, 'repair', control)
                smoke = SMOKES[row['repo']]
                study.atomic(directory / 'public-api-smoke.py', smoke.encode())
                code, stdout, stderr, truncated = workspace.execute('python -c ' + shlex.quote(smoke), time.monotonic() + 90)
                study.atomic(directory / 'public-api-stdout.txt', stdout)
                study.atomic(directory / 'public-api-stderr.txt', stderr)
                result.update(exit_code=code, output_truncated=truncated,
                              passed=code == 0 and not truncated and b'PASS:' in stdout,
                              source_identity=study.load(directory / 'source-identity.json'),
                              stdout=stdout.decode(errors='replace'), stderr=stderr.decode(errors='replace'))
            except Exception:
                result['failure'] = traceback.format_exc()
            finally:
                if workspace is not None:
                    workspace.close()
                for container in client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution_id}):
                    control.remove(container)
                for volume in client.volumes.list(filters={'label': study.LABEL + '=' + execution_id}):
                    volume.remove(force=True)
                control.closed.set()
                control.thread.join(timeout=5)
                result['elapsed_seconds'] = time.monotonic() - started
                result['owned_containers_remaining'] = len(client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution_id}))
                result['owned_volumes_remaining'] = len(client.volumes.list(filters={'label': study.LABEL + '=' + execution_id}))
                report['checks'].append(result)
                report_path.write_text(json.dumps(report, indent=2) + '\n')
                print(json.dumps({'event': 'source_qualification_case', **{key: result[key] for key in ('instance_id', 'passed', 'elapsed_seconds', 'owned_containers_remaining', 'owned_volumes_remaining')}}), flush=True)
            assert result['owned_containers_remaining'] == result['owned_volumes_remaining'] == 0
        report['completed'] = True
        report['all_passed'] = all(item['passed'] for item in report['checks'])
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'event': 'source_qualification_finished', 'passed': sum(item['passed'] for item in report['checks']), 'total': len(report['checks'])}), flush=True)
    finally:
        client.close()
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
