#!/usr/bin/env python3
"""The outside check (M25, April, 2026-11-04).

On 2026-11-03 the production server stopped and stayed off for 27 hours 55
minutes, and nobody was told. The uptime check and its failure mail both ran
on that server, so they stopped with it, and the account had no alarms. This
runs on GitHub, from outside AWS, every 30 minutes.

UP means our backend answered in its own format, including a refusal: a wrong
password reply is a working login route. DOWN means a 5xx, a timeout, or the
gateway's own {"message": "Not Found"} or {"message": "Service Unavailable"},
neither of which ever comes from our code.

An alert goes out after 2 failures in a row, once an hour while it stays down,
and once when it comes back with the total downtime. It goes to the phone first
and to email second, because an email alone gets buried. Standard library only,
and nothing here prints the account, the address or a password.

AUTO-REPAIR acts only when the site is already down, so it can never interrupt
a working server. A stopped server gets started, with a key that can only start
and read that one server. An AWS account that refuses the key cannot be repaired
from here, so it alerts at once in plain words. Every attempt is an alert.

    python scripts/monitor/outside_check.py --state state.json
    python scripts/monitor/outside_check.py --state s.json --base-url https://broken.invalid
"""
import argparse
import json
import os
import secrets
import smtplib
import ssl
import subprocess
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

BASE_URL = 'https://hclombardo.com'
TIMEOUT_SECONDS = 25
FAILS_BEFORE_MAIL = 2
REMINDER_EVERY = timedelta(minutes=55)    # runs come 30 minutes apart, and GitHub starts them late
FALLBACK_LOGIN = 'no-such-user@example.invalid'
PACIFIC = ZoneInfo('America/Los_Angeles')
BLOCKED = 'blocked'                       # AWS refused the key: closed, suspended or unpaid
ACCOUNT_REFUSALS = ('AuthFailure', 'UnrecognizedClient', 'InvalidClientTokenId', 'OptInRequired',
                    'PendingVerification', 'Blocked', 'AccountProblem', 'SignatureDoesNotMatch')


