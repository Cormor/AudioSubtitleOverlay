"""Summarize local installed-app evidence without copying private transcript text."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re


def json_rows(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:yield json.loads(line)
            except ValueError:continue  # Active last line may not yet be complete.


def normalize(text):
    return re.sub(r'\s+', '', text).casefold()


def summarize(directory):
    process=json.loads((directory/'process.json').read_text(encoding='utf-8'))
    pid=process['pid'];owned={pid};snapshots=[];memory=defaultdict(list)
    for row in json_rows(directory/'monitor.jsonl'):
        processes=row['processes']
        while True:
            old=len(owned);owned.update(x['pid'] for x in processes if x['parent'] in owned)
            if len(owned)==old:break
        ours=[x for x in processes if x['pid'] in owned]
        total=sum(x['working_set'] for x in ours)
        snapshots.append({'seconds':row['elapsed_seconds'],'process_count':len(ours),'total_mib':round(total/1024**2,3),
                          'source_submissions':row['source_submissions'],'translation_received':row['translation_received'],
                          'errors':row['errors']})
        for child in ours:memory[child['pid']].append(child['working_set']/1024**2)
    whole_log_counts={'source_submissions':0,'translation_received':0,'translation_submissions':0,'errors':0}
    health=[];log=directory/'isolated-appdata'/'AudioSubtitleOverlay'/'logs'/'app.log'
    if log.exists():
        for line in log.read_text(encoding='utf-8',errors='replace').splitlines():
            if f'pid={pid} ' in line:
                for token,key in [('字幕窗口已提交','source_submissions'),('译文已接收','translation_received'),('译文窗口已提交','translation_submissions'),(' ERROR ','errors')]:
                    if token in line:whole_log_counts[key]+=1
            if '运行状态：' not in line:continue
            values={key:float(value) for key,value in re.findall(r'(input_rms|backlog_seconds|inference_seconds)=([0-9.eE+-]+)',line)}
            health.append(values)
    translations=[];frames=[]
    for path in (directory/'trace').glob('run-*/events.jsonl'):
        for row in json_rows(path):
            if row.get('kind')=='translation':translations.append(row)
            elif row.get('kind')=='translation_frame':frames.append(row)
    # Require a returned translation to match its later rendered text. Waiting
    # labels and lane redraw counts cannot alone prove translation output.
    matches=[]
    for translation in translations:
        wanted=normalize(translation.get('text',''))
        if not wanted:continue
        for frame in frames:
            if frame['perf']<translation['perf']:continue
            visible=[''.join(fragment.get('text','') for fragment in entry.get('fragments',[])) for entry in frame.get('visible',[])]
            if any(wanted==normalize(text) or (len(normalize(text))>=4 and normalize(text) in wanted) for text in visible):
                matches.append({'sequence':translation['sequence'],'milliseconds_to_visible_submission':round((frame['perf']-translation['perf'])*1000,3)})
                break
    result_path=directory/'result.json'
    result=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else None
    late=[x['total_mib'] for x in snapshots if x['seconds']>=max(60,snapshots[-1]['seconds']-900)] if snapshots else []
    return {'completed':result is not None,'result':result,'whole_log_counts':whole_log_counts,'last_snapshot':snapshots[-1] if snapshots else None,
        'snapshot_count':len(snapshots),'process_memory_mib':{str(k):{'minimum':round(min(v),3),'maximum':round(max(v),3),'last':round(v[-1],3)} for k,v in memory.items()},
        'last_15_minutes_total_mib_range':[round(min(late),3),round(max(late),3)] if late else [],
        'health_samples':len(health),'max_backlog_seconds':max((x.get('backlog_seconds',0) for x in health),default=None),
        'max_inference_seconds':max((x.get('inference_seconds',0) for x in health),default=None),
        'traced_translations':len(translations),'returned_translations_matched_to_actual_frames':len(matches),
        'matching_rule':'Returned text matches normalized visible fragments after acceptance; local text is omitted.',
        'matched_sequence_and_submission_wait':matches}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--require-complete',action='store_true')
    args=parser.parse_args()
    result=summarize(args.directory)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='matched_sequence_and_submission_wait'},ensure_ascii=False,indent=2))
    if args.require_complete:
        final=result['result'];counts=result['whole_log_counts']
        if not(final and final['duration_seconds']>=3598 and final['exit_code']==0 and not final['remaining_processes'] and final['formal_settings_unchanged'] and counts['errors']==0 and counts['source_submissions'] and counts['translation_received'] and counts['translation_submissions']):raise SystemExit('Full installed acceptance has not passed')


if __name__=='__main__':main()
