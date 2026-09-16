"""Capture an already-running native SGLang deployment without generating tokens."""
import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import signal
import time

import docker

PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')
ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')


def timestamp_ns(value):
    match = re.fullmatch(r'(.+T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z', value)
    if match is None:
        raise ValueError('Expected a UTC Docker timestamp')
    seconds = int(datetime.fromisoformat(match[1] + '+00:00').timestamp())
    return seconds * 10**9 + int((match[2] or '').ljust(9, '0'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--container', default='qwen3.8-27b-sglang-6000pro')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    output = (ROOT / 'evidence' / args.output).resolve()
    if not output.is_relative_to(ROOT / 'evidence'):
        raise ValueError('Capture must remain in the owned native evidence tree')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = PROJECT / 'ai_scientist/swebench_study.py'
    spec = importlib.util.spec_from_file_location('capture_study', source)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
    runner_sha = study.file_digest(source)
    engine = client.info()
    assert not any(value in (engine['Name'] + engine['OperatingSystem']).lower()
                   for value in ('docker-desktop', 'docker desktop'))
    container = client.containers.get(args.container)
    control = study.Control(output, client, 'qwen-runtime-capture')
    signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
    signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
    endpoint = study.Endpoint({'target': {'backend': 'sglang', 'base_url': args.base_url}, 'native': {}}, control)
    print(json.dumps({'event': 'runtime_capture_started', 'container_id': container.id}), flush=True)
    try:
        metadata = {path: endpoint.request(path) for path in ('/v1/models', '/model_info', '/server_info', '/v1/loads')}
        settings = study._sglang_settings(metadata['/server_info'])
        repos = [settings['model_path'], settings['speculative_draft_model_path']]
        started = timestamp_ns(container.attrs['State']['StartedAt']) // 10**9
        log = container.logs(timestamps=True, since=int(started), until=int(started) + 300)
        assert len(log) <= study.MAX_RESPONSE
        study.atomic(output / 'startup.log', log)
        ready = re.search(rb'(?m)^(\S+) .*Application startup complete\.', log)
        assert ready is not None, 'No retained startup readiness evidence'
        ready_ns = timestamp_ns(ready[1].decode())
        port = study.urllib.parse.urlparse(args.base_url).port or 80
        snapshot = study._sglang_snapshot(control, container.id, repos, port=port,
                                          hash_files=True, deadline=time.monotonic() + 600)
        after = study._sglang_snapshot(control, container.id, repos, port=port,
                                      hash_files=False, deadline=time.monotonic() + 60)
        assert study._sglang_state(after) == study._sglang_state(snapshot), 'Runtime changed during capture'
        assert all(max(item['mtime_ns'], item['ctime_ns'], item['link_mtime_ns'], item['link_ctime_ns']) <= ready_ns
                   for item in snapshot['files']), 'Runtime source/cache modified after recorded startup'
        assert study.file_digest(source) == runner_sha, 'Runner changed during identity capture'
        manifest = {'schema_version': 1, 'backend': 'sglang', 'base_url': args.base_url,
                    'capture_runner_sha256': runner_sha, 'capture_script_sha256': study.file_digest(__file__),
                    'snapshot': snapshot, 'ready_ns': ready_ns, 'ready_at': ready[1].decode(),
                    'startup_log_sha256': study.file_digest(output / 'startup.log'), 'server_settings': settings,
                    'model_info': {key: metadata['/model_info'][key] for key in
                                   ('model_path', 'tokenizer_path', 'weight_version', 'model_type', 'architectures', 'is_generation')},
                    'effective_max_running_requests_per_dp': [state['effective_max_running_requests_per_dp']
                        for state in metadata['/server_info']['internal_states']],
                    'initial_load_snapshot': metadata['/v1/loads'], 'model_generations': 0,
                    'identity_assurance': 'Pinned running container/image, startup evidence and byte-verified on-disk cache/source. Not a cryptographic attestation of in-memory weights.',
                    'concurrency_assurance': 'Serving configuration is preserved. Passive load snapshots can be stale; exclusivity and absence of other clients are not established.'}
        study.save(output / 'runtime-manifest.json', manifest)
        print(json.dumps({'event': 'runtime_capture_finished', 'passed': True,
                          'files_verified': snapshot['file_count'], 'bytes_verified': snapshot['hashed_bytes'],
                          'manifest_sha256': study.file_digest(output / 'runtime-manifest.json'),
                          'context_tokens': settings['context_length'], 'configured_concurrency': settings['max_running_requests'],
                          'effective_concurrency': manifest['effective_max_running_requests_per_dp']}), flush=True)
    finally:
        endpoint.session.close()
        control.closed.set()
        control.thread.join(timeout=5)
        client.close()


if __name__ == '__main__':
    main()
