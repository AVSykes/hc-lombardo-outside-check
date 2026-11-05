# Outside check

Checks hclombardo.com from outside its own hosting every 30 minutes, and alerts when it is down. It holds this script and nothing else. Every account, key and address is in GitHub secrets.

- `outside_check.py`: the check, the alerts and the repair.
- `test_outside_check.py`: its tests. Run with `python -m pytest`.
- `.github/workflows/keepalive.yml`: stops GitHub turning the schedule off after 60 quiet days.
