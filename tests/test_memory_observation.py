import unittest
from datetime import datetime, timedelta, timezone

from scripts.check_memory_observation import evaluate


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 10, 22, 15, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.start = dict(version='candidate', started_at=self.since, required_seconds=86400,
                          local_receiver_stopped=True, wechat_restart_passed=True)

    def record(self, at, maintenance=None):
        memory = dict(ready=True, error='', maintenance_error='', pending=0, failed_pending=0,
                      maintenance_success=maintenance)
        container = dict(Running=True, health='healthy', image='fixed', StartedAt='fixed',
                         OOMKilled=False, restarts=0)
        return dict(time=at, **{'qqchat-bot':dict(container), 'wxchat-bot':dict(container)},
                    qq=dict(webhook_ready=True, memory=memory),
                    wechat=dict(login_ready=True, updated_at=at, last_successful_poll=at,
                                uncertain_sends=0, last_error='', memory=memory))

    def records(self):
        return [self.record(self.since+i*300, '2026-10-23T04:00:10+08:00' if i*300>=46810 else None)
                for i in range(289)]

    def test_full_day_with_scheduled_run_is_only_ready_for_review(self):
        report = evaluate(self.start, self.records(), self.since+86400)
        self.assertTrue(report['ready_for_review'])
        self.assertEqual(report['user_cross_channel_acceptance'], 'requires_separate_user_confirmation')

    def test_elapsed_time_alone_and_catchup_do_not_pass(self):
        records = [self.record(self.since, '2026-10-22T14:43:51+08:00')]
        report = evaluate(self.start, records, self.since+86400)
        self.assertFalse(report['ready_for_review'])
        self.assertIn('24_hours_not_yet_observed', report['pending'])
        self.assertIn('scheduled_04_00_run_not_observed', report['pending'])

    def test_errors_stale_poll_or_restart_need_review(self):
        for issue in ('memory', 'poll', 'restart'):
            with self.subTest(issue=issue):
                records = self.records()
                if issue == 'memory': records[10]['wechat']['memory']['error'] = 'upstream'
                if issue == 'poll': records[10]['wechat']['last_successful_poll'] -= 300
                if issue == 'restart': records[10]['wxchat-bot']['StartedAt'] = 'changed'
                self.assertFalse(evaluate(self.start, records, self.since+86400)['ready_for_review'])

    def test_clock_before_deadline_cannot_finish(self):
        records = self.records()[:-1]
        report = evaluate(self.start, records, self.since+86399)
        self.assertFalse(report['ready_for_review'])
        self.assertIn('24_hours_not_yet_observed', report['pending'])
