#!/usr/bin/env python3
"""Host-side observation. Emits counts/health/resources only; never chat or environment."""
import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def snapshot(root):
    result = {'at':datetime.now(timezone.utc).isoformat(), 'time':time.time()}
    for name in ('qqchat-bot','wxchat-bot'):
        try:
            raw = subprocess.check_output(['docker','inspect',name],stderr=subprocess.DEVNULL,timeout=10)
            info = json.loads(raw)[0]
            result[name] = {key:info['State'].get(key) for key in ('Running','StartedAt','OOMKilled','ExitCode')}
            result[name].update(image=info['Image'],restarts=info['RestartCount'],
                                health=info['State'].get('Health',{}).get('Status'))
        except Exception as exc:
            result[name] = {'error':type(exc).__name__}
    try:
        with urllib.request.urlopen('http://127.0.0.1:18080/health',timeout=5) as response:
            health=json.load(response)
        result['qq']={key:health.get(key) for key in ('status','webhook_ready','queue_size','task_queue_size',
                        'task_queue_active','accepted_events','rejected_requests','last_event_at','memory')}
    except Exception as exc:
        result['qq']={'error':type(exc).__name__}
    try:
        result['wechat']=json.loads((root/'wechat-data/health.json').read_text())
    except Exception as exc:
        result['wechat']={'error':type(exc).__name__}
    try:
        raw=subprocess.check_output(['docker','stats','--no-stream','--format',
            '{{json .}}','qqchat-bot','wxchat-bot'],timeout=15,stderr=subprocess.DEVNULL,text=True)
        result['resources']=[{k:v for k,v in json.loads(line).items() if k in
            ('Name','CPUPerc','MemUsage','MemPerc','PIDs','NetIO','BlockIO')} for line in raw.splitlines()]
    except Exception as exc:
        result['resources']={'error':type(exc).__name__}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    directory=args.root/'observations'
    directory.mkdir(exist_ok=True,mode=0o700)
    record=snapshot(args.root)
    path=directory/(datetime.now(timezone.utc).date().isoformat()+'.jsonl')
    with path.open('a') as stream:
        stream.write(json.dumps(record,ensure_ascii=False)+'\n')
    path.chmod(0o600)
    print(json.dumps(record,ensure_ascii=False))


if __name__=='__main__': main()
