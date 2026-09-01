## What this changes

<!-- Describe the change and why it is needed. -->

## How it was verified

<!-- Paste the exact commands you ran, and what they printed. -->

## Checklist

- [ ] `.venv/bin/python3 -m pytest -q` passes locally
- [ ] `.venv/bin/python3 -m ruff check .` reports no new violations
- [ ] Fixtures under `fixtures/` were updated if the change needs new sample data, and no existing fixture directory was renamed or restructured
- [ ] No secrets, tokens, API keys, real league ids or personal email addresses are in this diff
- [ ] `config/league.yaml` is not part of this diff (it is gitignored on purpose)
