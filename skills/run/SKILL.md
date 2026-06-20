---
name: run
description: Execute one issue loop. Acquires the issue, creates an isolated branch, runs PM phase, routes to dev/review/security phases with evidence capture. Stops at review-passed, blocked, or human-approval-required.
---

# /laplace:run

## Intent

Execute a single issue through the Laplace loop: acquire the issue lock, create an isolated branch, run the PM clarification phase, then route through dev / review / security phases with evidence capture at each gate. The loop stops at `review-passed`, `blocked`, `human-approval-required`, or a hard loop limit. The skill instructs the model; deterministic scaffolding (state transitions, branch setup, evidence writes) is delegated to `scripts/runner.py` which composes `scripts/state.py` and `scripts/policy.py` primitives.

## When to Run

- After `/laplace:approve <issue>` has moved an issue from `draft` to `approved`.
- When an issue is sitting in the `approved` queue and a human has invoked `/laplace:run <issue>` (or `/laplace:run` with no arg to pick the head of the approved queue).
- When resuming an interrupted run whose state is `pm-review`, `ready-for-dev`, or `needs-fix` and whose run lock is free.

Do NOT invoke on a `draft` issue (approve first), a `blocked` issue (resolve the blocker via the exception flow first), or an issue whose run lock is held by an active run.

## What It Does

### Step 1: Start the run

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py start <issue-id>
```

- Validates the issue is in `approved` state.
- Acquires the issue lock (held until `runner.py end`).
- Creates an isolated branch `laplace/<issue-id>` via `git checkout -b`. If the branch already exists, reuses it (idempotent). If the working directory is not a git repo (or git is unavailable), records `BRANCH_SKIPPED:not-a-git-repo` in the run log and proceeds with state transitions only — fail-safe, never crashes.
- Creates the run log at `.harness/state/runs/<run-id>.json`.
- Transitions `approved -> pm-review`.
- Every git invocation is routed through `policy.check_command` first; denied commands abort branch setup as skipped, not crash.

Output reports the run id, branch status (created / reused / skipped), and run log path.

### Step 2: PM phase (active in P3)

Invoke the `laplace-pm-agent` subagent to clarify scope, acceptance criteria, technical notes, and produce a `ready` / `blocked` decision.

- On `ready`:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> pm-review ready-for-dev --summary "<one-line PM decision>"
  ```

- On `blocked`:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> pm-review blocked --summary "<blocker reason>"
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py end <run-id> --outcome blocked
  ```

  Then stop. Surface the blocker to the human.

The PM phase is bounded by `max_pm_clarification_attempts` (default 2) from `.harness/config.yml`. Exceeding the limit without a `ready` decision transitions to `blocked`.

### Step 3: Dev phase (active in P4)

Invoke the `laplace-dev-agent` subagent to implement scoped changes and tests for the issue's acceptance criteria.

Workflow:

1. Transition into dev:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> ready-for-dev in-progress --summary "<one-line plan>"
   ```

