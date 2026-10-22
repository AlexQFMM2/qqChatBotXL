#!/usr/bin/env python3
"""Summarize sanitized host observations; never marks user acceptance complete."""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))


def evaluate(start, records, now):
    since = start['started_at']
    required = max(86400, start.get('required_seconds', 86400))
    rows = sorted((r for r in records if since <= r.get('time', 0) <= now),
                  key=lambda r: r['time'])
    deadline = since + required
    gaps = [b-a for a, b in zip([since]+[r['time'] for r in rows],
                                [r['time'] for r in rows]+[now])]
    reasons = []
    if now < deadline or not rows or rows[-1]['time'] < deadline:
        reasons.append('24_hours_not_yet_observed')
    if max(gaps, default=now-since) > 900:
        reasons.append('observation_gap_over_15_minutes')
    scheduled = []
    errors = 0
    max_pending = 0
    images = {name: set() for name in ('qqchat-bot', 'wxchat-bot')}
    starts = {name: set() for name in images}
    for row in rows:
        bad = False
        for name in images:
            container = row.get(name, {})
            images[name].add(container.get('image'))
            starts[name].add(container.get('StartedAt'))
            bad |= (not container.get('Running') or container.get('health') != 'healthy'
                    or bool(container.get('OOMKilled')) or bool(container.get('restarts')))
        wx = row.get('wechat', {})
        qq = row.get('qq', {})
        bad |= (not wx.get('login_ready') or not qq.get('webhook_ready')
                or bool(wx.get('last_error')) or bool(wx.get('uncertain_sends'))
                or row['time'] - (wx.get('updated_at') or 0) > 90
                or row['time'] - (wx.get('last_successful_poll') or 0) > 180)
        for channel in (wx, qq):
            memory = channel.get('memory', {})
            max_pending = max(max_pending, memory.get('pending', 0))
            bad |= (not memory.get('ready') or bool(memory.get('error'))
                    or bool(memory.get('maintenance_error')) or bool(memory.get('failed_pending')))
        errors += int(bad)
        stamp = wx.get('memory', {}).get('maintenance_success')
        try:
            done = datetime.fromisoformat(stamp).astimezone(SHANGHAI)
            # A startup catch-up outside the scheduled window is not a scheduled run.
            if since <= done.timestamp() <= row['time'] and done.hour == 4 and done.minute < 15:
                scheduled.append(done.isoformat())
        except (ValueError, TypeError):
            pass
    if not scheduled:
        reasons.append('scheduled_04_00_run_not_observed')
    if errors:
        reasons.append('health_or_memory_errors_need_review')
    if any(len(value) > 1 for value in images.values()) or any(len(value) > 1 for value in starts.values()):
        reasons.append('instance_changed_during_observation')
    if not start.get('local_receiver_stopped') or not start.get('wechat_restart_passed'):
        reasons.append('cutover_or_restart_evidence_missing')
    return dict(version=start['version'], generated_at=now, started_at=since,
                earliest_end=deadline, observed_seconds=max(0, now-since), samples=len(rows),
                max_gap_seconds=max(gaps, default=now-since), error_samples=errors,
                max_extraction_pending=max_pending, scheduled_runs=sorted(set(scheduled)),
                ready_for_review=not reasons, pending=reasons,
                user_cross_channel_acceptance='requires_separate_user_confirmation',
                latest_resources=rows[-1].get('resources') if rows else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    directory = args.root/'observations'
    start = json.loads((directory/'start.json').read_text())
    records, malformed = [], 0
    for path in directory.glob('????-??-??.jsonl'):
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                malformed += 1
    report = evaluate(start, records, time.time())
    if malformed:
        report['ready_for_review'] = False
        report['pending'].append('malformed_observation_records')
    target = directory/'report.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    temporary.chmod(0o600)
    os.replace(temporary, target)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
