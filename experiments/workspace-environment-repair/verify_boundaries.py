"""Fault-inject prepared workspaces and exercise trusted capture; never call a model."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback

import docker

PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')
ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')


class ForbiddenEndpoint:
    def __init__(self):
        self.calls = 0

    def tokens(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('A model endpoint was reached during a workspace fault')

    request = tokens


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepared', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--instance-id', choices=('matplotlib__matplotlib-14623', 'astropy__astropy-8707'))
    parser.add_argument('--protocol', required=True, help='Environment-validation protocol, relative to the project')
    args = parser.parse_args()
    prepared = (ROOT / 'evidence' / args.prepared).resolve()
    output = (ROOT / 'evidence' / args.output).resolve()
    assert prepared.is_relative_to(ROOT / 'evidence') and output.is_relative_to(ROOT / 'evidence')
    output.mkdir(parents=True, exist_ok=False)
    source = PROJECT / 'ai_scientist/swebench_study.py'
    spec = importlib.util.spec_from_file_location('boundary_study', source)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    qualification = study.load(prepared / 'source-qualification.json')
    assert qualification['all_passed'] and qualification['runner_sha256'] == study.file_digest(source)
    configured = (PROJECT / args.protocol).resolve()
    assert configured.is_relative_to(PROJECT)
    assert study.file_digest(configured) == qualification['protocol_sha256']
    protocol = study.load(configured)
    study.validate_protocol(protocol, configured.read_bytes())
    protocol_path = output / 'frozen-protocol.json'
    study.save(protocol_path, protocol)
    client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
    execution = 'workspace-boundaries-' + hashlib.sha256(str(output).encode()).hexdigest()[:20]
    control = study.Control(output, client, execution)
    signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
    report = {'purpose': 'workspace_fault_injection_and_source_patch_capture', 'model_generations': 0,
              'reference_material_used': False, 'runner_sha256': study.file_digest(source),
              'script_sha256': study.file_digest(__file__), 'checks': [], 'passed': False}
    targets = {'matplotlib__matplotlib-14623': 'lib/matplotlib/ft2font.',
               'astropy__astropy-8707': 'astropy/utils/_compiler.'}
    if args.instance_id:
        targets = {args.instance_id: targets[args.instance_id]}
    print(json.dumps({'event': 'boundary_checks_started'}), flush=True)
    workspace = None
    try:
        for row in protocol['cohort']:
            if row['instance_id'] not in targets:
                continue
            assert study.file_digest(source) == report['runner_sha256']
            case = output / row['instance_id']
            case.mkdir()
            origin = prepared / 'workspaces' / row['instance_id']
            identity = study.load(origin / 'source-identity.json')
            base, upload = origin / 'base', origin / 'pristine.tar'
            artifact = next(item for item in identity['runtime_artifacts']
                            if item['path'].startswith(targets[row['instance_id']]) and item['path'].endswith('.so'))
            original_archive_sha = study.file_digest(upload)
            faulty = case / 'missing-library-input'
            faulty.mkdir()
            faulty_upload = faulty / 'pristine.tar'
            omitted = 0
            with tarfile.open(upload, 'r|') as incoming, tarfile.open(faulty_upload, 'w|') as outgoing:
                for member in incoming:
                    name = str(PurePosixPath(member.name))
                    if name == artifact['path']:
                        omitted += 1
                        continue
                    outgoing.addfile(member, incoming.extractfile(member) if member.isfile() else None)
            assert omitted == 1
            faulty_identity = dict(identity, pristine_sha256=study.file_digest(faulty_upload))
            study.save(faulty / 'source-identity.json', faulty_identity)
            endpoint = ForbiddenEndpoint()
            trial = case / 'fault-trial'
            issue = {key: row[key] for key in ('instance_id', 'repo', 'base_commit')}
            issue['problem_statement'] = 'Workspace fault injection; model generation is forbidden.'
            result = study.run_trial(protocol, protocol_path, study.file_digest(protocol_path), execution,
                                     row, issue, 'direct', trial, endpoint, control, (base, faulty_upload))
            assert result['status'] == 'infrastructure_failure' and result['resolved'] is None
            assert endpoint.calls == 0 and result['metrics']['output_tokens'] == 0
            assert not (trial / 'evaluation-reserved.json').exists()
            receipt = study.load(trial / 'repair-qualification-api.json')
            assert receipt['checks'][-1]['name'] == 'api' and not receipt['checks'][-1]['passed']
            check = {'instance_id': row['instance_id'], 'missing_artifact': artifact['path'],
                     'fault_status': result['status'], 'resolved': result['resolved'],
                     'endpoint_calls': endpoint.calls, 'output_tokens': result['metrics']['output_tokens'],
                     'evaluation_started': False, 'fault_rejected_at': 'workspace_api_admission'}

            capture = case / 'capture'
            capture.mkdir()
            workspace = study.Workspace(protocol, row, upload, capture, 'repair', control)
            probe = "from pathlib import Path\nPath('/testbed/workspace_contract_probe.py').write_text('def repaired_value():\\n    return 42\\n')\n"
            probe += "with Path('/testbed/.gitignore').open('a') as stream:\n    stream.write('\\n!*.so\\n')\n"
            code, _, err, cut = workspace.execute('python -B -c ' + shlex.quote(probe), time.monotonic()+30)
            assert code == 0 and not cut, err
            patch, count = workspace.freeze(capture, base)
            workspace.close()
            workspace = None
            assert count == 2
            applied = case / 'applied-source'
            shutil.copytree(base, applied, symlinks=True)
            subprocess.run(['git', '-C', str(applied), 'apply', str(capture / 'patch.diff')], check=True, capture_output=True)
            result = subprocess.run([sys.executable, '-B', '-c',
                "from workspace_contract_probe import repaired_value; assert repaired_value() == 42"],
                cwd=applied, check=True, capture_output=True)
            assert all(study.file_digest(applied / item['path']) == item['sha256'] for item in identity['runtime_artifacts'])
            check.update(source_patch_applied=True, changed_files=count, runtime_artifacts_preserved=len(identity['runtime_artifacts']))

            tamper = case / 'tampered-runtime'
            tamper.mkdir()
            workspace = study.Workspace(protocol, row, upload, tamper, 'repair', control)
            edit = "from pathlib import Path\np = Path('/testbed') / " + repr(artifact['path']) + "\nwith p.open('r+b') as stream:\n    stream.write(b'FAIL')\n"
            code, _, err, cut = workspace.execute('python -B -c ' + shlex.quote(edit), time.monotonic()+30)
            assert code == 0 and not cut, err
            try:
                workspace.freeze(tamper, base)
            except study.InfrastructureError as error:
                assert 'altered a pinned runtime artifact' in str(error)
                assert not (tamper / 'patch.diff').exists()
                check['runtime_tampering_rejected'] = True
            else:
                raise AssertionError('Altered runtime artifact escaped trusted patch capture')
            workspace.close()
            workspace = None
            assert study.file_digest(upload) == original_archive_sha
            report['checks'].append(check)
            print(json.dumps({'event': 'boundary_case_passed', **check}), flush=True)
        assert len(report['checks']) == len(targets)
        report['passed'] = True
    except Exception:
        report['failure'] = traceback.format_exc()
    finally:
        if workspace is not None:
            workspace.close()
        for container in client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution}):
            control.remove(container)
        for volume in client.volumes.list(filters={'label': study.LABEL + '=' + execution}):
            volume.remove(force=True)
        control.closed.set()
        control.thread.join(timeout=5)
        report['owned_containers_remaining'] = len(client.containers.list(all=True, filters={'label': study.LABEL + '=' + execution}))
        report['owned_volumes_remaining'] = len(client.volumes.list(filters={'label': study.LABEL + '=' + execution}))
        study.save(output / 'boundary-verification.json', report)
        print(json.dumps({'event': 'boundary_checks_finished', 'passed': report['passed']}), flush=True)
        client.close()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
