#!/usr/bin/env python3
"""Laplace PostToolUse / PostToolUseFailure hook.

Reads JSON stdin:
  {"tool_name": "...", "tool_input": {...}, "tool_response": {...}}
On PostToolUseFailure the payload may include an `error` field or
`tool_response.success == false`.

PostToolUse does NOT block (the action already happened). It records sanitized
observations to .harness/logs/harness.log and warns loudly on bypass attempts.
HARD INVARIANT: never persist raw command output. All log writes pass through
redaction. stdout is JSON only; logs to stderr. Fail-open on every error.
stdlib-only.
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
    import policy  # type: ignore
    from redaction import redact, PATTERNS  # type: ignore
except Exception:  # pragma: no cover - fail open if deps missing
    policy = None  # type: ignore
    redact = None  # type: ignore
    PATTERNS = {}  # type: ignore


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_FIELDS = ("file_path", "path", "notebook_path", "filePath")


def _harness_log_path() -> str:
    """Return the harness log path. Resolved relative to CWD (project root)."""
    return os.path.join(os.getcwd(), ".harness", "logs", "harness.log")


def _append_log(line: str) -> None:
    """Append a redacted line to harness.log. Atomic-ish append (line buffered).
    All content is passed through redaction first."""
    if redact is not None:
        line = redact(line)
    try:
        path = _harness_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as exc:  # pragma: no cover - logging must never crash the hook
        sys.stderr.write(f"posttooluse: failed to append log: {exc}\n")


def _detect_secret_shaped(text: str) -> Optional[str]:
    """If any redaction pattern matches, return the pattern name (for the
    log entry). We detect presence only — never persist the raw match."""
    if not text or not isinstance(text, str):
        return None
    for name, pat in PATTERNS.items():
        if pat.search(text):
            return name
    return None


def _extract_command(tool_input: Dict[str, Any]) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _extract_paths(tool_input: Dict[str, Any]) -> list:
    out = []
    if not isinstance(tool_input, dict):
        return out
    for f in PATH_FIELDS:
        v = tool_input.get(f)
        if isinstance(v, str) and v:
            out.append(v)
    v = tool_input.get("paths")
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str):
                out.append(item)
    return out


def _tool_output_text(tool_response: Any) -> str:
    """Concatenate stdout/stderr/text fields into a single string for scanning."""
    if not isinstance(tool_response, dict):
        return ""
    parts = []
    for key in ("stdout", "stderr", "text", "output", "content", "error"):
        v = tool_response.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # assistant content blocks: {"type":"text","text":"..."}
                    t = item.get("text")
                    if isinstance(t, str):
                        parts.append(t)
    return "\n".join(parts)


def handle(tool_name: str, tool_input: Dict[str, Any],
           tool_response: Any, is_failure: bool) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    # 1. Bash output secret detection.
    if tool_name == "Bash":
        text = _tool_output_text(tool_response)
        if text:
            secret_kind = _detect_secret_shaped(text)
            if secret_kind:
                # NEVER include the raw match. Log only the kind + a redacted cmd.
                cmd = _extract_command(tool_input)[:120]
                entry = (
                    f"[{ts}] WARN secret-shaped-{secret_kind} in Bash output "
                    f"(cmd-redacted={redact(cmd) if redact else cmd})"
                )
                _append_log(entry)
                sys.stderr.write(
                    "posttooluse: WARNING Bash output matched secret pattern "
                    f"{secret_kind}; redacted entry logged (raw output NOT stored)\n"
                )

    # 2. Write/Edit to a policy-denied path — bypass attempt.
    if tool_name in WRITE_TOOLS:
        for path in _extract_paths(tool_input):
            if policy is None:
                break
            ok, reason = policy.check_path(path, write=True)
            if not ok:
                entry = (
                    f"[{ts}] POLICY-VIOLATION {tool_name} to denied path "
                    f"(path-redacted={redact(path) if redact else path}) reason={reason}"
                )
                _append_log(entry)
                sys.stderr.write(
                    f"posttooluse: POLICY VIOLATION {tool_name} reached denied "
                    f"path — PreToolUse should have blocked it. Logged.\n"
                )

    # 3. Failure evidence (PostToolUseFailure).
    if is_failure:
        err_text = ""
        if isinstance(tool_response, dict):
            err_text = str(tool_response.get("error") or tool_response.get("stderr") or "")
        elif isinstance(tool_response, str):
            err_text = tool_response
        # Redact before any persistence.
        entry = (
            f"[{ts}] FAILURE {tool_name} "
            f"(err-redacted={redact(err_text[:200]) if redact else err_text[:200]})"
        )
        _append_log(entry)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return selftest()

    try:
        raw = sys.stdin.read()
    except Exception:  # pragma: no cover
        sys.stdout.write("{}")
        return 0

    # Malformed JSON: fail open.
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.stderr.write("posttooluse: malformed JSON stdin, failing open\n")
        sys.stdout.write("{}")
        return 0

    if not isinstance(payload, dict):
        sys.stdout.write("{}")
        return 0

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_response = payload.get("tool_response")
    # Detect failure: explicit top-level error, tool_response.error, or
    # tool_response.success === false.
    is_failure = False
    if payload.get("error"):
        is_failure = True
    if isinstance(tool_response, dict):
        if tool_response.get("error") or tool_response.get("success") is False:
            is_failure = True

    try:
        handle(tool_name, tool_input, tool_response, is_failure)
    except Exception as exc:  # pragma: no cover - never block on logging errors
        sys.stderr.write(f"posttooluse: handle error (failing open): {exc}\n")

    # PostToolUse is observational only — must NOT emit a decision.
    # Empty JSON object is the safe no-op stdout.
    sys.stdout.write("{}")
    return 0


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

    tmp = tempfile.mkdtemp(prefix="laplace-posttooluse-")
    try:
        log_dir = os.path.join(tmp, ".harness", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # 1. Bash output with fake token -> redacted warning, no raw token.
        filler = "a" * 24
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "env"},
            "tool_response": {"stdout": f"Authorization: Bearer {filler}"},
        })
        rc, out, err = _run(payload, tmp)
        if rc != 0:
            failures.append(f"Bash-secret: exit={rc}")
        log_path = os.path.join(log_dir, "harness.log")
        with open(log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        if filler in log_content:
            failures.append("Bash-secret: raw token leaked into harness.log")
        if "secret-shaped" not in log_content:
            failures.append("Bash-secret: no secret-shaped warning entry logged")

        # 2. Write to denied path -> policy-violation entry.
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": ".env", "content": "X=1"},
            "tool_response": {"ok": True},
        })
        _run(payload, tmp)
        with open(log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        if "POLICY-VIOLATION" not in log_content:
            failures.append("Write-denied: no POLICY-VIOLATION entry logged")

        # 3. Failure path records sanitized failure entry.
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "make test"},
            "tool_response": {"success": False, "error": "exit 1"},
        })
        _run(payload, tmp)
        with open(log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        if "FAILURE" not in log_content:
            failures.append("Failure: no FAILURE entry logged")

        # 4. Malformed stdin fails open.
        rc, out, err = _run("not json {{{", tmp)
        if rc != 0:
            failures.append(f"malformed: exit={rc} (expected 0)")

        # 5. Clean Bash output (no secrets) does not warn.
        before_lines = sum(1 for _ in open(log_path, encoding="utf-8"))
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "README.md src tests"},
        })
        _run(payload, tmp)
        after_lines = sum(1 for _ in open(log_path, encoding="utf-8"))
        if after_lines != before_lines:
            failures.append("Clean output: should not log a warning entry")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("posttooluse selftest: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
