# PRD: Parallel scheduler v2 hardening

## Status
Draft — for `/laplace:intake`.

## Context

The parallel scheduler (`/laplace:run-parallel`, `/laplace:pipeline` parallel phase, v0.4.0) and the worktree isolation layer (v0.3.0) exposed four follow-up gaps during the v0.5.0 dogfooding cycle and security review:

1. **Main auto-merge**: each review-passed issue halts at `merge-wait`; the human merges N branches for N issues. For a 9-issue PRD that's 9 manual merges. An opt-in path to collapse this is missing.
2. **File-overlap detection (R-4 from the parallel PRD)**: two parallel issues touching the same file both reach review-passed; their merges conflict at main. v1 deferred this; the gap remains.
3. **Orphan worktree reconcile (security finding 4, low)**: a crash between `git worktree add` and parent-log append leaves a worktree on disk that `git worktree prune` cannot recover (prune only removes worktrees whose directory is gone, not the reverse). No laplace command sweeps these.
4. **Load-aware rate limiting**: `max_parallel` is a static cap. On a loaded machine, dispatching `max_parallel` concurrent full-issue loops can freeze the box. There is no pre-dispatch idle-resource check, no adaptive cap, no sequential fallback.

Multi-PRD parallel pipelines are explicitly deferred (separate future PRD).

## Problem

- 9 manual merges for a 9-issue PRD (gap 1).
- Silent file conflicts surface late at merge-wait (gap 2).
- Orphan worktrees accumulate across crashes, invisible to status, recoverable only by hand (gap 3).
- A static `max_parallel=2` can over-commit on a busy laptop or under-commit on an idle workstation (gap 4).

## Goals

- **(1) Opt-in integration-branch auto-merge at queue-exhaustion**: when `merge_policy: auto-merge-branch` is set and the queue exhausts, the scheduler auto-merges the integration branch `laplace/queue-<run-id>` into `main` IF tests pass and no conflict. The human still owns the release gate. Default OFF (`wait-for-human-merge` unchanged). Never auto-merges a conflict; never bypasses tests.
- **(2) Advisory file-overlap warning**: issues MAY declare `touches: [globs]` (optional, PRD-authored). Before each wave, the scheduler warns (does NOT block) when two ready issues' `touches` overlap. Dispatch proceeds; real conflicts still surface at merge-wait. The warning is informational, not a gate.
- **(3) Orphan worktree reconcile**: `/laplace:reconcile-worktrees` scans all run logs for `worktree_path`, cross-references `git worktree list --porcelain`, reports worktrees with no live parent run. `--sweep` removes them (after confirmation). `status` shows an orphan count when non-zero.
- **(4) Load-aware rate limiter**: before each wave dispatch, sample `os.getloadavg()` (1-min) divided by `os.cpu_count()`. If above `limits.load_threshold` (default 0.7), reduce the dispatch cap for this wave; if load is severe (above `load_severe`, default 1.5) or `max_parallel` would be 0 after reduction, defer the whole wave (`wave-deferred:high-load`) — effectively a sequential fallback when the machine is saturated. Windows (no `getloadavg`) skips the check with a warn.

## Non-goals

- **Direct auto-merge to main per-issue** — gap 1 collapses N merges into ONE integration→main merge at queue-exhaustion, behind a policy flag + tests + no-conflict. Per-issue main auto-merge (each review-passed branch straight to main) is NOT added — too dangerous, conflicts cascade.
- **Blocking on file overlap** — gap 2 is advisory only. Prediction is unreliable pre-dev; blocking would over-restrict.
- **Multi-PRD parallel pipelines** — deferred (separate PRD).
- **Cross-host / distributed rate limiting** — local single-host only.
- **`psutil` dependency** — stdlib only. Memory pressure check is best-effort via `shutil.disk_usage` (worktree disk) + `os.getloadavg`; no full RAM probe (psutil would be a new dep, out of scope).
- **Predictive file-overlap from code analysis** — `touches` is PRD-declared globs, not inferred from reading the codebase.

---

## Task: Integration-branch auto-merge to main at queue-exhaustion

### Background
`auto-merge-branch` policy (v0.2.0) already stacks review-passed issue branches into `laplace/queue-<run-id>`. Today the human merges that integration branch to main. This task adds the optional final hop: integration → main, at queue-exhaustion, gated by tests + no-conflict.