2. Dispatch the dev agent (via `Agent(subagent_type: "laplace-dev-agent")` or the runtime's equivalent) with:
   - The issue file path (`.harness/issues/<issue-id>.md`)
   - The run id (so the agent can append evidence)
   - The branch name (`laplace/<issue-id>`)
   - The commit instruction: after capturing test evidence and before reporting `ready-for-review`, commit all working-tree changes on `laplace/<issue-id>` with a conventional-commit message referencing the issue id (mandatory unless `BRANCH_SKIPPED` or policy denies `git commit` — record the reason and proceed)
   - Constraints: stay within issue scope, honor policy deny list, do not exceed `max_files_changed_without_approval` / `max_diff_lines_without_approval`

3. The dev agent runs the project's test command and captures output. Before the run can transition to `review`, test evidence MUST be recorded:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py evidence <run-id> test <test-output-path>
   ```

4. Commit dev changes on the issue branch (mandatory; AC-SI-007). After test evidence is captured and BEFORE advancing to `review`, the dev agent commits all working-tree changes on `laplace/<issue-id>` with a conventional-commit message referencing the issue id (e.g. `feat(<area>): <summary> (ISSUE-<id>)`). The advance to `review` MUST NOT happen with a dirty tree — the review agent diffs the branch HEAD against the base, so an uncommitted change is invisible to review and surfaces as an empty-diff `needs-fix`.

   Fail-safe (matches the existing branch-skip philosophy): if the working directory is not a git repo, the run was started with `BRANCH_SKIPPED`, or `git commit` is denied by policy, the dev agent records the reason in its summary and proceeds. The orchestrator does NOT hand-commit on the agent's behalf.

   Note: the orchestrator's spawn prompt to the dev agent MUST include this commit instruction. The instruction is codified in the dev agent contract (`agents/laplace-dev-agent.md`); the orchestrator copies it into the spawn prompt verbatim.

5. Transition to review:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> in-progress review --summary "<dev complete; tests captured>"
   ```

If the dev agent reports `blocked` (scope ambiguity, missing dependency, test infrastructure unavailable), transition `in-progress -> blocked`, end the run with `outcome=blocked`, and surface to the human. Do NOT silently retry.

### Step 4: Review phase (active in P5)

Invoke the `laplace-review-agent` subagent to review the dev diff against the issue's acceptance criteria, correctness, regressions, and maintainability. The review agent is read-only; it does not transition state itself.

Workflow:

1. Dispatch the review agent (via `Agent(subagent_type: "laplace-review-agent")` or the runtime's equivalent) with:
   - The issue file path (`.harness/issues/<issue-id>.md`)
   - The branch name (`laplace/<issue-id>`)
   - The run id
   - The base branch to diff against (default: `main`)

2. The review agent returns one of `review-passed`, `needs-fix`, or `recommend-security-review`. Handle each:

   - **On `review-passed`** (AC met, no regressions, no sensitive-change recommendation):
     - Advance to terminal review state. AC-LP-008 still requires test evidence, which the dev phase already captured — `runner.py advance` enforces the gate:

       ```
       python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> review review-passed --summary "<review-passed: AC met>"
       ```

     - Then proceed to Step 6 (end the run).

   - **On `needs-fix`** (at least one AC item failed or a regression is present):
     - Advance `review -> needs-fix`. `runner.py` increments the issue's `fix_attempts` counter in `tasks.json` and rejects with exit code 5 if `fix_attempts >= max_fix_attempts` (3):

       ```
       python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> review needs-fix --summary "<one-line required-fix summary>"
       ```

       If exit code is 5, the limit is exceeded — proceed to the "exceeded limit" handling below, do NOT retry.

     - Re-dispatch the dev agent for the fix (the dev agent reads the same issue file; the review agent's required-fix list reaches it via the orchestrator's spawn prompt):

       ```
       python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> needs-fix in-progress --summary "<re-dev: fix #N>"
       ```

     - The dev agent must capture fresh test evidence before reporting `ready-for-review` again. Then advance `in-progress -> review` and re-dispatch the review agent. Loop bounded by `max_fix_attempts` (3).

   - **On `recommend-security-review`** (AC met but diff touches auth, permissions, data access, dependencies, workflows, scripts, MCP config, or external APIs):
     - Advance `review -> security-review` and proceed to Step 5.

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> review security-review --summary "<recommended by review agent: <reason>>"
     ```

3. **Exceeded `max_fix_attempts`**: when `runner.py advance review needs-fix` returns exit code 5, the issue cannot take a 4th fix attempt. Transition to a human-handoff state and end the run:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> review blocked --summary "fix_attempts exceeded max_fix_attempts (3)"
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py end <run-id> --outcome blocked
   ```

   SPEC-002 §Loop Limits names `human-approval-required` as the preferred terminal for this case. In the current state engine `review -> human-approval-required` is not wired as a legal transition (the legal path is `review -> blocked -> human-resolution`), so the orchestrator transitions to `blocked` and surfaces the fix-attempt history to the human. The human can then resolve via the exception flow (`blocked -> human-resolution -> <previous-state>`).

### Step 5: Security phase (active in P5)

Determine whether security review is required. If not already done in Step 4 (review agent recommended it), run the advisory trigger check:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py security-check <issue-id> [--diff <diff-path>]
```

`security-check` reads the issue's `## Risk / Release Impact` and `## Routing Metadata` sections, optionally scans a diff file for sensitive paths and external-API markers, and prints `required: true|false` plus a trigger list. It always exits 0 (advisory; the orchestrator decides the transition).

- **If `required: true` and the issue is not yet in `security-review`**: advance `review -> security-review`:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> review security-review --summary "<trigger list>"
  ```

- **If `required: false` and the review agent did not recommend security**: skip the security agent. If already in `review`, advance `review -> review-passed` (test evidence already captured in dev phase per AC-LP-008). Proceed to Step 6.

Invoke the `laplace-security-agent` subagent. The security agent is read-only; it scans the diff across the dimensions in SPEC-002 §Security and Governance (secrets, auth, permissions, data access, command injection, prompt injection, dependencies, workflows, scripts, MCP, external API).

Dispatch the security agent with:
- The issue file path
- The branch name (`laplace/<issue-id>`)
- The run id
- The review agent's risk notes (passed through from Step 4)

The security agent returns one of `review-passed`, `needs-fix`, or `human-approval-required`. Handle each:

- **On `review-passed`** (security dimension clear):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> security-review review-passed --summary "<security dimension clear>"
  ```

  Note: `review -> review-passed` requires test evidence per AC-LP-008. When arriving from `security-review`, the test evidence captured during the dev phase still satisfies the gate (the run log retains all evidence entries). Proceed to Step 6.

- **On `needs-fix`** (auto-fixable findings): advance `security-review -> needs-fix`. `runner.py` increments `security_fix_attempts` and rejects with exit code 5 if `security_fix_attempts >= max_security_fix_attempts` (2):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> security-review needs-fix --summary "<one-line finding summary>"
  ```

  If exit code is 5, the limit is exceeded — proceed to the "exceeded limit" handling below.

  Then re-dispatch the dev agent for the fix, capture fresh test evidence, and advance `needs-fix -> in-progress -> review -> security-review` to re-enter the security agent. Loop bounded by `max_security_fix_attempts` (2).

- **On `human-approval-required`** (findings that cannot be auto-fixed, or categories that inherently require human sign-off: dependency, workflow, script, MCP, external API, auth/permission/data-access change, or any critical/high finding): end the run and surface to the human. Transition directly to the SPEC-named terminal state:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> security-review human-approval-required --summary "<human-approval-required: <reason>>"
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py end <run-id> --outcome human-approval-required
  ```

  `human-approval-required` is terminal; the human resolves outside the loop (re-intake, re-approve, or close the issue).

- **Exceeded `max_security_fix_attempts`**: when `runner.py advance security-review needs-fix` returns exit code 5, the issue cannot take a 3rd security fix attempt. Transition to `human-approval-required` (SPEC-named terminal for security fix exhaustion) and end:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py advance <issue-id> security-review human-approval-required --summary "security_fix_attempts exceeded max_security_fix_attempts (2)"
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py end <run-id> --outcome human-approval-required
  ```

  Surface the security findings + fix-attempt history to the human.

### Step 6: End the run

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py end <run-id> --outcome <final-state>
```

`<final-state>` is the issue's terminal or paused state: `review-passed`, `blocked`, `human-approval-required`, `release-candidate` (if the release agent has run in a later phase), or `max-attempts-exceeded`.

`runner.py end` finalizes the run log (`ended_at`, `outcome`) and releases the issue lock by delegating to `state.py run-end`.

## Evidence Capture

Evidence is appended to the run log's `evidence` array via:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/runner.py evidence <run-id> <kind> <path-or-text>
```

- `kind` ∈ {`test`, `review`, `security`, `manual`, `command`}. Other values are rejected.
- If `<path-or-text>` is an existing file path, the file is read, redacted, capped at 1000 chars, and stored as `summary` with `source_path` set.
- Otherwise the argument is treated as raw text, redacted, capped at 1000 chars, and stored as `summary` with no `source_path`.
- Raw command output is never stored beyond the redacted 1000-char summary.

Evidence MUST be captured before any pass transition (AC-LP-008).

## Output Format

Per SPEC-002 §Output Format (Result Template). The runner emits a result block for each command; the skill consolidates them into a final loop summary:

```
Laplace result: <outcome>

Issue: <issue-id>
State: <initial> -> <final>

Evidence:
  - run log: .harness/state/runs/<run-id>.json
  - branch: <created|reused|BRANCH_SKIPPED:<reason>>
  - <kind>: <redacted summary>

Artifacts:
  - .harness/state/runs/<run-id>.json
  - .harness/issues/<issue-id>.md (Run History appended)

Risks:
  - <risk or none>

Next:
  <one concrete next action>
```

## Constraints

- MUST NOT proceed past `pm-review` without an explicit `ready` decision from the PM phase. A `blocked` decision stops the loop.
- MUST NOT exceed `max_fix_attempts`, `max_pm_clarification_attempts`, or `max_security_fix_attempts`. These are enforced by state and runner; exceeding transitions to `blocked` or `human-approval-required`.
- MUST NOT create PRs, push, publish releases, or produce any external side effect. PR creation is gated behind `/laplace:create-pr` with a separate human approval.
- MUST capture evidence (test, review, security) before any pass transition. A pass transition without evidence is a violation of AC-LP-008.
- MUST route every subprocess invocation through `policy.check_command` first. `runner.py` already does this for git; agent-driven commands go through the PreToolUse hook.
- MUST redact every persisted summary and evidence entry via `redaction.py`. `runner.py` does this internally; the skill must not bypass it by writing to run logs directly.
- MUST hold the issue lock for the duration of the run. `runner.py start` acquires; `runner.py end` releases. A second `start` on the same issue while the lock is held fails with exit code 3.
- MUST enforce fix-attempt limits via `runner.py` (exit code 5 on limit exceeded); never bypass by transitioning directly to `needs-fix` without the counter. The dev fix loop is bounded by `max_fix_attempts` (3); the security fix loop by `max_security_fix_attempts` (2). On exit 5, transition to `blocked` (the legal human-handoff path in the current state engine) and surface the fix-attempt history to the human.

## Failure Modes

- **Issue not approved**: `runner.py start` exits with code 2 and a state-mismatch message. Recommend `/laplace:approve <issue>` first.
- **Lock held**: `runner.py start` exits with code 3 (`locked: locked by pid=<n>`). Another run is active. Recommend `/laplace:status` to inspect, or wait for the active run to end.
- **Illegal transition**: `runner.py advance` exits with code 2 with a `validate_transition` reason. The state machine is authoritative; the skill must not retry the same transition.
- **Agent failure**: if a phase agent returns an error or fails to produce a decision, transition to `blocked` with a redacted summary and stop. Do not silently retry beyond the configured attempt limits.
- **Git unavailable / not a repo**: `runner.py start` records `BRANCH_SKIPPED:not-a-git-repo` in the run log and proceeds with state transitions only. This is fail-safe behavior, not an error.
- **Run log corrupt or missing on end**: `runner.py end` delegates to `state.py run-end`, which exits with code 1 if the run id is unknown. The lock may remain held; recommend `/laplace:cancel <issue>` or manual `state.py unlock <issue>`.
