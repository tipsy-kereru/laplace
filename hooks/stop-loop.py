#!/usr/bin/env python3
"""Laplace Stop-hook loop controller.

Implements the ralph-wiggum fail-safe pattern (knowledge/03) adapted for
Laplace's issue-aware loop. State lives in .harness/state/active-loop.local.json
(which is already git-ignored via state.py's GITIGNORE_TEMPLATE that ignores
all of state/).

HARD INVARIANT (ralph fail-safe): every error path MUST exit 0 (allow stop).
The ONLY path that emits {"decision": "block"} is the narrow case:
    active loop AND incomplete AND iteration < max AND no completion signal.
A missing/corrupt state file, missing transcript, max iterations reached,
or any parse/IO error ALL exit 0. Never infinite-loop. Never lock the user in.

stdout is JSON only. Logs to stderr. Fail-open on every error. stdlib-only.
"""

import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

try:
    from state import MAX_STOP_HOOK_ITERATIONS  # type: ignore
except Exception:  # pragma: no cover - defensive default
    MAX_STOP_HOOK_ITERATIONS = 12

DEFAULT_COMPLETION_SIGNAL = "LAPLACE-P0P6-COMPLETE"


def _state_path() -> str:
    return os.path.join(os.getcwd(), ".harness", "state", "active-loop.local.json")


def _atomic_write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _delete_state(state_path: str) -> None:
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError as exc:  # pragma: no cover - best effort
        sys.stderr.write(f"stop-loop: failed to delete state: {exc}\n")


def _quarantine(state_path: str, reason: str) -> None:
    """Move a corrupt state file aside for debugging, then continue (exit 0)."""
    try:
        ts = int(time.time())
        dest = f"{state_path}.corrupt.{ts}"
        if os.path.exists(state_path):
            os.replace(state_path, dest)
        sys.stderr.write(f"stop-loop: quarantined state ({reason}) -> {dest}\n")
    except OSError as exc:  # pragma: no cover
        sys.stderr.write(f"stop-loop: quarantine failed: {exc}\n")


