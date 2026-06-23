# Bug: parallel scheduler cap (max_parallel) unenforced across waves

## Status
Confirmed bug — discovered during dogfood of `/laplace:run-parallel` on ISSUE-0006..0009.

## Reproduction

`max_parallel=2`, 4 independent approved issues (0006..0009). Two sequential `parallel_queue.py start` invocations:

- Wave 1: dispatched ISSUE-0006 + ISSUE-0007 (correct — 2 of 2 slots).
- Wave 2 (immediate re-invoke): dispatched ISSUE-0008 + ISSUE-0009 (WRONG — should be 0 slots; 0006/0007 in-flight).

Net: 4 issues running concurrently with `max_parallel=2`. Cap (AC-PQ-004/012) violated.

## Root cause

`scripts/parallel_queue.py::_compute_sets` computes `in_flight` by iterating the **`approved` queue**:

```python
for iid in approved:               # approved = state._load_queue()["approved"]
    st = tasks.get(iid, {}).get("status")
    if st in IN_FLIGHT_STATUSES:
        in_flight.append(iid)
```

But a dispatched issue transitions `approved → pm-review`, which **removes it from the approved queue** (per `state._set_issue_state` queue-rebuild). So on the next wave, the approved list no longer contains 0006/0007; the `for iid in approved` loop never sees them; `in_flight` is empty; `slots = max_parallel - 0 = 2`; the next two dispatch.

The selftest (`test_ac_pq_012`) passes because it drives within a single process where it manually pre-seeds statuses without going through the full `approved → pm-review` queue transition — the cross-process re-invocation case was never exercised.

## Fix direction

`in_flight` must be computed from **tasks.json statuses** (all issues whose status ∈ IN_FLIGHT_STATUSES), NOT from membership in the approved queue. Concretely: iterate `tasks` (not `approved`), collect every issue whose status is a non-terminal running state AND whose run log is not finalized. This survives the `approved → pm-review` queue exit because tasks.json still records the issue with its current status.

Alternative: count live worktrees (`state._find_active_parallel_run` → child runs → non-finalized) — directly measures what the cap is supposed to limit.

## Acceptance criteria

- AC-CAPFIX-001: with `max_parallel=2` and 4 independent approved issues, wave 1 dispatches exactly 2; the 2 transition to pm-review; wave 2 (re-invoke) dispatches 0 (slots = 2 - 2 in-flight = 0); outcome `wave-dispatched:waiting`.
- AC-CAPFIX-002: after one of the in-flight issues reaches terminal, the next wave dispatches exactly 1 (slots = 2 - 1).
- AC-CAPFIX-003: a NEW cross-process integration test (two subprocess invocations of `parallel_queue.py start`) reproduces the bug pre-fix and passes post-fix. The existing in-process selftest is insufficient.
- AC-CAPFIX-004: characterization — single-wave behavior (within one process) unchanged.

## Risk / Release Impact

- Risk Level: high (cap is a safety boundary for resource pressure; unenforced cap can freeze the host)
- Release Type: patch (0.5.1)
- Security Sensitivity: medium (the load-rate-limiter task in ISSUE-0009 builds on this cap; a broken cap undermines that too)
