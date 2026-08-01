# Hermes code review — outcome record

**Date:** 2026-08-01 · **Reviewed revision:** `b5f8996cc` · **Fixes landed on:** `15cb86eba`
**Result:** 4 fixes merged to `main` and pushed to `fork` (`github.com/powriej/hermes-agent`)
at `4a917bd71`. Upstream `origin` (NousResearch) untouched.

---

## 1. What was found and what happened to it

A read-only review of `b5f8996cc` produced 18 findings. Partway through, a `hermes update`
moved the checkout to `15cb86eba` — **8,858 commits** of upstream change. Rather than port
stale line anchors into a much-changed codebase, every finding was **re-derived against the new
HEAD**. Most had been fixed upstream in the interim; the ones that survived were fixed here.

### Fixed in this repo (4 commits, merged + pushed)

| Commit | Severity | Fix |
|---|---|---|
| `f95a9c65f` | **P1** | sudo password no longer handed to a shell that won't read it |
| `7822b7c7b` | **P2** | cron policy no longer judges live gateway users |
| `7f79c8055` | P3 | `--include-integration` actually selects integration tests |
| `4a917bd71` | P3 | CI durations merge no longer discards five of six slices |

**P1 — `tools/terminal_tool.py`.** `sudo_stdin` is piped to the top-level shell, not to sudo.
`sudo -S` consumes one line only when it actually prompts; when it doesn't, the line falls
through and the operator's password comes back as command output — into model context,
transcripts, and logs. `SUDO_PASSWORD` is deliberately scrubbed from the child environment
(`environments/local.py`), so this handed it back through a different door. Two gaps closed:
the `_sudo_nopasswd_works()` probe was gated on `not has_configured_password` — skipped in
exactly the leaking case — and `sudo -n`/`--non-interactive` (which no host probe can detect,
being a property of the command) now withholds the password. **This host has passwordless sudo
and was therefore the vulnerable configuration**; `sudo -n true; cat` returned the password
verbatim before the fix and returns nothing now.

**P2 — `tools/approval.py`.** `HERMES_CRON_SESSION` is process-wide and never cleared, and the
gateway runs the cron ticker in its own process. `check_execute_code_guard` computed
`is_gateway`/`is_ask` and then checked the cron marker unconditionally, so after any cron job
a live user's `execute_code` took the cron branch: BLOCKED with a "no user present" message
under the default mode, or **silently approved with no prompt** under `cron_mode: approve`.
Now gated on `not is_gateway and not is_ask`. Real standalone cron is unchanged.

### Fixed upstream before we got there (no action needed)

| Finding | Evidence at `15cb86eba` |
|---|---|
| HCR-003 job-store lost updates | `cron/jobs.py:104` — `RLock` **plus** cross-process `flock` on `.jobs.lock` (#60703); all three mutators take it. Exactly the layering the proposal specified. |
| HCR-005 compression drops a turn's transcript | `conversation_compression.py:1845-1850` now skips session rotation entirely on an aborted/no-op compression, so nothing zeroes the flush cursor. |
| HCR-006 test runner picks an unusable venv | `scripts/run_tests.sh` probes `import pytest` and skips venvs lacking it. |
| HCR-007 stale testing documentation | `_isolate_plugin`, `isolate_timeout`, `--no-isolate`, and the xdist claims are all gone from `AGENTS.md`; bare pytest flags now work without `--`. Verified by running the documented forms. |
| HCR-011 skill PRs unvalidated pre-merge | `scripts/ci/classify_changes.py:42` — the site lane is now `("website/", "skills/", "optional-skills/")`. |

### Obsolete by design change

- **HCR-004** (profile cron leaking `HERMES_HOME`) and the **uncommitted WIP** in `stash@{0}`:
  upstream redesigned cron to be per-profile (`cron/scheduler.py:574-578`, #4707). Per-job
  profile switching is gone and `_job_profile_context` no longer exists.
- **HCR-009** (untrusted-result fence): the `startswith` fail-open guard no longer exists in
  that form. Needs fresh derivation if still wanted — not a port.

### Still outstanding (not re-derived)

HCR-010 token accounting on truncated responses · HCR-012 MCP stdio PID misattribution ·
HCR-013 browser reaper trusting attacker-writable temp state · HCR-014 max-iteration
scaffolding persisting into the next turn · HCR-016 steer scan crossing a turn boundary.

All cite `b5f8996cc` line numbers. Given how much upstream fixed independently, re-derive
before writing any code against them.

---

## 2. Validation

- Full suite on the merged tree: **23,740 passed, 12 failed** (+3 vs 23,737 pre-fix — the new
  regression tests). **All 12 failures are pre-existing**: 11 reproduce on a pristine clone of
  `15cb86eba`; membership varies between `test_pin_peer_name` and `test_tui_gateway_server`,
  both of which pass in isolation on *both* trees — load-dependent flakes under 16-way
  parallelism.
- `tests/tools/` 5,181 passed · approvals + cron 509 passed · `ruff` clean on every changed file.
- 9 behavioral regression tests added. The runner test was **verified to fail with the fix
  reverted** — an earlier version passed for the wrong reason, because a probe under `tmp_path`
  makes pytest resolve rootdir to the temp tree so the repo's `addopts` never applied.
- Two pre-existing sudo tests were made hermetic; they silently depended on the developer's own
  sudoers config.

### Test environment
`pytest-asyncio==1.3.0` and `pytest-timeout==2.4.0` — declared at `pyproject.toml` dev pins but
absent — were installed into `venv/` with `uv` (the venv has no `pip`). `venv/` is gitignored.
This unblocked ~2,447 previously-unrunnable async tests and removed the need for an `addopts`
override, giving true CI parity.

---

## 3. Your stashed work

`stash@{0}` (`hermes-update-autostash-20260801-105036`) holds three files: your cross-profile
cron delivery WIP plus two agent edits from the interrupted implementation run. A plain-text
backup lives outside the repo.

**It was deliberately never popped.** The WIP calls `_job_profile_context`, which no longer
exists at this HEAD — popping would produce code referencing an undefined symbol. It also had a
latent crash: `job.get("profile", "").strip()` raises `AttributeError` because `create_job`
stores `"profile": None` explicitly (`cron/jobs.py:705`), and `tick()` swallows it as a delivery
error — every profile-less delivering cron job would have stopped delivering.

**Recommendation: drop it.** Upstream's per-profile cron design solves the problem it was
written for. Kept anyway, because recommending and destroying are different things.

---

## 4. Operational notes

- **A `hermes update` mid-session is disruptive**: it autostashes tracked work, moves HEAD, and
  removed the untracked review artifacts that existed at the time. There is no config knob —
  the banner check is passive and `hermes update` is an explicit command — so the mitigation is
  simply not to run it during long agent sessions.
- **This file is untracked on purpose**, to keep session-specific notes out of a history that
  tracks upstream. It is backed up outside the repo, because the last update deleted exactly
  this kind of file. Commit it if you'd rather it survive in the fork.
- **Upstream moves fast** — `origin/main` was already past `15cb86eba` at push time. If the P1
  and P2 fixes should reach NousResearch rather than just the fork, they need a PR; both
  defects still exist in upstream's tree.
