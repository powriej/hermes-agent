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

### The remaining five — re-derived and fixed (2026-08-01)

All five were re-derived at `15cb86eba`; all five still existed. Four are fixed:

| Commit | Finding | Fix |
|---|---|---|
| `29e0b25fb` | HCR-013 | Browser reaper skips socket dirs this uid doesn't own (and symlinks). Upstream had already added `_verify_reapable_browser_daemon` ("is this PID a browser daemon"); this adds "is this directory even ours", which that cannot answer. |
| `56a68c0af` | HCR-014 | Iteration-summary request flagged `_iteration_summary_request` so it never reaches the durable transcript. |
| `26c544096` | HCR-016 | Steer scan bounded at `current_turn_user_idx`, so it can no longer rewrite a previous turn's tool result or invalidate the cache prefix. |
| `c36d87bd6` | HCR-012 | MCP refuses to record a stdio child's pgid when it equals `os.getpgid(0)`. Upstream's `_filter_mcp_children` is a denylist; this makes the "killpg signals Hermes itself" case structurally impossible instead of dependent on that list staying complete. |

**HCR-010 (token accounting on truncated responses) — NOT FIXED, deliberately.**

The defect is real and re-confirmed: the three `finish_reason == "length"` exits
(`agent/conversation_loop.py:2961`, `:3032`, `:3083`) all precede the only site that
increments session token counters (`:3241-3250`), so truncated responses — typically the
largest completions, since they hit the output cap — contribute nothing to totals, cost
estimates, or the session DB.

It is not fixed because the minimal correct fix is not small. `canonical_usage` is not
normalized until `:3148`, *after* the length branch, so the accounting cannot simply be
hoisted — normalization has to move with it, inside the hottest path of a ~7,000-line
function, touching streaming and provider-fallback interactions that this review explicitly
never assessed (see "not assessed" in the coverage notes). The payoff is analytics accuracy:
no data loss, no correctness impact, no security impact. That risk/benefit does not justify
an under-validated refactor of the core loop.

**For whoever picks it up:** either hoist normalization + accounting above `:2823`, or
extract the accounting into a helper and call it at each of the three exits. Either way it
needs streaming and provider-fallback coverage that does not exist today, and a test
asserting the same response is not counted twice across the continuation retry.

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
- **Upstream moves fast** — `origin/main` was already past `15cb86eba` at push time.

## 5. Upstream PR — DEFERRED (operator decision, 2026-08-01)

The P1 (sudo password) and P2 (cron approval) fixes live **only in this fork**. Both defects
still exist in `NousResearch/hermes-agent`; upstreaming them was considered and **deferred**.

Not an oversight — recorded so it is not re-raised as an open action. Standing facts for
whenever it is revisited:

- This fork is protected; the exposure is to other Hermes operators, not to this machine.
- The P1 is the one with reach beyond a single user: on any host with passwordless sudo or a
  warm sudo timestamp, a configured `SUDO_PASSWORD` is echoed into command output, and from
  there into model context, transcripts, and logs. No malicious model is required.
- Both fixes are self-contained, carry regression tests, and rebase cleanly in principle —
  though upstream moves fast enough (`origin/main` was already past `15cb86eba` at push time)
  that they will need re-verification against whatever HEAD is current at PR time.
- SECURITY.md §3.1 classes the sudo issue as in-scope credential exfiltration ("via a mechanism
  that should have prevented it"), so it would go through the private security channel rather
  than a plain PR. The cron approval issue is a functional/trust-model defect, not a §3.1
  vulnerability, and belongs in a normal PR.