def _read_last_assistant_text(transcript_path: str) -> Optional[str]:
    """Best-effort JSONL parse of the transcript. Return the last assistant
    text message, or None on any error."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    last_text = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Claude Code transcript entries are {type, message: {role, content}}.
                msg = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    if content:
                        last_text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text")
                            if isinstance(t, str) and t:
                                last_text = t
    except OSError:
        return None
    return last_text


def _next_instruction(state: Dict[str, Any]) -> str:
    """Compose the bounded next instruction for the loop. Kept under 500 chars.

    Laplace is issue-aware: the next instruction is NOT the full original
    prompt. For MVP we emit a bounded instruction pointing at /laplace:run
    continuation. Future phases can read a pending next-action from the run log.
    """
    issue_id = state.get("issue_id") or "<unknown>"
    run_id = state.get("run_id") or "<unknown>"
    iteration = int(state.get("iteration", 1))
    max_iter = int(state.get("max_iterations", MAX_STOP_HOOK_ITERATIONS))
    return (
        f"Laplace loop continues: issue={issue_id} run={run_id} "
        f"iteration={iteration}/{max_iter}. Continue the current phase per "
        f"/laplace:run. Emit {state.get('completion_signal', DEFAULT_COMPLETION_SIGNAL)} "
        f"when all acceptance criteria are met and evidence is recorded."
    )[:500]


def _emit_block(state: Dict[str, Any]) -> None:
    iteration = int(state.get("iteration", 1))
    max_iter = int(state.get("max_iterations", MAX_STOP_HOOK_ITERATIONS))
    issue_id = state.get("issue_id") or "<unknown>"
    reason = _next_instruction(state)
    sys_msg = f"Laplace loop: iteration {iteration}/{max_iter} | issue={issue_id}"
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": reason,
        "systemMessage": sys_msg[:300],
    }))
    sys.exit(0)


def _emit_allow() -> None:
    # Empty stdout or {"decision":"allow"} both mean "let it stop".
    sys.exit(0)


def run() -> int:
    state_path = _state_path()

    # 1. Read stdin -> transcript_path.
    try:
        raw = sys.stdin.read()
    except Exception:  # pragma: no cover
        _emit_allow()

    transcript_path = None
    if raw.strip():
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                transcript_path = payload.get("transcript_path")
        except json.JSONDecodeError:
            # Malformed stdin: fail open.
            sys.stderr.write("stop-loop: malformed JSON stdin, allowing stop\n")
            _emit_allow()

    # If there is no state file, there is no active loop. Allow stop.
    if not os.path.isfile(state_path):
        _emit_allow()

    # 2. Load state. Corrupt -> quarantine + exit 0.
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _quarantine(state_path, f"load error: {exc}")
        _emit_allow()

    if not isinstance(state, dict):
        _quarantine(state_path, "state is not a dict")
        _emit_allow()

    # 3. Validate numeric fields. Non-int -> quarantine + exit 0.
    iteration = state.get("iteration")
    max_iter = state.get("max_iterations")
    if not isinstance(iteration, int) or not isinstance(max_iter, int):
        _quarantine(state_path, f"non-int iteration/max: {iteration!r}/{max_iter!r}")
        _emit_allow()
    if iteration < 0 or max_iter <= 0:
        _quarantine(state_path, f"out-of-range iter/max: {iteration}/{max_iter}")
        _emit_allow()

    # 4. iteration >= max_iterations -> delete state, allow stop.
    if iteration >= max_iter:
        sys.stderr.write(
            f"stop-loop: max_stop_hook_iterations reached ({iteration}/{max_iter})\n"
        )
        _delete_state(state_path)
        _emit_allow()

    # 5. Read transcript. Missing/no assistant text -> delete state, allow stop.
    last_text = _read_last_assistant_text(transcript_path) if transcript_path else None
    if last_text is None:
        # Either no transcript or no assistant text. Treat as loop-not-active.
        _delete_state(state_path)
        _emit_allow()

    # 6. Completion signal check. Literal substring match.
    # MVP design note: ralph uses <promise>...</promise> tag extraction with
    # exact-match comparison. For Laplace MVP we use a simpler literal substring
    # match on a fixed completion signal. This is sufficient because the
    # completion signal is a project-specific opaque literal (not user-derived),
    # and the model is instructed (via skill) to emit the exact string only on
    # genuine completion. The fail-safe on missing/corrupt state still applies.
    completion_signal = state.get("completion_signal") or DEFAULT_COMPLETION_SIGNAL
    if isinstance(completion_signal, str) and completion_signal and completion_signal in last_text:
        sys.stderr.write("stop-loop: completion signal detected, loop complete\n")
        _delete_state(state_path)
        _emit_allow()

    # 7. Otherwise: increment, atomic-rewrite, emit block.
    state["iteration"] = iteration + 1
    state["last_iterated_at"] = time.time()
    try:
        _atomic_write_json(state_path, state)
    except OSError as exc:  # pragma: no cover
        sys.stderr.write(f"stop-loop: failed to rewrite state, allowing stop: {exc}\n")
        _emit_allow()

    _emit_block(state)
    return 0  # unreachable


# --- selftest ----------------------------------------------------------------

def selftest() -> int:
    import shutil
    import subprocess
    import tempfile

    failures = []

    def _run(payload: str, cwd: str) -> Tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            input=payload, capture_output=True, text=True, timeout=15, cwd=cwd,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _write_state(cwd: str, state: Dict[str, Any]) -> str:
        state_path = os.path.join(cwd, ".harness", "state", "active-loop.local.json")
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        _atomic_write_json(state_path, state)
        return state_path

    def _write_transcript(cwd: str, last_text: str) -> str:
        path = os.path.join(cwd, ".transcript.jsonl")
        entries = [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": last_text}]}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    tmp = tempfile.mkdtemp(prefix="laplace-stoploop-")
    try:
        payload_empty = json.dumps({})

        # 1. Missing state file -> exit 0, no block.
        rc, out, err = _run(payload_empty, tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"missing-state: rc={rc} out={out!r}")

        # 2. Corrupt state JSON -> quarantined, exit 0.
        state_path = _write_state(tmp, {"broken": "json"})
        # Overwrite with corrupt JSON.
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("{not valid json,,,")
        rc, out, err = _run(payload_empty, tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"corrupt-state: rc={rc} out={out!r}")
        # Quarantine file should exist.
        quarantined = [f for f in os.listdir(os.path.dirname(state_path))
                       if f.startswith("active-loop.local.json.corrupt.")]
        if not quarantined:
            failures.append("corrupt-state: no quarantine file created")

        # 3. iteration >= max_iterations -> state deleted, exit 0.
        state_path = _write_state(tmp, {
            "active": True, "iteration": 12, "max_iterations": 12,
            "completion_signal": DEFAULT_COMPLETION_SIGNAL,
            "run_id": "r1", "issue_id": "ISSUE-1",
            "started_at": "2026-06-15T00:00:00Z",
        })
        rc, out, err = _run(payload_empty, tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"max-iter: rc={rc} out={out!r}")
        if os.path.exists(state_path):
            failures.append("max-iter: state file should be deleted")

        # 4. Missing transcript -> state deleted, exit 0.
        state_path = _write_state(tmp, {
            "active": True, "iteration": 1, "max_iterations": 12,
            "completion_signal": DEFAULT_COMPLETION_SIGNAL,
            "run_id": "r2", "issue_id": "ISSUE-2",
            "started_at": "2026-06-15T00:00:00Z",
        })
        rc, out, err = _run(payload_empty, tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"missing-transcript: rc={rc} out={out!r}")
        if os.path.exists(state_path):
            failures.append("missing-transcript: state file should be deleted")

        # 5. Completion signal present -> state deleted, exit 0.
        state_path = _write_state(tmp, {
            "active": True, "iteration": 1, "max_iterations": 12,
            "completion_signal": DEFAULT_COMPLETION_SIGNAL,
            "run_id": "r3", "issue_id": "ISSUE-3",
            "started_at": "2026-06-15T00:00:00Z",
        })
        transcript = _write_transcript(tmp, f"All done. {DEFAULT_COMPLETION_SIGNAL}")
        rc, out, err = _run(json.dumps({"transcript_path": transcript}), tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"completion: rc={rc} out={out!r}")
        if os.path.exists(state_path):
            failures.append("completion: state file should be deleted")

        # 6. Active loop, under limit, no completion -> iteration incremented, block.
        state_path = _write_state(tmp, {
            "active": True, "iteration": 3, "max_iterations": 12,
            "completion_signal": DEFAULT_COMPLETION_SIGNAL,
            "run_id": "r4", "issue_id": "ISSUE-4",
            "started_at": "2026-06-15T00:00:00Z",
        })
        transcript = _write_transcript(tmp, "Still working on the implementation.")
        rc, out, err = _run(json.dumps({"transcript_path": transcript}), tmp)
        if rc != 0:
            failures.append(f"active-continue: rc={rc} (expected 0)")
        try:
            decision = json.loads(out)
        except json.JSONDecodeError:
            failures.append(f"active-continue: non-JSON stdout: {out!r}")
        else:
            if decision.get("decision") != "block":
                failures.append(f"active-continue: decision={decision.get('decision')} expected block")
            if "iteration" not in decision.get("systemMessage", ""):
                failures.append(f"active-continue: systemMessage missing iteration: {decision}")
        # State file should still exist with iteration=4.
        with open(state_path, "r", encoding="utf-8") as f:
            updated = json.load(f)
        if updated.get("iteration") != 4:
            failures.append(f"active-continue: iteration not incremented: {updated.get('iteration')}")

        # 7. Malformed stdin -> exit 0, no block.
        rc, out, err = _run("not json {{{", tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"malformed-stdin: rc={rc} out={out!r}")

        # 8. Non-int iteration -> quarantine, exit 0.
        state_path = _write_state(tmp, {
            "active": True, "iteration": "five", "max_iterations": 12,
            "completion_signal": DEFAULT_COMPLETION_SIGNAL,
            "run_id": "r5", "issue_id": "ISSUE-5",
            "started_at": "2026-06-15T00:00:00Z",
        })
        rc, out, err = _run(payload_empty, tmp)
        if rc != 0 or '"decision": "block"' in out:
            failures.append(f"non-int-iter: rc={rc} out={out!r}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("stop-loop selftest: PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    sys.exit(run())