### Scope
**In Scope:**
- `scripts/queue_runner.py` + `scripts/parallel_queue.py` — at `queue-exhausted`, if `merge_policy == "auto-merge-branch"` AND config `limits.auto_merge_main_at_exhaustion` is true (default false): run `python3 -m pytest -q`; if pass, attempt `git checkout main && git merge --no-ff laplace/queue-<run-id>`; if clean → record `main-merged:<sha>` in the parent log outcome; if conflict → `git merge --abort`, halt `main-merge-conflict:<run-id>` (do NOT force).
- `.harness/config.yml` — add `auto_merge_main_at_exhaustion: false` under `policy`. `state.load_config` parses as bool.
- Protected-ref guard: the merge target is hardcoded `main` (fallback `master`); never derived from config. Structural (same pattern as the existing auto-merge-branch protected-ref guard).
- policy.check_command routing for every git op.
- Tests + selftest cases: clean merge → main-merged; conflict → halt; tests fail → skip main-merge, still queue-exhausted (integration branch intact for human).

**Out of Scope:**
- Per-issue main auto-merge.
- Pushing main (still the release gate's job, via `/laplace:release`).

### Acceptance Criteria
- AC-AM-001: with `auto_merge_main_at_exhaustion: true` + `merge_policy: auto-merge-branch`, queue-exhaustion runs tests; if pass + clean merge, main advances to include the integration branch; parent log records `main-merged:<sha>`.
- AC-AM-002: tests fail → no main-merge attempt; outcome `queue-exhausted` (integration branch preserved); human resolves.
- AC-AM-003: merge conflict → `git merge --abort`; outcome `main-merge-conflict:<run-id>`; main unchanged; human resolves.
- AC-AM-004: default config (`auto_merge_main_at_exhaustion: false`) → behavior unchanged (human merges integration to main).
- AC-AM-005: protected-ref guard — merge target hardcoded main/master; never config-derived.

### Risk / Release Impact
- Risk Level: high (auto-advances main)
- Release Type: minor
- Security Sensitivity: high (main mutation; tests + no-conflict + structural target guard are the boundary)

---

## Task: Advisory file-overlap warning

### Background
Issues that touch the same file will conflict at merge. v1 deferred detection (R-4). This task adds an optional PRD-declared `touches` field and a pre-dispatch advisory warning. It does NOT block — file prediction pre-dev is unreliable, and blocking would over-restrict legitimate parallel work.

### Scope
**In Scope:**
- `scripts/intake.py` — parse `Touches: <glob>, <glob>` lines in PRD task sections into an optional `touches: List[str]` field on the issue record (mirrors `depends_on` parsing).
- `scripts/parallel_queue.py` — before each wave dispatch, compute pairwise overlap of `touches` globs across the ready set (using `fnmatch`). On overlap, append an advisory `overlap_warning: [(issue_a, issue_b, shared_glob), ...]` to the wave entry. Dispatch proceeds unchanged.
- `/laplace:status` — show the overlap warning when present in the active parallel run.
- Tests + selftest.

**Out of Scope:**
- Blocking dispatch on overlap.
- Inferring `touches` from codebase analysis.
- Overlap detection across NON-ready issues (only the ready set matters for the current wave).

### Acceptance Criteria
- AC-FO-001: `intake` parses `Touches: src/auth/**, src/db/**` into issue `touches`.
- AC-FO-002: parallel_queue wave entry includes `overlap_warning` listing (a, b, glob) pairs when two ready issues' touches intersect via fnmatch.
- AC-FO-003: overlap does NOT block dispatch (advisory only).
- AC-FO-004: status surfaces the warning.

### Risk / Release Impact
- Risk Level: low (advisory only)
- Release Type: patch
- Security Sensitivity: low

---

## Task: Orphan worktree reconcile

### Background
Security finding 4 (low): a crash between `git worktree add` and parent-log append orphans a worktree. `git worktree prune` only removes worktrees whose directory is gone, not the reverse — so it does NOT recover this case. A laplace-side reconcile is needed.

### Scope
**In Scope:**
- `scripts/parallel_queue.py` (or a new `scripts/reconcile.py`): `cmd_reconcile_worktrees(args)`:
  - Scan all `.harness/state/runs/*.json` for `worktree_path` (child run logs).
  - Run `git worktree list --porcelain` (policy.check_command).
  - A worktree is "live" if some non-finalized run log references it. Otherwise orphan.
  - Report orphans (path + last-known issue id from the orphan's run log if recoverable).
  - `--sweep`: remove orphan worktrees (`git worktree remove --force` for orphans only; never touch live ones). Confirmation prompt unless `--yes`.
- `commands/reconcile-worktrees.md` + `skills/reconcile-worktrees/SKILL.md`.
- `/laplace:status` — show `Orphan worktrees: N` when non-zero.
- Tests + selftest.

**Out of Scope:**
- Automatic sweep on every status run (too surprising; explicit command only).
- Recovering orphan worktrees whose run log is ALSO gone (unrecoverable; report as manual).

### Acceptance Criteria
- AC-OW-001: `/laplace:reconcile-worktrees` lists worktrees on disk with no live parent run.
- AC-OW-002: `--sweep --yes` removes orphans; never removes a worktree referenced by a non-finalized run.
- AC-OW-003: status shows orphan count when non-zero; byte-identical when zero.
- AC-OW-004: a worktree whose run log is missing is reported as "manual recovery" (not auto-swept).

### Risk / Release Impact
- Risk Level: medium (destructive — removes worktrees)
- Release Type: minor
- Security Sensitivity: medium (the live-orphan boundary is the safety surface)

---

## Task: Load-aware rate limiter (sequential fallback)

### Background
`max_parallel` is a static cap. On a loaded machine, dispatching the full cap freezes the box. On an idle workstation, the cap under-commits. A pre-dispatch load sample lets the scheduler adapt: reduce the wave's dispatch count when load is high, defer entirely (sequential fallback) when saturated.

### Scope
**In Scope:**
- `scripts/parallel_queue.py` — before computing `to_dispatch`, call `_load_headroom(target)`:
  - `cpu = os.cpu_count() or 1`.
  - `load1 = os.getloadavg()[0]` (Unix); on `AttributeError` (Windows) → return None (skip check, warn once per run).
  - `ratio = load1 / cpu`.
  - headroom rules: `ratio < load_threshold` (default 0.7) → full cap; `load_threshold <= ratio < load_severe` (default 1.5) → reduced cap `max(1, max_parallel - ceil((ratio - load_threshold) * max_parallel))`; `ratio >= load_severe` → cap 0 (defer wave).
  - If computed cap is 0 → outcome `wave-deferred:high-load:<ratio>`, dispatch nothing, exit 0 (resumable; the human/model re-invokes after load drops).
  - If reduced cap < requested → record `load_cap: <int>` in the wave entry.
- `.harness/config.yml` — `load_threshold: 0.7`, `load_severe: 1.5` under `limits`. `state.load_config` parses as positive float.
- `os.getloadavg` is stdlib (Unix). `os.cpu_count` stdlib. `shutil.disk_usage` for the worktree disk — optional: if free disk below `min_free_disk_gb` (default 1), also defer. Documented; not a primary signal.
- `/laplace:status` — show `Load: ratio=N (cap M of max_parallel K)` in the active parallel block.
- Tests + selftest (mock `os.getloadavg`).

**Out of Scope:**
- RAM pressure (psutil would be a new dep).
- Cross-host scheduling.
- Dynamic cap changes mid-wave (cap is per-wave; next wave re-samples).
- Throttling individual sub-processes (nice/cgroup).

### Acceptance Criteria
- AC-RL-001: `os.getloadavg` sampled per wave; ratio computed against `os.cpu_count()`.
- AC-RL-002: ratio < `load_threshold` → full `max_parallel` dispatch (unchanged behavior).
- AC-RL-003: `load_threshold <= ratio < load_severe` → reduced cap, recorded as `load_cap` in wave.
- AC-RL-004: ratio >= `load_severe` → `wave-deferred:high-load:<ratio>`, nothing dispatched, exit 0, resumable.
- AC-RL-005: Windows (no `getloadavg`) → skip check with one-time warn; dispatch at static `max_parallel`.
- AC-RL-006: `/laplace:status` shows current ratio + effective cap.
- AC-RL-007: characterization — with load always below threshold, behavior byte-identical to v0.4.0.

### Risk / Release Impact
- Risk Level: low (adaptive cap; never exceeds existing max_parallel; defers safely)
- Release Type: patch
- Security Sensitivity: low (stdlib sampling; no new deps)

---

## Open questions (PM phase)

- Task 1: should the integration→main auto-merge also push main, or leave push to `/laplace:release`? Recommend: leave push to release (don't conflate main-advance with publish). The auto-merge advances local main; release pushes.
- Task 2: should `touches` be required or optional? Recommend optional (most issues won't declare it; that's fine — overlap detection is best-effort).
- Task 4: should `load_severe` default be 1.5 (aggressive) or 2.0 (lenient)? Recommend 1.5 for v1 (conservative; laplace parallel is opt-in already); users bump via config.
- Should all four tasks ship in one release (0.6.0) or split? Recommend one release — they're a coherent "parallel v2 hardening" set and the release-command does it in one step.

## Risk / Release Impact (overall)

- Risk Level: high (Task 1 auto-advances main; the other three are low/medium)
- Release Type: minor (0.6.0)
- Security Sensitivity: high (Task 1 main mutation; Task 3 worktree removal)
