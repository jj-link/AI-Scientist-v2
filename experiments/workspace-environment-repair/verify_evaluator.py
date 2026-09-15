"""Verify shared image pins with official negative/reference controls; no model access.

Reference patches stay in the trusted evaluator execution, never model workspaces.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time
import traceback

import docker
from datasets import load_from_disk

PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')
ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')
NEGATIVE_PATCH = '''diff --git a/workspace_environment_control.py b/workspace_environment_control.py
new file mode 100644
--- /dev/null
+++ b/workspace_environment_control.py
@@ -0,0 +1 @@
+ENVIRONMENT_CONTROL = True
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--protocol', required=True)
    parser.add_argument('--execution-id', required=True)
    args = parser.parse_args()
    source = PROJECT / 'ai_scientist/swebench_study.py'
    spec = importlib.util.spec_from_file_location('evaluator_control_study', source)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    assert study.SAFE_ID.fullmatch(args.execution_id)
    configured = (PROJECT / args.protocol).resolve()
    assert configured.is_relative_to(PROJECT)
    raw = configured.read_bytes()
    protocol = json.loads(raw)
    protocol_sha = study.validate_protocol(protocol, raw)
    directory = ROOT / 'runs' / args.execution_id
    directory.mkdir(mode=0o700, exist_ok=False)
    (directory / 'trials').mkdir()
    frozen = directory / 'frozen-protocol.json'
    study.atomic(frozen, raw)
    row = next(row for row in protocol['cohort'] if row['instance_id'] == 'astropy__astropy-8707')
    assert study.file_digest(protocol['dataset']['records_path']) == protocol['dataset']['records_sha256']
    reference = next(item for item in load_from_disk(str(ROOT / 'harness/dataset/test'))
                     if item['instance_id'] == row['instance_id'])
    assert reference['base_commit'] == row['base_commit'] and reference['repo'] == row['repo']
    record = next(record for record in study.load(protocol['dataset']['records_path'])
                  if record['instance_id'] == row['instance_id'])
    assert record['base_commit'] == row['base_commit']
    client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
    engine = client.info()
    assert not any(text in (engine['Name'] + engine['OperatingSystem']).lower()
                   for text in ('docker-desktop', 'docker desktop'))
    control = study.Control(directory, client, args.execution_id)
    signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
    report = {'purpose': 'trusted_official_evaluator_environment_controls', 'model_generations': 0,
              'reference_material_used': True, 'reference_material_scope': 'trusted evaluator only; no model workspace',
              'protocol_sha256': protocol_sha, 'runner_sha256': study.file_digest(source),
              'script_sha256': study.file_digest(__file__), 'checks': [], 'passed': False}
    print(json.dumps({'event': 'evaluator_controls_started'}), flush=True)
    try:
        for name, patch, expected in [('negative', NEGATIVE_PATCH, False), ('reference', reference['patch'], True)]:
            assert study.file_digest(source) == report['runner_sha256']
            trial = directory / 'trials' / name
            trial.mkdir()
            identity = dict(row, schema_version=1, execution_id=args.execution_id,
                            protocol_sha256=protocol_sha, runner_sha256=report['runner_sha256'],
                            arm=name, repetition=0,
                            evaluation_id=study.digest((args.execution_id + '/' + name).encode())[:32])
            study.save(trial / 'identity.json', identity)
            study.atomic(trial / 'patch.diff', patch.encode())
            study.save(trial / 'patch-identity.json', {'sha256': study.digest(patch.encode())})
            code, _, stderr, cut = control.command(
                [sys.executable, str(source), '--protocol', str(frozen), '--execution-id', args.execution_id,
                 '--evaluate-trial', str(trial)],
                deadline=time.monotonic() + protocol['budgets']['evaluation_timeout_seconds'] + 60,
                output_path=trial / 'evaluator-stdout.log', limit=study.MAX_RESPONSE, cwd=directory)
            study.atomic(trial / 'evaluator-stderr.log', stderr)
            assert code == 0 and not cut, stderr.decode(errors='replace')
            result = study.load(trial / 'evaluation-result.json')
            sandbox = study.load(trial / 'evaluation-sandbox.json')
            check = {'control': name, 'expected_resolved': expected, 'result': result,
                     'sandbox': sandbox, 'official_report_sha256': study.file_digest(trial / 'official-report.json'),
                     'patch_sha256': study.file_digest(trial / 'patch.diff')}
            report['checks'].append(check)
            assert sandbox['image_id'] == row['image_id'] and sandbox['read_only_root'] and sandbox['network'] == 'none'
            assert result == {'status': 'resolved' if expected else 'unresolved', 'resolved': expected}, result
            print(json.dumps({'event': 'evaluator_control_passed', 'control': name, 'resolved': expected}), flush=True)
        report['passed'] = True
    except Exception:
        report['failure'] = traceback.format_exc()
    finally:
        for container in client.containers.list(all=True, filters={'label': study.LABEL + '=' + args.execution_id}):
            control.remove(container)
        for volume in client.volumes.list(filters={'label': study.LABEL + '=' + args.execution_id}):
            volume.remove(force=True)
        control.closed.set()
        control.thread.join(timeout=5)
        report['owned_containers_remaining'] = len(client.containers.list(all=True, filters={'label': study.LABEL + '=' + args.execution_id}))
        report['owned_volumes_remaining'] = len(client.volumes.list(filters={'label': study.LABEL + '=' + args.execution_id}))
        study.save(directory / 'evaluator-verification.json', report)
        print(json.dumps({'event': 'evaluator_controls_finished', 'passed': report['passed']}), flush=True)
        client.close()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
