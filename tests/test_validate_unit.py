"""Unit tests for validate.parse_frontmatter and check helpers."""
import os
import tempfile

import validate


def test_parse_frontmatter_extracts_fields() -> None:
    text = "---\nname: foo\ndescription: bar baz\n---\n\n# Body\n"
    fm = validate.parse_frontmatter(text)
    assert fm is not None
    assert fm["name"] == "foo"
    assert fm["description"] == "bar baz"


def test_parse_frontmatter_returns_none_without_markers() -> None:
    assert validate.parse_frontmatter("# no frontmatter\n") is None


def test_parse_frontmatter_strips_quotes() -> None:
    text = '---\nname: "quoted"\n---\n'
    fm = validate.parse_frontmatter(text)
    assert fm["name"] == "quoted"


def test_run_validation_passes_on_real_plugin() -> None:
    """The shipped plugin must validate cleanly (warnings allowed)."""
    rep = validate.run_validation(validate.DEFAULT_ROOT)
    assert rep.ok_all, f"validation failures: {rep.fails}"


def test_run_validation_detects_broken_fixture() -> None:
    with tempfile.TemporaryDirectory(prefix="lp-validate-test-") as tmp:
        os.makedirs(os.path.join(tmp, ".claude-plugin"))
        with open(os.path.join(tmp, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": ""}')  # empty name, missing version/description
        rep = validate.run_validation(tmp)
        assert not rep.ok_all, "broken fixture should fail validation"
