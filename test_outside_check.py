"""The outside check decides UP, DOWN and when to mail (M25)."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('outside_check', os.path.join(ROOT, 'outside_check.py'))
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)

T0 = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
GOOD = {'Home page': None, 'API': None, 'Login': None}
BAD = {'Home page': None, 'API': 'the gateway said "Not Found"', 'Login': 'HTTP 503'}


def run(state, results, minutes):
    new, mail = oc.step(state, results, T0 + timedelta(minutes=minutes))
    if mail and new.get('status') == 'down':
        new.update(alerted=True, last_mail=(T0 + timedelta(minutes=minutes)).isoformat())
    return new, mail


class TestWhatCountsAsUp:
    def test_a_wrong_password_reply_in_our_format_is_up(self):
        assert oc.backend_refusal(401, json.dumps({'success': False, 'error': 'Invalid email or password'}), {401, 429}) is None

    def test_a_locked_account_is_still_our_backend(self):
        assert oc.backend_refusal(429, json.dumps({'success': False, 'error': 'Too many failed attempts.'}), {401, 429}) is None

    def test_the_gateways_not_found_is_down(self):
        assert 'Not Found' in oc.backend_refusal(404, '{"message":"Not Found"}', {401, 429})

    def test_the_gateways_service_unavailable_is_down(self):
        assert oc.backend_refusal(503, '{"message":"Service Unavailable"}', {401, 429}) == 'HTTP 503'

    def test_any_5xx_is_down(self):
        assert oc.backend_refusal(500, json.dumps({'success': False, 'error': 'x'}), {401, 429}) == 'HTTP 500'

    def test_no_answer_is_down(self):
        assert oc.backend_refusal(None, '', {401}) == 'no answer'

    def test_a_page_that_is_not_json_is_down(self):
        assert 'not our JSON' in oc.backend_refusal(401, '<html>', {401})

    def test_a_login_that_succeeds_is_not_the_expected_reply(self):
        assert oc.backend_refusal(200, json.dumps({'success': True}), {401, 429}) is not None


class TestWhenItMails:
    def test_one_failure_stays_quiet(self):
        state, mail = run({}, BAD, 0)
        assert mail is None and state['fails'] == 1

    def test_one_failure_then_good_stays_quiet(self):
        state, _ = run({}, BAD, 0)
        state, mail = run(state, GOOD, 30)
        assert mail is None and state['status'] == 'up' and 'down_since' not in state

    def test_the_second_failure_mails_what_failed_and_since_when(self):
        state, _ = run({}, BAD, 0)
        state, mail = run(state, BAD, 30)
        assert mail[0].startswith('HC LOMBARDO DOWN')
        assert 'Login: HTTP 503' in mail[1] and 'Failing since' in mail[1] and 'Home page' not in mail[1]

    def test_no_second_mail_inside_the_hour(self):
        state, _ = run({}, BAD, 0)
        state, _ = run(state, BAD, 30)
        state, mail = run(state, BAD, 60)
        assert mail is None

    def test_a_reminder_after_an_hour_and_only_one(self):
        state, _ = run({}, BAD, 0)
        state, _ = run(state, BAD, 30)
        state, _ = run(state, BAD, 60)
        state, mail = run(state, BAD, 90)
        assert mail[0].startswith('HC LOMBARDO DOWN: still down after 1 hour 30 minutes')
        state, mail = run(state, BAD, 120)
        assert mail is None

    def test_back_up_mails_the_total_downtime_once(self):
        state, _ = run({}, BAD, 0)
        state, _ = run(state, BAD, 30)
        state, mail = run(state, GOOD, 150)
        assert mail[0] == 'HC LOMBARDO BACK UP after 2 hours 30 minutes' and 'Total downtime' in mail[1]
        state, mail = run(state, GOOD, 180)
        assert mail is None

    def test_copy_has_no_long_dashes(self):
        state, _ = run({}, BAD, 0)
        state, a = run(state, BAD, 30)
        state, b = run(state, BAD, 90)
        _, c = run(state, GOOD, 120)
        for mail in (a, b, c):
            assert not any(ch in mail[0] + mail[1] for ch in '–—')


class TestAMailThatCannotGoOut:
    def test_no_settings_turns_the_run_red_and_tries_again(self, tmp_path, monkeypatch):
        monkeypatch.delenv('SMTP_HOST', raising=False)
        monkeypatch.delenv('MAIL_TO', raising=False)
        for k in ('PUSHOVER_TOKEN', 'PUSHOVER_USER'):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(oc, 'run_checks', lambda base, account: BAD)
        path = str(tmp_path / 's.json')
        assert oc.main(['--state', path, '--no-repair']) == 0
        assert oc.main(['--state', path, '--no-repair']) == 1
        with open(path) as f:
            assert json.load(f)['alerted'] is False
        assert oc.main(['--state', path, '--no-repair']) == 1

    def test_a_good_mail_is_recorded_once(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(oc, 'run_checks', lambda base, account: BAD)
        monkeypatch.setattr(oc, 'send', lambda mail: sent.append(mail) or True)
        path = str(tmp_path / 's.json')
        for _ in range(3):
            assert oc.main(['--state', path, '--no-repair']) == 0
        assert len(sent) == 1
        with open(path) as f:
            assert json.load(f)['alerted'] is True

    def test_a_back_up_mail_that_fails_is_tried_again(self, tmp_path, monkeypatch):
        path = str(tmp_path / 's.json')
        with open(path, 'w') as f:
            json.dump({'status': 'down', 'down_since': T0.isoformat(), 'fails': 3, 'alerted': True, 'last_mail': T0.isoformat()}, f)
        monkeypatch.setattr(oc, 'run_checks', lambda base, account: GOOD)
        monkeypatch.setattr(oc, 'send', lambda mail: False)
        assert oc.main(['--state', path]) == 1
        with open(path) as f:
            assert json.load(f)['alerted'] is True


NOW = T0 + timedelta(minutes=30)
ENV = {'REPAIR_AWS_KEY_ID': 'k', 'REPAIR_AWS_SECRET': 's', 'REPAIR_INSTANCE': 'i', 'REPAIR_REGION': 'r'}


class TestRepair:
    def go(self, monkeypatch, aws, fails=2, state=None, start=(True, '')):
        started = []
        monkeypatch.setattr(oc, 'server_state', lambda env: aws)
        monkeypatch.setattr(oc, 'start_server', lambda env: started.append(1) or start)
        new = {'fails': fails}
        return oc.repair(state or {}, new, BAD, NOW, ENV), new, started

    def test_a_stopped_server_is_started_and_says_what_broke_and_what_was_done(self, monkeypatch):
        alert, new, started = self.go(monkeypatch, 'stopped')
        assert started == [1] and new['last_start']
        assert alert[0].startswith('HC LOMBARDO DOWN') and 'What broke' in alert[1] and 'What was done' in alert[1]

    def test_one_failure_never_starts_anything(self, monkeypatch):
        alert, _, started = self.go(monkeypatch, 'stopped', fails=1)
        assert alert is None and started == []

    def test_a_running_server_is_never_touched(self, monkeypatch):
        alert, _, started = self.go(monkeypatch, 'running', fails=9)
        assert alert is None and started == []

    def test_it_does_not_start_twice_inside_the_hour(self, monkeypatch):
        alert, _, started = self.go(monkeypatch, 'stopped', state={'last_start': T0.isoformat()})
        assert alert is None and started == []

    def test_a_start_that_fails_says_so(self, monkeypatch):
        alert, _, _ = self.go(monkeypatch, 'stopped', start=(False, 'AWS refused the start'))
        assert 'could not be started' in alert[0] and 'Sign in to AWS' in alert[1]

    def test_an_account_refusal_alerts_at_once_in_plain_words_and_only_once(self, monkeypatch):
        alert, new, started = self.go(monkeypatch, oc.BLOCKED, fails=1)
        assert started == [] and 'cannot be repaired automatically' in alert[1] and 'billing' in alert[1]
        again, _, _ = self.go(monkeypatch, oc.BLOCKED, fails=2, state={'account_alerted': True})
        assert again is None

    def test_a_good_check_never_calls_aws(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oc, 'run_checks', lambda base, account: GOOD)
        monkeypatch.setattr(oc, 'server_state', lambda env: (_ for _ in ()).throw(AssertionError('touched AWS')))
        assert oc.main(['--state', str(tmp_path / 's.json')]) == 0

    def test_a_test_address_never_calls_aws(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oc, 'run_checks', lambda base, account: BAD)
        monkeypatch.setattr(oc, 'server_state', lambda env: (_ for _ in ()).throw(AssertionError('touched AWS')))
        assert oc.main(['--state', str(tmp_path / 's.json'), '--no-repair', '--no-mail']) == 0

    def test_no_key_reads_as_unknown(self):
        assert oc.server_state({}) is None

    def test_a_refused_key_reads_as_blocked(self, monkeypatch):
        monkeypatch.setattr(oc, '_aws', lambda args, env: ('', 'An error occurred (AuthFailure) when calling'))
        assert oc.server_state(ENV) == oc.BLOCKED

    def test_another_aws_error_is_unknown_not_blocked(self, monkeypatch):
        monkeypatch.setattr(oc, '_aws', lambda args, env: ('', 'Could not connect to the endpoint URL'))
        assert oc.server_state(ENV) is None


class TestChannels:
    def test_the_phone_goes_first_and_email_also_goes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(oc, 'send_push', lambda m, e: calls.append('push') or True)
        monkeypatch.setattr(oc, 'send_email', lambda m, e: calls.append('email') or True)
        assert oc.send(('s', 'b'), {}) is True and calls == ['push', 'email']

    def test_one_channel_is_enough(self, monkeypatch):
        monkeypatch.setattr(oc, 'send_push', lambda m, e: False)
        monkeypatch.setattr(oc, 'send_email', lambda m, e: True)
        assert oc.send(('s', 'b'), {}) is True

    def test_nothing_configured_is_not_sent(self):
        assert oc.send(('s', 'b'), {}) is False

    def test_a_down_alert_is_high_priority_and_back_up_is_not(self, monkeypatch):
        got = {}
        monkeypatch.setattr(oc, '_post', lambda url, data, headers=None: got.update(data=data.decode()) or True)
        env = {'PUSHOVER_TOKEN': 't', 'PUSHOVER_USER': 'u'}
        assert oc.send_push(('HC LOMBARDO DOWN: x', 'b'), env) is True
        assert 'priority=1' in got['data']
        oc.send_push(('HC LOMBARDO BACK UP after 1 hour', 'b'), env)
        assert 'priority=0' in got['data']


class TestNothingPrivateInThisRepository:
    """It is public. Everything specific lives in secrets."""
    def test_no_private_detail_in_the_tree(self):
        names = ['outside_check.py', 'README.md', os.path.join('.github', 'workflows', 'outside-check.yml')]
        text = ''.join(open(os.path.join(ROOT, n), encoding='utf-8').read() for n in names)
        for bad in ('i-0', 'us-east', 'AKIA', '@hclombardo', 'proton', 'gmail.com'):
            assert bad not in text, bad
