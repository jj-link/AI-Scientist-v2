"""Qualify synthetic Qwen protocol behavior; never start the eight-issue pilot."""
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import time
import traceback
from urllib.parse import urlsplit

PROJECT = Path('/mnt/c/Users/josep/Projects/personal/AI-Scientist-v2')
ROOT = Path('/home/workbench/Projects/personal/AI-Scientist-v2-study-runtime')
COHORT = (
    'sympy__sympy-20438', 'sympy__sympy-22080', 'django__django-11138',
    'pylint-dev__pylint-8898', 'sphinx-doc__sphinx-8551', 'pydata__xarray-3095',
    'matplotlib__matplotlib-14623', 'astropy__astropy-8707',
)
SYNTHETIC = (
    'This is synthetic runtime qualification, not benchmark repair. No defect is '
    'alleged and no benchmark problem is being solved. Do not seek issue statements, '
    'reference patches, hidden tests, or evaluators. Use one execute call at a time. '
    'Treat tool results as observations; never claim an unobserved result. '
)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def publish(path, value):
    """Keep even early import/admission failures independent of controller imports."""
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    with open(path, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def synthetic_protocol(protocol):
    result = copy.deepcopy(protocol)
    result['prompts'] = {
        'direct': SYNTHETIC + 'Complete only the specified synthetic file task, then call finish with exactly {}.',
        'repair': SYNTHETIC + 'Start from this fresh workspace. Only the visible handoff is prior-phase material. Complete only the specified synthetic file task, then call finish with exactly {}.',
        'notes': SYNTHETIC + 'Inspect the requested public function with execute, then call handoff with concise code-free notes. Do not copy source, code, transcripts, or hidden reasoning.',
        'diagnosis': SYNTHETIC + 'Inspect the requested public function with execute, then call handoff with all five code-free diagnosis fields. No defect is alleged: say so rather than inventing a root cause. No code, source copies, transcripts, or hidden reasoning.',
        'locations': SYNTHETIC + 'Inspect the requested public function with execute, then call handoff with only its existing relative path and exact qualified symbol. No prose or source code.',
    }
    return result


def cap_body(study, protocol, cap):
    target = protocol['target']
    return {
        'model': target['model'],
        'messages': [
            {'role': 'system', 'content': SYNTHETIC + 'Answer in plain language. Do not call tools.'},
            {'role': 'user', 'content': 'Write a deliberately long, detailed explanation of how a public library organizes books, at least two thousand words. Do not shorten or conclude early. Do not use tools.'},
        ],
        'tools': study.tools_for('direct', False), 'tool_choice': 'auto',
        'parallel_tool_calls': False, 'temperature': target['temperature'],
        'seed': target['seed'], 'max_tokens': cap, 'stream': True,
        'stream_options': {'include_usage': True},
        'reasoning_effort': target['reasoning_effort'],
        'chat_template_kwargs': {'enable_thinking': target['thinking'],
                                 'preserve_thinking': target['preserve_thinking']},
    }


def audit_endpoint_type(study):
    class AuditedEndpoint(study.Endpoint):
        """Retain production guards; observe the actual HTTP dispatch boundary."""
        def __init__(self, protocol, control, directory, guard):
            super().__init__(protocol, control)
            self.directory = directory
            directory.mkdir()
            self.guard = guard
            self.api_number = self.http_number = 0
            self.active = []
            self.forbid_generation = False
            native_request = self.session.request

            def dispatch(method, url, **kwargs):
                self.guard()
                path = urlsplit(url).path
                record = {'api_id': self.active[-1], 'method': method, 'path': path,
                          'body': kwargs.get('json'), 'observed_at_ns': time.time_ns()}
                if self.forbid_generation and path == '/v1/chat/completions':
                    publish(directory / f'blocked-{self.active[-1]:05d}.json', record)
                    raise RuntimeError('Negative control reached generation dispatch; no request sent')
                number = self.http_number
                self.http_number += 1
                # A transport failure leaves a dispatch attempt, not fabricated usage.
                publish(directory / f'http-{number:05d}-request.json', record)
                try:
                    response = native_request(method, url, **kwargs)
                    publish(directory / f'http-{number:05d}-headers.json', {
                        'status_code': response.status_code, 'headers': dict(response.headers),
                        'received_at_ns': time.time_ns(),
                    })
                    native_chunks = response.iter_content

                    def observed_chunks(chunk_size=1, decode_unicode=False):
                        require(not decode_unicode, 'Raw native evidence requires byte chunks')
                        with open(directory / f'http-{number:05d}-response.raw', 'xb') as evidence:
                            try:
                                for block in native_chunks(chunk_size=chunk_size, decode_unicode=False):
                                    evidence.write(block)
                                    evidence.flush()
                                    yield block
                            finally:
                                evidence.flush()
                                os.fsync(evidence.fileno())

                    response.iter_content = observed_chunks
                    if response.status_code >= 400:
                        # Production raises before consuming HTTP error bodies.
                        # Retain those too, bounded, and never retry the request.
                        total = 0
                        try:
                            for block in response.iter_content(65536):
                                total += len(block)
                                if total > study.MAX_RESPONSE:
                                    break
                        finally:
                            response.close()
                    return response
                except BaseException:
                    publish(directory / f'http-{number:05d}-error.json', {'failure': traceback.format_exc()})
                    raise

            self.session.request = dispatch

        def request(self, path, data=None, deadline=None, events_path=None):
            self.guard()
            number = self.api_number
            self.api_number += 1
            self.active.append(number)
            prefix = self.directory / f'api-{number:05d}'
            publish(Path(str(prefix) + '-request.json'), {
                'path': path, 'body': data, 'started_at_ns': time.time_ns(),
                'events_path': str(events_path) if events_path is not None else None,
            })
            try:
                result = super().request(path, data, deadline, events_path)
                publish(Path(str(prefix) + '-response.json'), result)
                self.guard()
                return result
            except BaseException:
                publish(Path(str(prefix) + '-error.json'), {'failure': traceback.format_exc()})
                raise
            finally:
                self.active.pop()

    return AuditedEndpoint


def generation_evidence(output):
    requests = []
    for path in sorted(output.glob('**/http-*-request.json')):
        value = json.loads(path.read_text())
        if value['path'] != '/v1/chat/completions':
            continue
        response = path.parent / f"api-{value['api_id']:05d}-response.json"
        item = {'request': str(path.relative_to(output)), 'request_sha256': sha(path),
                'response': str(response.relative_to(output)) if response.exists() else None,
                'usage': None, 'reasoning_observed': False, 'tool_calls_observed': []}
        if response.exists():
            actual = json.loads(response.read_text())
            item['usage'] = actual.get('usage')
            choice = actual['choices'][0]
            item['finish_reason'] = choice['finish_reason']
            item['reasoning_observed'] = bool(choice['message'].get('reasoning_content'))
            item['tool_calls_observed'] = [call['function']['name'] for call in choice['message'].get('tool_calls', [])]
        requests.append(item)
    complete = [item for item in requests if item['usage'] is not None]
    return {
        'qualification_generation_requests': len(requests),
        'qualification_generations_with_observed_usage': len(complete),
        'generation_count_exact': len(requests) == len(complete),
        'unknown_usage_requests': len(requests) - len(complete),
        'observed_prompt_tokens': sum(item['usage']['prompt_tokens'] for item in complete),
        'observed_completion_tokens': sum(item['usage']['completion_tokens'] for item in complete),
        'draft_tokens': None, 'accepted_draft_tokens': None, 'requests': requests,
        'count_method': 'One saved native HTTP chat dispatch attempt per request; never count tokenize/metadata calls. Transport failures have unknown generation/usage, not zero.',
    }


def phase_observations(directory, name):
    entries = [json.loads(line) for line in (directory / f'{name}-trace.jsonl').read_text().splitlines()]
    requests = [entry['body'] for entry in entries if 'request' in entry]
    responses = [entry['response'] for entry in entries if 'response' in entry]
    tools = [entry for entry in entries if 'tool' in entry]
    replayed = False
    for index, response in enumerate(responses[:-1]):
        message = response['choices'][0]['message']
        calls = message.get('tool_calls', [])
        if (not message.get('reasoning_content') or not calls
                or any(call['function']['name'] != 'execute' for call in calls)):
            continue
        assistant = {key: value for key, value in message.items()
                     if key in {'role', 'content', 'reasoning_content', 'tool_calls'}}
        following = requests[index + 1]['messages']
        for offset, item in enumerate(following):
            results = following[offset + 1:offset + 1 + len(calls)]
            if (item == assistant and len(results) == len(calls)
                    and all(tool.get('role') == 'tool' and tool.get('tool_call_id') == call['id']
                            and any(tool.get('content') == entry['delivered'] for entry in tools)
                            for call, tool in zip(calls, results))):
                replayed = True
    return {
        'requests': requests, 'responses': responses,
        'successful_execute_calls': sum(entry['exit_code'] == 0 for entry in tools),
        'execute_calls': len(tools), 'reasoning_tool_history_replayed': replayed,
        'reasoning_observed': any(response['choices'][0]['message'].get('reasoning_content') for response in responses),
    }


def verify_frozen_file(study, control, directory, filename, base):
    source = directory / 'repair-source'
    path = source / filename
    require(not (base / filename).exists(), 'Synthetic path already exists in pristine baseline')
    require(path.is_file() and not path.is_symlink(), 'Synthetic file missing or symlinked in frozen snapshot')
    content = path.read_text(encoding='utf-8')
    tree = ast.parse(content)
    expected = ast.parse('def qualification_answer():\n    return 42\n')
    # Never execute arbitrary model code on the host. Only this complete, inert AST
    # is admitted; comments/whitespace may differ. The compiled tree is the export.
    require(ast.dump(tree) == ast.dump(expected), 'Frozen synthetic function differs from the requested inert program')
    scope = {'__builtins__': {}}
    exec(compile(tree, str(path), 'exec'), scope)
    result = scope['qualification_answer']()
    require(type(result) is int and result == 42, 'Exported synthetic function did not return 42')
    names = study.checked_command(control, ['git', '-C', str(source), 'diff', '--cached',
                                          '--name-only', '-z', 'HEAD'], time.monotonic() + 30)
    require(names == filename.encode() + b'\0', 'Synthetic task changed files other than its unique file')
    return {'file': filename, 'sha256': sha(path), 'content': content, 'function_result': result,
            'verification': 'Executed only the AST-whitelisted actual frozen export; no official evaluator.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', required=True, help='Frozen corrected direct-only pilot protocol, absolute or project-relative')
    parser.add_argument('--output', required=True, help='New single-component native evidence identifier')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', args.output):
        parser.error('--output must be one SAFE_ID, not a path')
    evidence = ROOT / 'evidence'
    output = evidence / args.output
    require(evidence.resolve().is_relative_to(ROOT.resolve()), 'Evidence tree escapes native root')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {
        'purpose': 'synthetic_live_runtime_qualification_not_benchmark_scoring',
        'pilot_status': 'UNSTARTED', 'pilot_model_trials': 0, 'official_evaluations': 0,
        'reference_material_used': False, 'attempts_per_synthetic_case': 1,
        'passed': False, 'completed': False, 'checks': [], 'started_at_ns': time.time_ns(),
        'concurrency_assurance': 'Timestamped passive snapshots only. Server load snapshots may be stale; neither current idleness nor exclusivity is established.',
    }
    publish(output / 'started.json', report)
    client = control = endpoint = study = None
    endpoints = []
    started = time.monotonic()
    owned = False
    guard = None
    execution = 'qwen-qualification-' + hashlib.sha256(str(output).encode()).hexdigest()[:24]

    def check(name, action):
        record = {'name': name, 'passed': False, 'started_at_ns': time.time_ns()}
        print(json.dumps({'event': 'synthetic_runtime_check_started', 'check': name}), flush=True)
        try:
            if guard is not None:
                guard()
            record['observed'] = action()
            if guard is not None:
                guard()
            record['passed'] = True
            return record['observed']
        except BaseException:
            record['failure'] = traceback.format_exc()
            raise
        finally:
            record['finished_at_ns'] = time.time_ns()
            report['checks'].append(record)
            publish(output / f'check-{len(report["checks"]):02d}-{name}.json', record)
            print(json.dumps({'event': 'synthetic_runtime_check_finished', 'check': name,
                              'passed': record['passed']}), flush=True)

    try:
        require(os.name == 'posix', 'Run only with the pinned native WSL Python')
        import pwd
        require(pwd.getpwuid(os.getuid()).pw_name == 'workbench', 'Native workbench user required')
        import docker
        source = PROJECT / 'ai_scientist/swebench_study.py'
        script = Path(__file__).resolve()
        protocol_path = (PROJECT / args.protocol).resolve()
        require(protocol_path.is_relative_to(PROJECT), 'Protocol must remain in the project')
        raw = protocol_path.read_bytes()
        identities = {source: sha(source), script: sha(script), protocol_path: hashlib.sha256(raw).hexdigest()}
        report.update(runner_sha256=identities[source], qualification_script_sha256=identities[script],
                      protocol_sha256=identities[protocol_path], protocol_path=str(protocol_path))
        spec = importlib.util.spec_from_file_location('qwen_qualification_study', source)
        study = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(study)
        protocol = json.loads(raw)
        study.validate_protocol(protocol, raw)
        require(protocol['purpose'] == 'development_feasibility' and protocol['arms'] == ['direct']
                and protocol['repetitions'] == 1, 'Only the separately frozen direct-only pilot protocol is admissible')
        require(tuple(row['instance_id'] for row in protocol['cohort']) == COHORT,
                'Protocol must contain the complete, ordered, preselected eight-row cohort')
        target = protocol['target']
        require(target['backend'] == 'sglang' and target['context_tokens'] == 65536
                and target['reasoning_effort'] == 'xhigh', 'Expected the approved Qwen SGLang policy')
        manifest = Path(protocol['native']['runtime_manifest_path']).resolve()
        require(manifest.is_relative_to(ROOT), 'Runtime manifest must be native owned evidence')
        identities[manifest] = target['runtime_manifest_sha256']

        def source_guard():
            for path, expected in identities.items():
                require(sha(path) == expected, 'Pinned qualification input changed: ' + str(path))

        guard = source_guard
        guard()
        study.atomic(output / 'frozen-protocol.json', raw)
        study.atomic(output / 'frozen-runtime-manifest.json', manifest.read_bytes())
        publish(output / 'source-identities.json', {str(path): value for path, value in identities.items()})
        report['cohort'] = list(COHORT)
        report['runtime_manifest_sha256'] = target['runtime_manifest_sha256']
        client = docker.DockerClient(base_url='unix:///run/docker.sock', timeout=60)
        engine = client.info()
        require(not any(value in (engine['Name'] + engine['OperatingSystem']).lower()
                        for value in ('docker-desktop', 'docker desktop')), 'Docker Desktop is prohibited')
        publish(output / 'native-docker.json', {'socket': 'unix:///run/docker.sock', 'engine': engine})
        labels = {'label': study.LABEL + '=' + execution}
        require(not client.containers.list(all=True, filters=labels) and not client.volumes.list(filters=labels),
                'Execution label already exists; refusing to adopt or remove prior resources')
        owned = True
        control = study.Control(output, client, execution)
        signal.signal(signal.SIGTERM, lambda *_: control.stopped.set())
        signal.signal(signal.SIGINT, lambda *_: control.stopped.set())
        prepared = {}

        def admit_all():
            prepared.update(study.prepare_workspaces(protocol, output, control))
            qualification = study.load(output / 'workspaces/qualification.json')
            require(qualification['passed'] and len(qualification['checks']) == 16
                    and all(item['passed'] for item in qualification['checks']), 'Incomplete eight-row workspace admission')
            require(not generation_evidence(output)['qualification_generation_requests'], 'Generation occurred before admission')
            return {'model_free': True, 'workspaces': 16, 'issues': list(prepared),
                    'evidence': 'workspaces/qualification.json', 'official_evaluations': 0}

        check('model-free-eight-row-workspace-admission', admit_all)
        Endpoint = audit_endpoint_type(study)
        endpoint = Endpoint(protocol, control, output / 'endpoint', guard)
        endpoints.append(endpoint)
        check('production-runtime-binding', lambda: (study.runtime_check(protocol, endpoint, output),
              {'evidence': 'runtime-private.json', 'runtime_bound': endpoint.runtime_pin is not None})[1])

        def load_snapshot(label):
            value = {'requested_at_ns': time.time_ns(), 'interpretation': report['concurrency_assurance']}
            value['server_load'] = endpoint.request('/v1/loads', deadline=time.monotonic() + 30)
            value['received_at_ns'] = time.time_ns()
            code, out, err, cut = control.command(
                ['nvidia-smi', '--query-gpu=uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'],
                deadline=time.monotonic() + 30)
            value['gpu_snapshot'] = {'exit_code': code, 'stdout': out.decode(errors='replace'),
                                     'stderr': err.decode(errors='replace'), 'truncated': cut,
                                     'observed_at_ns': time.time_ns()}
            publish(output / f'load-{label}.json', value)
            return {'evidence': f'load-{label}.json', 'exclusivity_established': False}

        check('load-before', lambda: load_snapshot('before'))
        synthetic = synthetic_protocol(protocol)
        publish(output / 'synthetic-phase-configuration.json', {
            'purpose': 'qualification_only_not_a_valid_pilot_protocol',
            'prompts': synthetic['prompts'], 'budgets': synthetic['budgets'], 'target': synthetic['target'],
        })
        row = protocol['cohort'][0]
        base, upload = prepared['sympy__sympy-20438']

        def run_phase(case, arm, name, issue, handoff='', configuration=None, freeze_file=None,
                      expect='finish', need_replay=False):
            config = synthetic if configuration is None else configuration
            directory = output / case
            directory.mkdir()
            publish(directory / 'synthetic-input.json', {'issue': issue, 'visible_handoff': handoff,
                    'arm': arm, 'phase': name, 'benchmark_task': False,
                    'budgets': config['budgets'][name], 'context_tokens': config['target']['context_tokens']})
            workspace = study.Workspace(protocol, row, upload, directory,
                                        'preparation' if name == 'preparation' else 'repair', control)
            try:
                totals = study.metrics()
                phase_started = time.monotonic()
                result, visible = study.phase(config, arm, name, issue, handoff, base,
                                              workspace, directory, endpoint, totals)
                totals['elapsed_seconds'] = time.monotonic() - phase_started
                require(result == expect, f'{case}: expected {expect}, observed {result}')
                observed = phase_observations(directory, name)
                if expect in {'finish', 'handoff'}:
                    calls = [call for response in observed['responses']
                             for call in response['choices'][0]['message'].get('tool_calls', [])]
                    require(len(observed['requests']) == len(observed['responses'])
                            and sum(call['function']['name'] == 'execute' for call in calls) == observed['execute_calls']
                            and sum(call['function']['name'] == expect for call in calls) == 1
                            and all(call['function']['name'] in {'execute', expect} for call in calls)
                            and all(response['choices'][0]['finish_reason'] != 'length'
                                    and response['choices'][0]['message'].get('tool_calls')
                                    for response in observed['responses']),
                            case + ': incomplete/invalid action was followed by controller recovery; qualification fails')
                if expect in {'finish', 'handoff'} and case != 'terminal-only':
                    require(observed['successful_execute_calls'] > 0, case + ': no successful actual execute call')
                if need_replay:
                    require(observed['reasoning_tool_history_replayed'], case + ': no observed reasoning/tool history replay')
                if handoff:
                    first = observed['requests'][0]['messages']
                    require(len(first) == 3 and [message['role'] for message in first] == ['system', 'user', 'user']
                            and first[-1]['content'] == 'Visible preparation handoff (the only prior-phase material):\n' + handoff,
                            'Fresh repair received material other than the visible validated handoff')
                answer = {'termination_reason': result, 'metrics': totals,
                          'metrics_scope': 'Synthetic phase; excludes workspace setup and artifact validation/freeze. No official evaluation.',
                          'reasoning_observed': observed['reasoning_observed'],
                          'execute_calls': observed['execute_calls'],
                          'reasoning_tool_history_replayed': observed['reasoning_tool_history_replayed'],
                          'fresh_baseline_sha256': workspace.source_identity['pristine_sha256'],
                          'evidence_directory': case}
                if freeze_file is not None:
                    patch, changed = workspace.freeze(directory, base)
                    totals['changed_files'] = changed
                    require(changed == 1 and patch, 'Expected one synthetic file in frozen patch')
                    answer['frozen_file'] = verify_frozen_file(study, control, directory, freeze_file, base)
                if expect == 'handoff':
                    validated = study.valid_handoff(arm, json.loads(visible), base)
                    require(validated == visible, 'Production handoff revalidation failed')
                    location = {'path': 'sympy/core/sympify.py', 'symbol': 'sympify'}
                    if arm == 'locations':
                        require(json.loads(visible) == {'locations': [location]}, 'Handoff missed requested exact public location')
                    else:
                        require(location['path'] in visible and location['symbol'] in visible, 'Handoff omitted inspected public function')
                    publish(directory / 'visible-handoff.json', {'arm': arm, 'handoff': visible, 'validated': True})
                    answer['visible_handoff'] = visible
                if case == 'terminal-only':
                    require(len(observed['requests']) == 1 and observed['execute_calls'] == 0,
                            'Terminal-only control attempted an action or additional generation')
                    body = observed['requests'][0]
                    require([tool['function']['name'] for tool in body['tools']] == ['finish']
                            and body['tool_choice'] == {'type': 'function', 'function': {'name': 'finish'}},
                            'Terminal-only request did not force finish')
                    patch, changed = workspace.freeze(directory, base)
                    totals['changed_files'] = changed
                    require(not patch and changed == 0, 'Terminal-only control modified source')
                return answer
            finally:
                workspace.close()

        def file_task(filename):
            return {'synthetic_case': 'unique-file-return-42', 'task': SYNTHETIC +
                    f'Create only {filename}, containing exactly one zero-argument function named qualification_answer whose sole statement returns the integer 42. '
                    'Do not edit any existing file. Use execute to create the file and check its result; then call finish with exactly {}.'}

        # The impossible context and forged identities must not send any generation.
        def context_negative():
            config = copy.deepcopy(synthetic)
            config['target']['context_tokens'] = 1
            config['budgets']['direct']['tool_calls'] = 0
            endpoint.forbid_generation = True
            before = generation_evidence(output)['qualification_generation_requests']
            try:
                answer = run_phase('context-negative', 'direct', 'direct',
                    {'synthetic_case': 'impossible-context-budget', 'task': SYNTHETIC + 'Do not modify files; finish.'},
                    configuration=config, expect='budget_exhausted')
                require(generation_evidence(output)['qualification_generation_requests'] == before,
                        'Context rejection generated tokens')
                answer['model_free'] = True
                answer['context_tokens'] = 1
                return answer
            finally:
                endpoint.forbid_generation = False

        check('model-free-context-rejection', context_negative)

        def identity_negative(kind):
            bad = Endpoint(protocol, control, output / ('negative-' + kind + '-endpoint'), guard)
            endpoints.append(bad)
            bad.runtime_pin = copy.deepcopy(endpoint.runtime_pin)
            bad.server_identity = endpoint.server_identity
            bad.forbid_generation = True
            if kind == 'runtime-pin':
                bad.runtime_pin['snapshot']['started_at'] += '-forged'
                expected = 'Pinned SGLang container, source, or model cache changed'
            else:
                bad.server_identity = (endpoint.server_identity[0], 'forged-start-identity')
                expected = 'Native server restarted during execution'
            try:
                bad.request('/v1/chat/completions', cap_body(study, protocol, 1),
                            time.monotonic() + 120, output / ('forbidden-' + kind + '.sse'))
            except study.InfrastructureError as error:
                require(str(error) == expected, 'Identity control rejected for the wrong reason: ' + str(error))
                return {'model_free': True, 'rejected_before_http_generation': True, 'error': str(error),
                        'mutation': 'Only this disposable Endpoint copy; real runtime and frozen manifest unchanged'}
            raise RuntimeError('Forged identity was not rejected by production guard')

        check('model-free-runtime-pin-rejection', lambda: identity_negative('runtime-pin'))
        check('model-free-start-identity-rejection', lambda: identity_negative('start-identity'))
        suffix = hashlib.sha256(str(output).encode()).hexdigest()[:16]
        filename = f'qualification_{suffix}_direct.py'
        check('synthetic-direct', lambda: run_phase('synthetic-direct', 'direct', 'direct', file_task(filename),
                                                   freeze_file=filename, need_replay=True))
        for arm in ('notes', 'diagnosis', 'locations'):
            issue = {'synthetic_case': 'public-function-inspection', 'task': SYNTHETIC +
                     'Use execute to inspect public sympy/core/sympify.py:sympify. Locate and explain its public purpose, '
                     'not a defect. Include that relative path and exact symbol in the code-free handoff; for locations '
                     'return only that one location. Do not modify source or copy code into the handoff.'}
            handoff = check('synthetic-' + arm + '-handoff', lambda arm=arm: run_phase(
                'synthetic-' + arm + '-handoff', arm, 'preparation', issue, expect='handoff'))['visible_handoff']
            filename = f'qualification_{suffix}_{arm}.py'
            check('synthetic-' + arm + '-fresh-repair', lambda arm=arm, handoff=handoff, filename=filename: run_phase(
                'synthetic-' + arm + '-fresh-repair', arm, 'repair', file_task(filename), handoff,
                freeze_file=filename, need_replay=True))
        terminal = copy.deepcopy(synthetic)
        terminal['budgets']['direct']['tool_calls'] = 0
        check('synthetic-terminal-only', lambda: run_phase('terminal-only', 'direct', 'direct',
              {'synthetic_case': 'forced-terminal-only', 'task': SYNTHETIC + 'Do not modify files. Call finish with exactly {} now.'},
              configuration=terminal))

        def cap_probe(cap):
            directory = output / f'cap-{cap}'
            directory.mkdir()
            deadline = time.monotonic() + protocol['budgets']['direct']['seconds']
            body = cap_body(study, protocol, cap)
            count = endpoint.prompt_count(body, deadline)
            require(count + cap <= target['context_tokens'], 'Cap probe would exceed the real context')
            publish(directory / 'counted-request.json', {'body': body, 'input_tokens': count,
                    'body_sha256': study.digest(study.json_bytes(body)), 'output_token_cap': cap})
            actual = endpoint.request('/v1/chat/completions', body, deadline, directory / 'events.sse')
            publish(directory / 'response.json', actual)
            study._response_identity(target, actual)
            usage = actual['usage']
            choice = actual['choices'][0]
            require(usage['prompt_tokens'] == count, 'Cap probe prompt-count/usage mismatch')
            require(type(usage['completion_tokens']) is int and usage['completion_tokens'] == cap
                    and usage['total_tokens'] == count + cap, 'Cap probe did not deliver exactly its output cap')
            require(choice['finish_reason'] == 'length', 'Long-answer cap probe did not terminate at length')
            return {'max_tokens': cap, 'usage': usage, 'finish_reason': choice['finish_reason'],
                    'reasoning_observed': bool(choice['message'].get('reasoning_content')),
                    'visible_characters': len(choice['message'].get('content') or ''),
                    'reasoning_characters': len(choice['message'].get('reasoning_content') or ''),
                    'returned_tool_calls': choice['message'].get('tool_calls', []), 'executed_tool_calls': 0,
                    'prompt_count_parity': True, 'draft_tokens': None, 'accepted_draft_tokens': None}

        for cap in (1, 31):
            check('synthetic-output-cap-' + str(cap), lambda cap=cap: cap_probe(cap))
        check('load-after', lambda: load_snapshot('after'))
        check('final-runtime-identity', lambda: (endpoint.check_sglang(time.monotonic() + 120),
              {'unchanged': True})[1])
        guard()
        report['passed'] = True
    except BaseException:
        report['failure'] = traceback.format_exc()
    finally:
        cleanup_errors = []
        if control is not None:
            # No server is ever registered with Control or selected by this unique
            # execution label. Do not stop/reconfigure the serving deployment.
            try:
                if owned:
                    labels = {'label': study.LABEL + '=' + execution}
                    for container in client.containers.list(all=True, filters=labels):
                        try:
                            control.remove(container)
                        except BaseException:
                            cleanup_errors.append(traceback.format_exc())
                    for volume in client.volumes.list(filters=labels):
                        try:
                            volume.remove(force=True)
                        except BaseException:
                            cleanup_errors.append(traceback.format_exc())
                    report['owned_containers_remaining'] = len(client.containers.list(all=True, filters=labels))
                    report['owned_volumes_remaining'] = len(client.volumes.list(filters=labels))
                    require(report['owned_containers_remaining'] == report['owned_volumes_remaining'] == 0,
                            'Owned workspace resources remain after cleanup')
            except BaseException:
                cleanup_errors.append(traceback.format_exc())
            finally:
                control.closed.set()
                control.thread.join(timeout=5)
        for item in endpoints:
            try:
                item.session.close()
            except BaseException:
                cleanup_errors.append(traceback.format_exc())
        if client is not None:
            try:
                client.close()
            except BaseException:
                cleanup_errors.append(traceback.format_exc())
        try:
            if guard is not None:
                guard()
        except BaseException:
            cleanup_errors.append(traceback.format_exc())
        try:
            accounting = generation_evidence(output)
            report['generation_accounting'] = accounting
            require(accounting['generation_count_exact'], 'One or more dispatched requests has unknown final usage')
        except BaseException:
            cleanup_errors.append(traceback.format_exc())
        if cleanup_errors:
            report['cleanup_or_final_evidence_failures'] = cleanup_errors
            report['passed'] = False
        report['completed'] = True
        report['elapsed_seconds'] = time.monotonic() - started
        report['finished_at_ns'] = time.time_ns()
        publish(output / 'qualification-receipt.json', report)
        print(json.dumps({'event': 'synthetic_runtime_qualification_finished', 'passed': report['passed'],
                          'receipt': str(output / 'qualification-receipt.json'), 'pilot_status': 'UNSTARTED',
                          'pilot_model_trials': 0, 'official_evaluations': 0}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
