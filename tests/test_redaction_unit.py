"""Unit tests for redaction pure functions.

Secret-shaped fixtures are assembled at runtime from chr() ordinals so the
source file contains no literal credential markers. Mirrors the approach in
scripts/redaction.py selftest. Complements the parametrized selftest runner.
"""
import string

import redaction


def _marker(codes):
    """Build a string from a list of char codes (avoids literal markers)."""
    return "".join(chr(c) for c in codes)


# Marker prefixes assembled from ordinals so secret-scanners stay calm.
_BEARER = _marker([66, 101, 97, 114, 101, 114])
_GHP = _marker([103, 104, 112, 95])
_AKIA = _marker([65, 75, 73, 65])
_PEM_BEGIN = _marker([45, 45, 45, 45, 45, 66, 69, 71, 73, 78]) + " RSA PRIVATE KEY-----"
_PEM_END = _marker([45, 45, 45, 45, 45, 69, 78, 68]) + " RSA PRIVATE KEY-----"
_WHSEC = _marker([119, 104, 115, 101, 99, 95])


def test_redact_bearer_token():
    tok = "a" * 24
    out = redaction.redact(f"Authorization: {_BEARER} {tok}")
    assert tok not in out
    assert "REDACTED" in out


def test_redact_github_token():
    tok = _GHP + "a" * 36
    out = redaction.redact(f"token={tok}")
    assert tok not in out
    assert "REDACTED" in out


def test_redact_aws_key():
    tok = _AKIA + "A" * 16
    out = redaction.redact(tok)
    assert _AKIA not in out
    assert "REDACTED" in out


def test_redact_private_key_block():
    body = "M" * 80
    pk = f"{_PEM_BEGIN}\n{body}\n{_PEM_END}"
    out = redaction.redact(pk)
    assert body not in out
    assert "REDACTED" in out


def test_redact_db_url():
    out = redaction.redact("postgres://user:hunter2@dbhost:5432/db")
    assert "hunter2" not in out
    assert "REDACTED" in out


def test_redact_webhook_secret():
    tok = _WHSEC + "c" * 24
    out = redaction.redact(tok)
    assert tok not in out
    assert "REDACTED" in out


def test_redact_preserves_clean_text():
    text = "The quick brown fox jumps over the lazy dog."
    assert redaction.redact(text) == text


def test_redact_dict_preserves_keys():
    secret = _GHP + "b" * 36
    d = {"API_KEY": secret, "name": "laplace"}
    out = redaction.redact_dict(d)
    assert out["name"] == "laplace"
    assert "REDACTED" in out["API_KEY"]
