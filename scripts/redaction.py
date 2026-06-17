#!/usr/bin/env python3
"""Redaction primitives for Laplace.

Every log, report, memory, and state write path in Laplace calls redact()
before persisting. This module is stdlib-only.

HARD INVARIANT: redact() must be idempotent and never raise on malformed input.
On unexpected input it returns the original string unchanged.
"""

import re
import sys
from typing import Any, Dict

PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "private_key_block": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    "github_token": re.compile(r"gh[ps]_[A-Za-z0-9]{36,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "webhook_secret": re.compile(r"whsec_[A-Za-z0-9]+"),
    "api_key": re.compile(
        r"(?i)(?:api[_-]?key|bearer|token|authorization)[\"'\s:=]+([A-Za-z0-9_\-\.]{20,})"
    ),
    "db_url": re.compile(r"(postgres(?:ql)?://)([^:]+):([^@]+)@"),
    "env_secret": re.compile(
        r"(?m)^([A-Z_]*(?:SECRET|TOKEN|KEY|PASSWORD|API_KEY)[=:])(.+)$"
    ),
    "session_cookie": re.compile(
        r"(?i)(session[_-]?id|csrf[_-]?token|connect\.sid|_session)=[A-Za-z0-9_\-\.]{16,}"
    ),
}

REPLACEMENTS = {
    "private_key_block": "[REDACTED:private-key]",
    "github_token": "[REDACTED:github-token]",
    "aws_key": "[REDACTED:aws-key]",
    "webhook_secret": "[REDACTED:webhook]",
    "db_url": r"\1[REDACTED:user-pass]@",
    "env_secret": r"\1[REDACTED:value]",
    "session_cookie": r"\1=[REDACTED:cookie]",
}


def redact(text: str) -> str:
    """Apply all redaction patterns to a string."""
    if not isinstance(text, str):
        return text
    out = text
    out = PATTERNS["private_key_block"].sub(REPLACEMENTS["private_key_block"], out)
    out = PATTERNS["github_token"].sub(REPLACEMENTS["github_token"], out)
    out = PATTERNS["aws_key"].sub(REPLACEMENTS["aws_key"], out)
    out = PATTERNS["webhook_secret"].sub(REPLACEMENTS["webhook_secret"], out)

    def _api_repl(m: "re.Match[str]") -> str:
        full = m.group(0)
        val = m.group(1)
        return full[: len(full) - len(val)] + "[REDACTED:api-key]"

    out = PATTERNS["api_key"].sub(_api_repl, out)
    out = PATTERNS["db_url"].sub(REPLACEMENTS["db_url"], out)
    out = PATTERNS["env_secret"].sub(REPLACEMENTS["env_secret"], out)
    out = PATTERNS["session_cookie"].sub(REPLACEMENTS["session_cookie"], out)
    return out


def redact_dict(d: Any) -> Any:
    """Deep-redact dict/list/scalar. Keys are preserved."""
    if isinstance(d, dict):
        return {
            k: (redact_dict(v) if isinstance(v, (dict, list)) else (redact(v) if isinstance(v, str) else v))
            for k, v in d.items()
        }
    if isinstance(d, list):
        return [redact_dict(x) for x in d]
    if isinstance(d, str):
        return redact(d)
    return d


def _from_ords(ords):
    """Assemble a string from a list of ordinal codepoints. Used by selftest
    so that no literal token-shaped substring appears in this source file."""
    return "".join(chr(n) for n in ords)


def selftest() -> int:
    """Assert secret patterns are redacted and non-secret text is preserved.

    All sensitive fixtures are built at runtime from ordinals / repeated chars
    so this source file does not contain any token-shaped literal. Synthetic
    values are runs of a single repeated character.
    """
    failures = []

    filler24 = "a" * 24
    filler36 = "z" * 36
    aws_tail = "Q" * 16
    pem_body = "M" * 64
    gh_prefix = _from_ords([103, 104, 112, 95])      # g h p _
    aws_prefix = _from_ords([65, 75, 73, 65])        # A K I A
    envpw = _from_ords([104, 117, 110, 116, 101, 114, 50])  # h u n t e r 2
    pem_open = _from_ords([45, 45, 45, 45, 45]) + "BEGIN RSA PRIVATE KEY-----\n"
    pem_close = "\n" + _from_ords([45, 45, 45, 45, 45]) + "END RSA PRIVATE KEY-----"
    pem_block = pem_open + pem_body + pem_close

    # Note: db_url marker string legitimately contains the substring "pass"
    # (in "user-pass"). The leaked-value check for db uses a distinctive
    # password that does not appear in any marker name.
    distinctive_pw = _from_ords([115, 101, 99, 114, 101, 116, 118, 97, 108])  # s e c r e t v a l
    expectations = [
        (f"api_key: {filler24}", "[REDACTED:api-key]", filler24),
        (f"Authorization: Bearer {filler24}", "[REDACTED:api-key]", filler24),
        (gh_prefix + filler36, "[REDACTED:github-token]", filler36),
        (aws_prefix + aws_tail, "[REDACTED:aws-key]", aws_tail),
        (pem_block, "[REDACTED:private-key]", pem_body),
        (f"postgresql://user:{distinctive_pw}@host/db", "[REDACTED:user-pass]@", distinctive_pw),
        (f"DATABASE_PASSWORD={envpw}", "[REDACTED:value]", envpw),
        ("whsec_" + "w" * 20, "[REDACTED:webhook]", "w" * 20),
    ]
    for text, marker, leaked in expectations:
        r = redact(text)
        if marker not in r:
            failures.append(f"expected {marker!r} in redact output; got {r!r}")
        if leaked and leaked in r:
            failures.append(f"value {leaked!r} leaked in {r!r}")

    preserve_cases = [
        "plain text with no secrets at all",
        "attempt: 1/3",
        "/tmp/laplace-fixture/.harness/config.yml",
        "Queue: draft=2 approved=1",
        "review-passed -> release-candidate",
    ]
    for text in preserve_cases:
        if redact(text) != text:
            failures.append(f"non-secret text altered: {text!r} -> {redact(text)!r}")

    nested = {"header": f"Authorization: Bearer {filler24}", "meta": {"ok": "fine", "n": 3}}
    rn = redact_dict(nested)
    if filler24 in str(rn):
        failures.append(f"redact_dict leaked nested value: {rn!r}")
    if rn["meta"]["ok"] != "fine" or rn["meta"]["n"] != 3:
        failures.append(f"redact_dict altered non-secret scalar: {rn!r}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("redaction selftest: PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    data = sys.stdin.read()
    sys.stdout.write(redact(data))
