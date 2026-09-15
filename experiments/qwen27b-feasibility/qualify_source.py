"""Exercise mandatory workspace admission, without model calls or hidden tests."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import time
import traceback

import docker

ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')
PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--cohort', choices=('development', 'original'), default='development')
    parser.add_argument('--instance-id', help='Qualify one pinned issue without selecting new benchmark tasks')
    parser.add_argument('--protocol', help='Separately frozen environment-validation protocol, relative to the project')
    args = parser.parse_args()
    output = (ROOT / 'evidence' / args.output).resolve()
    if not output.is_relative_to(ROOT / 'evidence'):
        raise ValueError('Qualification output must remain in the owned evidence tree')
    output.mkdir(parents=True, exist_ok=False)
    source = PROJECT / 'ai_scientist/swebench_study.py'
    spec = importlib.util.spec_from_file_location('qualification_study', source)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    cohort_path = PROJECT / 'experiments/qwen27b-feasibility/cohort-selection.json'
    selection = json.loads(cohort_path.read_text())
    protocol_path = ((PROJECT / args.protocol).resolve() if args.protocol else
                     PROJECT / 'experiments/gemma12b-paper-comparison/source-frozen-protocol.json')
    if not protocol_path.is_relative_to(PROJECT):
        raise ValueError('Qualification protocol must remain in the project')
    raw = protocol_path.read_bytes()
    study.atomic(output / 'frozen-protocol.json', raw)
    if not args.protocol:
        assert hashlib.sha256(raw).hexdigest() == selection['source_protocol_sha256']
    protocol = json.loads(raw)
    if args.protocol:
        if args.cohort != 'original':
            raise ValueError('An explicit protocol must qualify its own pinned cohort')
        study.validate_protocol(protocol, raw)
    rows = selection['cohort'] if args.cohort == 'development' else protocol['cohort']
    rows = [row for row in rows if args.instance_id is None or row['instance_id'] == args.instance_id]
    if not rows:
        raise ValueError('Requested issue is outside the pinned cohort')
    protocol['cohort'] = rows
    client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
    engine = client.info()
    assert not any(text in (engine['Name'] + engine['OperatingSystem']).lower()
                   for text in ('docker-desktop', 'docker desktop'))
    execution_id = 'workspace-qualification-' + hashlib.sha256(str(output).encode()).hexdigest()[:20]
    control = study.Control(output, client, execution_id)
    signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
    report = {'purpose': 'model_workspace_admission_not_benchmark_scoring',
              'runner_sha256': study.file_digest(source), 'cohort': args.cohort,
              'protocol_sha256': hashlib.sha256(raw).hexdigest(),
              'cohort_sha256': study.digest(study.json_bytes(rows)),
              'qualification_script_sha256': study.file_digest(__file__),
              'source_protocol_sha256': selection['source_protocol_sha256'],
              'model_generations': 0, 'reference_material_used': False,
              'checks': [], 'completed': False, 'all_passed': False}
    started = time.monotonic()
    print(json.dumps({'event': 'source_qualification_started', 'issues': len(rows)}), flush=True)
    try:
        study.prepare_workspaces(protocol, output, control)
        report['all_passed'] = True
    except Exception:
        report['failure'] = traceback.format_exc()
    finally:
        for container in client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution_id}):
            control.remove(container)
        for volume in client.volumes.list(filters={'label': study.LABEL + '=' + execution_id}):
            volume.remove(force=True)
        control.closed.set()
        control.thread.join(timeout=5)
        report['elapsed_seconds'] = time.monotonic() - started
        report['owned_containers_remaining'] = len(client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution_id}))
        report['owned_volumes_remaining'] = len(client.volumes.list(filters={'label': study.LABEL + '=' + execution_id}))
        report['source_identities'] = [study.load(path) for path in sorted((output / 'workspaces').glob('*/source-identity.json'))]
        report['checks'] = [study.load(path) for path in sorted((output / 'workspaces').glob('*/*-qualification.json'))]
        report['completed'] = True
        study.save(output / 'source-qualification.json', report)
        print(json.dumps({'event': 'source_qualification_finished', 'all_passed': report['all_passed'],
                          'passed_workspaces': sum(check['passed'] for check in report['checks']),
                          'expected_workspaces': 2 * len(rows),
                          'owned_containers_remaining': report['owned_containers_remaining'],
                          'owned_volumes_remaining': report['owned_volumes_remaining']}), flush=True)
        client.close()
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
