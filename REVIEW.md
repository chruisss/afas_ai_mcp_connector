# Review rules

What the review lanes hold a PR in this repo to. AGENTS.md has the house rules; this file says what is worth raising.

## Important (blocker or major)
- A write tool (insert, update, delete) registered without `AFAS_ALLOW_WRITES=true`, writes that skip validation by default, or a weakened `afas_validate_payload`.
- An AFAS token, client secret, encryption key, member id or `.env` value anywhere in code, tests, docs, logs or the diff (tests use the fictional values in `tests/conftest.py`).
- A new dependency that is not MIT, BSD, Apache, PSF or ISC licensed, or that is unlicensed or copyleft.
- Changed behaviour without a matching update to `docs/reference/tools.md` or `docs/reference/configuration.md`.
- New behaviour without tests that drive the tools through the in-process MCP client against the fake AFAS (`tests/test_server.py`) or the login over HTTP (`tests/test_oauth.py`), or any drop below 100% coverage.
- A bundled connector catalogue or field list (the server discovers them from AFAS metainfo), a new tool, transport or way of authenticating to AFAS nobody asked for; in particular a shared AFAS credential such as a classic token.
- A commit or PR title that is not Conventional Commits.
- A check removed from or weakened in `.github/workflows/ci.yaml` without a reason in the PR.
- A claim of AFAS behaviour that the tests cannot verify, without the PR saying so.
- A deployment guide for one specific platform; the docs stay with Docker Compose behind any reverse proxy.

## Minor
- `TYPE_CHECKING` blocks, `from __future__ import annotations`, or syntax newer than Python 3.11.
- A public item without a Google docstring, a logger not named per class, or a module-level function with a leading underscore.
- A function that mutates its arguments instead of returning a value.
- A PR that bundles more than one logical change.

## Skip
- Style, import order and formatting that `ruff` owns (single quotes, 120 columns).
- Findings that `ty` or the tests in CI already report.
- Dependabot bumps and lockfile churn.
- More than three nits per lane.
