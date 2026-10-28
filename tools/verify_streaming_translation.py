"""Bounded replay/trace acceptance for real draft translations and compositor frames.

This records local evidence only. It does not play audio or modify formal settings.
Replay reuses benchmark_replay.py; installed-executable runs reuse the existing
verify_installed_runtime.py and supply their process.json with --run-record.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


MAX_TRACE_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 200_000
SOURCE_FILES = ('VERSION', 'pipeline.py', 'subtitle_events.py', 'display_module.py',
                'text_processing.py',
                'main.py', 'application_controller.py', 'overlay.py', 'runtime_trace.py',
                'translation.py', 'runtime_workers.py', 'recognition_sessions.py',
                'asr_backends.py', 'rolling_transcript.py', 'tools/benchmark_replay.py')
CONTRACT_FILES=('subtitle_events.py','text_processing.py','display_module.py')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def source_identity(source):
    return {name: sha256(source / name) for name in SOURCE_FILES}


def analysis_contract_identity(source,expected=None):
    actual={name:sha256(source/name) for name in CONTRACT_FILES}
    if expected is not None and any(expected.get(name)!=digest for name,digest in actual.items()):
        raise ValueError('Source/display analysis contracts differ from target BUILD_INFO')
    return actual


def target_build_contract(executable):
    candidates=(executable.parent/'_internal/BUILD_INFO.json',executable.parent/'BUILD_INFO.json')
    path=next((candidate for candidate in candidates if candidate.is_file()),None)
    if path is None:raise ValueError('Target BUILD_INFO is required to bind display normalization')
    metadata=json.loads(path.read_text(encoding='utf-8'))
    expected=metadata.get('source_files')
    if not isinstance(expected,dict) or any(not isinstance(expected.get(name),str) for name in CONTRACT_FILES):
        raise ValueError('Target BUILD_INFO lacks source/display contract identities')
    return expected,dict(path=str(path),sha256=sha256(path),version=metadata.get('version'))


def load_analysis_contracts(source,expected=None):
    """Load the exact production source/display rules without an Application or GUI."""
    identity=analysis_contract_identity(source,expected)
    previous={name:sys.modules.get(Path(name).stem) for name in CONTRACT_FILES}
    modules={}
    try:
        for name in CONTRACT_FILES:
            stem=Path(name).stem;alias='streaming_contract_'+stem+'_'+identity[name][:12]
            spec=importlib.util.spec_from_file_location(alias,source/name)
            module=importlib.util.module_from_spec(spec);sys.modules[alias]=module;sys.modules[stem]=module
            spec.loader.exec_module(module);modules[stem]=module
        if analysis_contract_identity(source,expected)!=identity:
            raise ValueError('Analysis contracts changed while loading')
        return modules['subtitle_events'].source_text_extends,modules['display_module'].DisplayModule._normalize_text,identity
    finally:
        for name,value in previous.items():
            stem=Path(name).stem
            if value is None:sys.modules.pop(stem,None)
            else:sys.modules[stem]=value


def normalize(text):
    return ''.join(char for char in text if not char.isspace())


def real_text(text):
    normalized = normalize(text).strip('.…()（）[]【】{}<>《》').casefold()
    return bool(normalized and normalized not in {'翻译中', '正在翻译', '等待翻译', '等待译文', 'translating'})


def ordered_fragments(fragments, translated):
    whole, cursor = normalize(translated), 0
    for fragment in fragments:
        piece = normalize(fragment)
        if not piece:
            continue
        found = whole.find(piece, cursor)
        if found < 0:
            return False
        cursor = found + len(piece)
    return cursor > 0


def block_key(row):
    return tuple(row.get(name) for name in
                 ('pipeline_id', 'session_id', 'source_id', 'block_id', 'translation_epoch'))


def source_key(row, sequence=None):
    return block_key(row) + (row.get('sequence') if sequence is None else sequence,)


def translation_key(row):
    return source_key(row) + (row.get('revision'), row.get('final'))


def distribution(values):
    values = sorted(values)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'minimum_ms': values[0] * 1000,
            'median_ms': values[len(values) // 2] * 1000,
            'p95_ms': values[int(.95 * (len(values) - 1))] * 1000,
            'maximum_ms': values[-1] * 1000}


def load_trace(directory):
    events = directory / 'events.jsonl'
    metadata = json.loads((directory / 'trace.json').read_text(encoding='utf-8'))
    if events.stat().st_size > MAX_TRACE_BYTES:
        raise ValueError('Trace exceeds 128 MiB')
    rows = []
    with events.open(encoding='utf-8') as stream:
        for line in stream:
            if len(rows) >= MAX_EVENTS or len(line) > 4 * 1024 * 1024:
                raise ValueError('Trace event count or line limit exceeded')
            rows.append(json.loads(line))
    return rows, metadata


def analyze(rows, metadata, extends, minimum_partial=2,*,display_normalizer=None):
    if display_normalizer is None:
        from display_module import DisplayModule
        display_normalizer=DisplayModule._normalize_text
    failures = []
    if not metadata.get('text_included'):
        failures.append('trace_text_missing')
    if metadata.get('dropped_events') or metadata.get('pending_events') or metadata.get('error'):
        failures.append('trace_incomplete')
    sources = {}
    source_versions = {}
    histories = {}
    finals = {}
    first_final_perf = {}
    translations = {}
    contexts = [row for row in rows if row.get('kind') == 'translation_context']
    calls = [row for row in rows if row.get('kind') == 'translation_call']
    completions = [row for row in rows if row.get('kind') == 'translation_call_finished']
    cached = [row for row in rows if row.get('kind') == 'translation_cached']
    if not contexts:
        failures.append('translation_context_evidence_missing')
    for row in rows:
        if row.get('kind') == 'source':
            if not isinstance(row.get('text'), str):
                continue
            key = source_key(row)
            previous = sources.get(key)
            if previous and previous['text'] != row['text']:
                failures.append('conflicting_source_identity')
            sources[key] = row
            source_versions.setdefault(key, []).append(row)
            histories.setdefault(block_key(row), []).append(row)
            if row.get('final'):
                finals[block_key(row)] = row
                first_final_perf[block_key(row)] = min(first_final_perf.get(block_key(row), float('inf')), row['perf'])
        elif row.get('kind') in {'translation', 'translation_not_applied'}:
            translations.setdefault(translation_key(row), []).append(row)

    ui_histories={}
    for row in rows:
        if row.get('kind')!='presented':continue
        at=row.get('presented_at')
        if type(at) not in (int,float) or not math.isfinite(at) or row.get('perf',at)<at:
            failures.append('invalid_source_presentation_time');continue
        matching=[version for version in source_versions.get(source_key(row),[])
                  if version.get('perf',float('inf'))<=at and all(version.get(name)==row.get(name)
                     for name in ('revision','final','text','language'))]
        if not matching:
            failures.append('unmatched_source_presentation');continue
        version=max(matching,key=lambda item:item['perf'])
        ui_histories.setdefault(block_key(row),[]).append((at,version))
    if not ui_histories:failures.append('source_presentation_evidence_missing')
    for versions in ui_histories.values():
        ordered=sorted(versions,key=lambda item:(item[0],item[1]['sequence']))
        if any(a[1]['sequence']>b[1]['sequence'] for a,b in zip(ordered,ordered[1:])):
            failures.append('source_presentation_sequence_regressed')

    def model_completed(result):
        key = translation_key(result)
        matching_cache = [row for row in cached if translation_key(row) == key
                          and row.get('source_language') == result.get('source_language')
                          and row.get('target_language') == result.get('target_language')
                          and row.get('finished_at', float('inf')) <= result['perf']]
        if matching_cache:
            return True
        successful = [row for row in completions if translation_key(row) == key
                      and row.get('success') is True
                      and row.get('source_language') == result.get('source_language')
                      and row.get('target_language') == result.get('target_language')
                      and row.get('finished_at', float('inf')) <= result['perf']]
        return any(translation_key(call) == key and call.get('started_at') == done.get('started_at')
                   and call.get('source_language') == result.get('source_language')
                   and call.get('target_language') == result.get('target_language')
                   and call.get('input_characters') == len(result.get('source_text', ''))
                   and call.get('backend') == done.get('backend')
                   for done in successful for call in calls)

    partials = {}
    final_frames = {}
    matched_frames = 0
    invalid_frames = 0
    for row in rows:
        if row.get('kind') != 'translation_frame':
            continue
        presented = row.get('presented_at')
        for frame in row.get('visible', []):
            fragments = [part.get('text', '') for part in frame.get('fragments', [])]
            if not fragments or not real_text(''.join(fragments)):
                continue
            if not isinstance(presented, (int, float)):
                invalid_frames += 1
                continue
            received = [item for item in translations.get(translation_key(frame), [])
                        if item.get('perf', float('inf')) <= presented]
            result = max(received, key=lambda item: item['perf'], default=None)
            version_rows = [item for item in source_versions.get(source_key(frame), [])
                            if item.get('perf', float('inf')) <= presented]
            source = max(version_rows, key=lambda item: item['perf'], default=None)
            current_rows = [(at,item) for at,item in ui_histories.get(block_key(frame), []) if at<=presented]
            shown=max(current_rows,key=lambda item:(item[0],item[1]['sequence']),default=None)
            current=shown[1] if shown else None
            active = max((item for item in contexts if item.get('perf', float('inf')) <= presented),
                         key=lambda item: item['perf'], default=None)
            valid = bool(result and source and current and active)
            if valid:
                valid = (result.get('source_text') == source['text']
                         and display_normalizer(source['text'],source.get('language')) == frame.get('source_text')
                         and result.get('source_language') == source.get('language')
                         and result.get('perf', float('inf')) <= presented
                         and real_text(result.get('text', ''))
                         and ordered_fragments(fragments, result['text'])
                         and source.get('final') == frame.get('final')
                         and frame.get('source_sequence') == current['sequence']
                         and frame.get('source_final') == current.get('final')
                         and source['sequence'] <= current['sequence']
                         and active.get('pipeline_id') == frame.get('pipeline_id')
                         and active.get('session_id') == frame.get('session_id')
                         and active.get('translation_epoch') == frame.get('translation_epoch')
                         and active.get('enabled') is True
                         and active.get('target_language') == result.get('target_language')
                         and model_completed(result))
            if valid:
                lineage = [item for item in histories[block_key(frame)]
                           if source['sequence'] <= item['sequence'] <= current['sequence']
                           and item.get('perf', float('inf')) <= presented]
                lineage.sort(key=lambda item: (item['sequence'], item.get('perf', 0)))
                valid = (bool(lineage) and lineage[0]['sequence'] == source['sequence']
                         and lineage[-1]['sequence'] == current['sequence']
                         and all(a.get('language') == b.get('language')
                            and extends(a['text'], b['text'], previous_final=a['final'])
                            for a, b in zip(lineage, lineage[1:])))
            if not valid:
                invalid_frames += 1
                continue
            matched_frames += 1
            final = finals.get(block_key(frame))
            if (not frame.get('final') and final and presented < first_final_perf[block_key(frame)]
                    and frame.get('translation_state') in {'draft', 'updating'}):
                partials.setdefault(block_key(frame), {})[frame['sequence']] = presented
            if (frame.get('final') and frame.get('source_final')
                    and frame.get('translation_state') == 'final' and not frame.get('pending')
                    and frame['sequence'] == current['sequence']
                    and frame['source_text'] == display_normalizer(current['text'],current.get('language'))):
                final_frames[source_key(frame)] = presented

    partial_count = sum(len(values) for values in partials.values())
    maximum_in_one_block = max((len(values) for values in partials.values()), default=0)
    if partial_count < minimum_partial or maximum_in_one_block < minimum_partial:
        failures.append('insufficient_distinct_drafts_drawn_before_final')
    streaming_completed_blocks = [key for key, values in partials.items()
                                 if len(values) >= minimum_partial and key in finals
                                 and source_key(finals[key]) in final_frames]
    if not streaming_completed_blocks:
        failures.append('same_block_drafts_and_exact_final_not_proved')
    latest_final = max(finals.values(), key=lambda row: row.get('perf', 0), default=None)
    if latest_final is None or source_key(latest_final) not in final_frames:
        failures.append('latest_final_not_exactly_drawn')
    if invalid_frames:
        failures.append('unmatched_or_stale_translation_frames')
    if not calls or not completions:
        failures.append('real_model_call_evidence_missing')
    intervals = []
    last_partial = {}
    for call in sorted(calls, key=lambda row: row.get('started_at', 0)):
        if call.get('final'):
            continue
        key = (call.get('pipeline_id'), call.get('session_id'), call.get('translation_epoch'), call.get('backend'))
        now = call.get('started_at', 0)
        if key in last_partial:
            intervals.append(now - last_partial[key])
        last_partial[key] = now
    return dict(success=not failures, failures=sorted(set(failures)), source_events=len(sources),
                source_partial_events=sum(not row.get('final') for row in sources.values()),
                source_final_events=len(finals), received_translation_results=len(translations),
                source_presentation_events=sum(len(items) for items in ui_histories.values()),
                immediately_applied_translation_results=sum(row.get('display_applied') is True for versions in translations.values() for row in versions),
                matched_translation_frames=matched_frames, invalid_translation_frames=invalid_frames,
                distinct_drafts_drawn_before_final=partial_count,
                maximum_distinct_drafts_in_one_block=maximum_in_one_block,
                completed_streaming_blocks=len(streaming_completed_blocks),
                exact_final_frames=len(final_frames), real_model_calls=len(calls),
                partial_model_calls=sum(not row.get('final') for row in calls),
                final_model_calls=sum(bool(row.get('final')) for row in calls),
                cached_results=len(cached), input_characters=sum(row.get('input_characters', 0) for row in calls),
                failed_calls=sum(not row.get('success') for row in completions),
                model_call_duration=distribution([row['finished_at'] - row['started_at'] for row in completions]),
                queue_to_call=distribution([row['started_at'] - row['queued_at'] for row in calls]),
                partial_call_spacing=distribution(intervals),
                scope='Actual ASR/MT event identities and translated fragments submitted to Windows compositor; not scanout or acoustic latency')


def replay(args, output):
    if os.name != 'nt' or args.source is None:
        raise ValueError('Replay requires Windows and --source')
    if not 2 <= args.audio_seconds <= 60 or args.wav.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Replay requires 2..60 seconds and WAV at most 64 MiB')
    before = source_identity(args.source)
    trace = output / 'trace'
    env = os.environ.copy()
    env.update(ASR_TRACE_DIR=str(trace), ASR_TRACE_SECONDS=str(min(120, args.audio_seconds + 35)),
               ASR_TRACE_TEXT='1', PYTHONIOENCODING='utf-8')
    env.pop('ASR_TRACE_AUDIO', None)
    command = [sys.executable, str(args.source / 'tools' / 'benchmark_replay.py'),
               '--source', str(args.source), '--wav', str(args.wav), '--output', str(output / 'replay'),
               '--rounds', '1', '--audio-seconds', str(args.audio_seconds),
               '--source-language', args.source_language, '--target-language', args.target_language]
    started = time.time()
    with (output / 'replay-console.txt').open('wb') as console:
        process = subprocess.Popen(command, cwd=args.source, env=env, stdout=console, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=args.audio_seconds + 55)
        except subprocess.TimeoutExpired:
            # This exact still-owned process tree was created above; never target
            # a process by application name or touch the user's formal instance.
            subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                           stdout=console, stderr=subprocess.STDOUT, timeout=10, check=False)
            process.wait(timeout=10)
            raise TimeoutError('Owned replay exceeded its bounded deadline')
    provenance = dict(pid=process.pid, started_epoch=started, exit_code=code,
                      source=str(args.source), source_sha256=before,
                      source_unchanged=source_identity(args.source) == before,
                      wav_sha256=sha256(args.wav), audio_seconds=args.audio_seconds, command=command,
                      ended_epoch=time.time(), trace_root=str(trace),
                      executable=str(Path(sys.executable).resolve()), executable_sha256=sha256(Path(sys.executable)))
    producer_record = json.loads((output / 'replay' / 'provenance.json').read_text(encoding='utf-8'))
    producer_pid, parent_pid = producer_record.get('pid'), producer_record.get('parent_pid')
    if producer_pid != process.pid and parent_pid != process.pid:
        raise ValueError('Replay producer is neither the owned launcher nor its direct venv child')
    if (producer_record.get('source_sha256') != before
            or not producer_record.get('source_unchanged')):
        raise ValueError('Replay producer did not attest the same unchanged source')
    provenance.update(producer_pid=producer_pid, producer_parent_pid=parent_pid)
    replay_result = json.loads((output / 'replay' / 'result.json').read_text(encoding='utf-8'))
    if not replay_result.get('formal_settings_unchanged'):
        raise RuntimeError('Formal settings changed during the isolated replay')
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
    if code or not provenance['source_unchanged']:
        raise RuntimeError('Replay failed or source changed during execution')
    runs = list(trace.glob('run-*'))
    if len(runs) != 1:
        raise ValueError('Expected exactly one owned trace run')
    return runs[0], provenance


def bind_trace(directory, metadata, record, *, source=None, exe=None):
    root = Path(record.get('trace_root', '')).resolve()
    if not record.get('trace_root') or not directory.is_relative_to(root):
        raise ValueError('Trace is outside the root recorded by its producer')
    producer = metadata.get('process_id')
    allowed = set(record.get('owned_pids', [record.get('pid')])) if exe else {record.get('pid')}
    if not exe and record.get('producer_parent_pid') == record.get('pid'):
        allowed.add(record.get('producer_pid'))
    if not producer or producer not in allowed:
        raise ValueError('Trace process does not match the recorded owned run')
    if (metadata.get('started_epoch', 0) < record.get('started_epoch', float('inf')) - 1
            or not record.get('ended_epoch')
            or metadata.get('ended_epoch', float('inf')) > record['ended_epoch'] + 1):
        raise ValueError('Trace is outside the recorded run time window')
    if exe:
        expected_exe, expected_hash = exe, record.get('executable_sha256')
        if Path(record.get('exe', '')).resolve() != exe or not record.get('executable_unchanged'):
            raise ValueError('Installed run did not preserve this executable identity')
    else:
        if (Path(record.get('source', '')).resolve() != source
                or not record.get('source_unchanged')
                or source_identity(source) != record.get('source_sha256')):
            raise ValueError('Trace has no matching unchanged source provenance')
        expected_exe = Path(record.get('executable', '')).resolve()
        expected_hash = record.get('executable_sha256')
    if (not expected_hash or sha256(expected_exe) != expected_hash
            or Path(metadata.get('executable', '')).resolve() != expected_exe
            or metadata.get('executable_sha256') != expected_hash):
        raise ValueError('Trace executable fingerprint does not match its recorded producer')
    return dict(process_id=producer, trace_root=str(root), executable_sha256=expected_hash,
                recorded_started_epoch=record['started_epoch'], recorded_ended_epoch=record['ended_epoch'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    artifact = parser.add_mutually_exclusive_group(required=True)
    artifact.add_argument('--source', type=Path)
    artifact.add_argument('--exe', type=Path)
    capture = parser.add_mutually_exclusive_group(required=True)
    capture.add_argument('--trace', type=Path)
    capture.add_argument('--wav', type=Path)
    parser.add_argument('--run-record', type=Path, help='Recorded source provenance.json or installed-run process.json, required with --trace')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audio-seconds', type=float, default=18)
    parser.add_argument('--source-language', choices=['zh', 'en'], default='zh')
    parser.add_argument('--target-language', choices=['zh', 'en'], default='en')
    parser.add_argument('--minimum-partials', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.minimum_partials <= 64:
        parser.error('minimum-partials must be 1..64')
    if args.trace and not args.run_record:
        parser.error('--trace requires --run-record from the actual recording run')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.source:
        args.source = args.source.resolve()
        contract = args.source / 'subtitle_events.py'
        identity = dict(source=str(args.source), source_sha256=source_identity(args.source))
    else:
        args.exe = args.exe.resolve()
        if args.wav or not args.run_record:
            parser.error('--exe requires --trace and --run-record from the existing installed-run helper')
        record = json.loads(args.run_record.read_text(encoding='utf-8'))
        if Path(record.get('exe', '')).resolve() != args.exe or not record.get('pid'):
            raise ValueError('Installed run record does not identify this executable')
        contract = Path(__file__).resolve().parents[1] / 'subtitle_events.py'
        identity = dict(exe=str(args.exe), executable_sha256=sha256(args.exe), run_record=record)
    expected=None;build_binding=None
    if args.exe is not None:expected,build_binding=target_build_contract(args.exe)
    extends,display_normalizer,contract_hashes=load_analysis_contracts(contract.parent,expected)
    identity['analysis_contract_binding']=dict(source_files=contract_hashes,target_build=build_binding)
    if args.wav:
        directory, provenance = replay(args, output)
        identity.update(provenance=provenance)
        record = provenance
    else:
        directory = args.trace.resolve()
        record = json.loads(args.run_record.read_text(encoding='utf-8'))
    rows, metadata = load_trace(directory)
    identity['producer_binding'] = bind_trace(directory, metadata, record, source=args.source, exe=args.exe)
    result = analyze(rows, metadata, extends, args.minimum_partials,display_normalizer=display_normalizer)
    result.update(trace=str(directory), trace_metadata=metadata, artifact=identity,
                  contract_sha256=contract_hashes['subtitle_events.py'],display_contract_sha256=contract_hashes)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return not result['success']


if __name__ == '__main__':
    raise SystemExit(main())