def fetch(url, data=None):
    """(status, body text, error). A status of None means no answer came back."""
    headers = {'User-Agent': 'HCLombardo-outside-check/1.0'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            return r.status, r.read(200000).decode('utf-8', 'replace'), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(200000).decode('utf-8', 'replace'), None
    except Exception as e:
        return None, '', type(e).__name__


def backend_refusal(status, body, allowed):
    """None when this is our backend refusing in its own format, else the reason."""
    if status is None:
        return 'no answer'
    if status >= 500:
        return 'HTTP %d' % status
    try:
        reply = json.loads(body)
    except ValueError:
        return 'HTTP %d, not our JSON' % status
    if not isinstance(reply, dict):
        return 'HTTP %d, not our JSON' % status
    if 'message' in reply and 'success' not in reply:
        return 'the gateway said "%s"' % str(reply['message'])[:60]
    if reply.get('success') is False and reply.get('error') and status in allowed:
        return None
    return 'HTTP %d, unexpected reply' % status


def check_home(base):
    status, body, err = fetch(base + '/')
    if status is None:
        return 'no answer (%s)' % err
    if status != 200:
        return 'HTTP %d' % status
    return None if 'id="root"' in body else 'HTTP 200, but not our page'


def check_api(base):
    status, body, err = fetch(base + '/api/auth/me')
    why = backend_refusal(status, body, {401})
    return '%s (%s)' % (why, err) if err else why


def check_login(base, account):
    # A new wrong password every run. It is never right, and it is never stored.
    payload = json.dumps({'email': account, 'password': secrets.token_urlsafe(24)}).encode('utf-8')
    status, body, err = fetch(base + '/api/auth/login', payload)
    # 429 is the account's own lockout or the rate limit, still our backend answering.
    why = backend_refusal(status, body, {401, 429})
    return '%s (%s)' % (why, err) if err else why


def run_checks(base, account):
    """{name: None when fine, or the reason it failed}."""
    return {'Home page': check_home(base), 'API': check_api(base), 'Login': check_login(base, account)}


def _when(iso):
    return datetime.fromisoformat(iso).astimezone(PACIFIC).strftime('%b %d, %I:%M %p PT')


def _span(seconds):
    minutes = int(round(seconds / 60.0))
    hours, minutes = divmod(minutes, 60)
    if hours:
        return '%d hour%s %d minute%s' % (hours, '' if hours == 1 else 's', minutes, '' if minutes == 1 else 's')
    return '%d minute%s' % (minutes, '' if minutes == 1 else 's')


def _failed_lines(results):
    return '\n'.join('%s: %s' % (name, why) for name, why in results.items() if why)


def step(state, results, now):
    """The whole decision, with no network in it. (new state, mail or None).
    Mail is (subject, body). The caller records `alerted` once a mail really went."""
    state = dict(state or {})
    failed = any(results.values())
    stamp = now.isoformat()
    if not failed:
        mail = None
        if state.get('alerted'):
            total = (now - datetime.fromisoformat(state['down_since'])).total_seconds()
            mail = ('HC LOMBARDO BACK UP after %s' % _span(total),
                    'hclombardo.com is answering again.\n\nDown since %s\nBack at %s\nTotal downtime: about %s\n\n'
                    'Timed from the first failed check to the first good one. Checks run every 30 minutes, so it '
                    'can be off by up to about half an hour.\n' % (_when(state['down_since']), _when(stamp), _span(total)))
        return {'status': 'up', 'last_check': stamp}, mail

    if not state.get('down_since'):
        state = {'down_since': stamp, 'fails': 0, 'alerted': False, 'last_mail': None}
    state.update(status='down', last_check=stamp, fails=int(state.get('fails') or 0) + 1)
    since = _when(state['down_since'])
    mail = None
    if not state['alerted']:
        if state['fails'] >= FAILS_BEFORE_MAIL:
            mail = ('HC LOMBARDO DOWN: the site failed %d checks in a row' % state['fails'],
                    'What failed:\n%s\n\nFailing since %s\n\nYou will get one reminder an hour until it is back, then '
                    'one email with the total downtime.\n' % (_failed_lines(results), since))
    elif now - datetime.fromisoformat(state['last_mail']) >= REMINDER_EVERY:
        total = (now - datetime.fromisoformat(state['down_since'])).total_seconds()
        mail = ('HC LOMBARDO DOWN: still down after %s' % _span(total),
                'What is failing now:\n%s\n\nFailing since %s\n' % (_failed_lines(results), since))
    return state, mail


def _post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return 200 <= r.status < 300


def send_push(mail, env):
    """To the phone, which does not depend on email, through Pushover. True when
    it took it, None when it is not set up."""
    sent = None
    urgent = not mail[0].startswith('HC LOMBARDO BACK UP')
    if env.get('PUSHOVER_TOKEN') and env.get('PUSHOVER_USER'):
        try:
            sent = _post('https://api.pushover.net/1/messages.json', urllib.parse.urlencode({
                'token': env['PUSHOVER_TOKEN'], 'user': env['PUSHOVER_USER'], 'title': mail[0][:250],
                'message': mail[1][:1000], 'priority': 1 if urgent else 0}).encode('utf-8'))
        except Exception as e:
            print('push failed: %s' % type(e).__name__)
            sent = False
    return sent


def send_email(mail, env):
    """Through whatever SMTP the secrets name, never through AWS, so a stopped
    AWS account cannot silence it. True when it went, None when not set up."""
    host, to = env.get('SMTP_HOST'), env.get('MAIL_TO')
    if not host or not to:
        return None
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = mail[0], env.get('MAIL_FROM') or env.get('SMTP_USER'), to
    msg.set_content(mail[1])
    try:
        with smtplib.SMTP(host, int(env.get('SMTP_PORT') or 587), timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(env.get('SMTP_USER'), env.get('SMTP_PASS'))
            s.send_message(msg)
        return True
    except Exception as e:
        print('email failed: %s' % type(e).__name__)     # the type only: a message can carry an address
        return False


def send(mail, env=os.environ):
    """True when at least one channel took it."""
    results = (send_push(mail, env), send_email(mail, env))
    if results == (None, None):
        print('alert due, but no alert settings are configured')
    return any(results)


def _aws(args, env):
    """(stdout, error words) from the AWS CLI on the runner, using the limited key."""
    run_env = dict(os.environ, AWS_ACCESS_KEY_ID=env.get('REPAIR_AWS_KEY_ID') or '',
                   AWS_SECRET_ACCESS_KEY=env.get('REPAIR_AWS_SECRET') or '', AWS_DEFAULT_REGION=env.get('REPAIR_REGION') or '', AWS_PAGER='')
    run_env.pop('AWS_PROFILE', None)
    r = subprocess.run(['aws'] + args + ['--output', 'text'], capture_output=True, text=True, env=run_env, timeout=60)
    return r.stdout.strip(), '' if r.returncode == 0 else r.stderr.strip()


def server_state(env):
    """'running', 'stopped' and so on, BLOCKED when AWS refuses the key, None when
    this cannot tell: no key, or a problem reaching AWS."""
    if not all(env.get(k) for k in ('REPAIR_AWS_KEY_ID', 'REPAIR_AWS_SECRET', 'REPAIR_INSTANCE', 'REPAIR_REGION')):
        return None
    try:
        out, err = _aws(['ec2', 'describe-instances', '--instance-ids', env.get('REPAIR_INSTANCE') or '',
                         '--query', 'Reservations[0].Instances[0].State.Name'], env)
    except Exception as e:
        print('aws read failed: %s' % type(e).__name__)
        return None
    if err:
        return BLOCKED if any(w in err for w in ACCOUNT_REFUSALS) else None
    return out or None


def start_server(env):
    try:
        _, err = _aws(['ec2', 'start-instances', '--instance-ids', env.get('REPAIR_INSTANCE') or ''], env)
    except Exception as e:
        return False, type(e).__name__
    return not err, 'AWS refused the start' if err else ''


def repair(state, new_state, results, now, env):
    """Runs only when a check already failed. (repair alert or None). Pure apart
    from the two AWS calls, and it records what it did on new_state."""
    aws = server_state(env)
    new_state['server'] = aws
    failing = _failed_lines(results)
    if aws == BLOCKED:
        if state.get('account_alerted'):
            new_state['account_alerted'] = True
            return None
        new_state['account_alerted'] = True
        return ('HC LOMBARDO DOWN: AWS is refusing the account',
                'The site is failing and AWS refused our key. The account may be closed, suspended or blocked over '
                'billing. This cannot be repaired automatically.\n\nSign in to AWS and check Billing first.\n\n'
                'What failed:\n%s\n' % failing)
    new_state['account_alerted'] = False
    if aws != 'stopped' or int(new_state.get('fails') or 0) < FAILS_BEFORE_MAIL:
        return None
    last = state.get('last_start')
    if last and now - datetime.fromisoformat(last) < REMINDER_EVERY:
        new_state['last_start'] = last
        return None
    ok, why = start_server(env)
    new_state['last_start'] = now.isoformat()
    if ok:
        return ('HC LOMBARDO DOWN: the server was stopped, starting it now',
                'What broke: the server was stopped.\nWhat was done: the outside check started it. It takes about '
                'two minutes to come up. The next check says whether the site is back.\n\nWhat failed:\n%s\n' % failing)
    return ('HC LOMBARDO DOWN: the server is stopped and could not be started',
            'What broke: the server is stopped.\nWhat was done: the outside check tried to start it and could not (%s). '
            'Sign in to AWS and start it.\n\nWhat failed:\n%s\n' % (why, failing))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', required=True)
    ap.add_argument('--base-url', default=BASE_URL)
    ap.add_argument('--no-mail', action='store_true')
    ap.add_argument('--no-repair', action='store_true', help='a test against another address must not touch the server')
    args = ap.parse_args(argv)
    try:
        with open(args.state, encoding='utf-8') as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    now = datetime.now(timezone.utc)
    results = run_checks(args.base_url.rstrip('/'), os.environ.get('MONITOR_LOGIN') or FALLBACK_LOGIN)
    for name, why in results.items():
        print('%-10s %s' % (name, 'ok' if why is None else 'FAILED: ' + why))
    new_state, mail = step(state, results, now)
    repair_alert = None
    if any(results.values()) and not args.no_repair:
        repair_alert = repair(state, new_state, results, now, os.environ)
    if repair_alert and not args.no_mail:
        print('repair alert:', repair_alert[0])
        if not send(repair_alert):
            print('repair alert could not be sent')
    sent = None
    if mail:
        print('mail due:', mail[0])
        sent = False if args.no_mail else send(mail)
        if sent and new_state.get('status') == 'down':
            new_state.update(alerted=True, last_mail=now.isoformat())
        elif not sent and new_state.get('status') == 'up':
            new_state = state        # not told yet, so the next run tries the back up mail again
    with open(args.state, 'w', encoding='utf-8') as f:
        json.dump(new_state, f, indent=1)
    # A red run is GitHub's own email, the second way to be told when ours cannot go out.
    return 1 if (mail and not sent) else 0


if __name__ == '__main__':
    sys.exit(main())
