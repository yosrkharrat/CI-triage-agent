"""The diff guard: every way of turning a test green without fixing anything.

Each rule gets a small diff that breaks it, and the honest fixes next to them
must pass, or the guard would just refuse everything.
"""

from __future__ import annotations

import pytest

from ci_triage.guard import ChangedFile, forbidden, guard, is_test_file, pin_only


def _patch(removed: list[str] = (), added: list[str] = ()) -> str:  # type: ignore[assignment]
    body = [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "@@ -1,3 +1,3 @@\n" + "\n".join(body)


def _f(path: str, removed=(), added=(), status="modified", **kw) -> ChangedFile:
    return ChangedFile(path, status, _patch(list(removed), list(added)), **kw)


# -- what passes -----------------------------------------------------------


def test_an_honest_source_fix_passes():
    r = guard([_f("src/app/core.py", ["    return a - b"], ["    return a + b"])])
    assert r.ok and not r.flags
    assert r.files == 1


def test_adding_a_test_passes():
    r = guard([
        _f("src/app/core.py", ["x = 1"], ["x = 2"]),
        _f("tests/test_core.py", [], ["def test_new_case():", "    assert add(1, 2) == 3"]),
    ])
    assert r.ok, r.violations


def test_moving_a_test_within_its_file_passes():
    r = guard([_f("tests/test_core.py", ["def test_add():"], ["def test_add():"])])
    assert r.ok, r.violations


def test_a_pure_rename_of_source_has_no_patch_and_passes():
    r = guard([ChangedFile("src/new.py", "renamed", None, previous_filename="src/old.py", changes=0)])
    assert r.ok, r.violations


def test_a_dependency_bump_in_pyproject_passes():
    r = guard([_f("pyproject.toml", ['    "httpx>=0.27",'], ['    "httpx>=0.28",'])])
    assert r.ok, r.violations


# -- deleted and uncollected tests -----------------------------------------


def test_a_deleted_test_file_fails():
    r = guard([ChangedFile("tests/test_core.py", "removed", _patch(["def test_add():"]))])
    assert not r.ok
    assert "test file deleted" in r.violations[0]


def test_a_deleted_test_function_fails():
    r = guard([_f("tests/test_core.py", ["def test_add():", "    assert add(1, 2) == 3"], [])])
    assert "test_add" in r.violations[0]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("def test_add():", "def _test_add():"),
        ("def test_add():", "def check_add():"),
        ("func TestAdd(t *testing.T) {", "func testAdd(t *testing.T) {"),
        ("  it('adds two numbers', () => {", "  xit('adds two numbers', () => {"),
        ("class TestCore:", "class Core:"),
    ],
)
def test_a_test_renamed_out_of_collection_fails(before, after):
    path = "pkg/core_test.go" if "func" in before else (
        "src/core.test.ts" if "it(" in before else "tests/test_core.py"
    )
    r = guard([_f(path, [before], [after])])
    assert not r.ok, f"{before!r} -> {after!r} slipped through"


def test_a_test_file_renamed_out_of_collection_fails():
    r = guard([ChangedFile("tests/core_checks.txt", "renamed", None,
                           previous_filename="tests/test_core.py", changes=0)])
    assert not r.ok


def test_a_test_file_renamed_away_from_its_pattern_fails():
    r = guard([ChangedFile("src/core_check.py", "renamed", None,
                           previous_filename="src/test_core.py", changes=0)])
    assert "renamed out of collection" in r.violations[0]


# -- skips -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("tests/test_core.py", "@pytest.mark.skip(reason='flaky')"),
        ("tests/test_core.py", "@pytest.mark.xfail"),
        ("tests/test_core.py", "    pytest.skip('later')"),
        ("tests/test_core.py", "@unittest.skip('later')"),
        ("tests/test_core.py", "        self.skipTest('later')"),
        ("tests/test_core.py", "x = compute()  # type: ignore"),
        ("tests/test_core.py", "import os  # noqa: F401"),
        ("src/core.test.ts", "  it.skip('adds', () => {"),
        ("src/core.test.ts", "  test.only('adds', () => {"),
        ("src/core.test.ts", "  // @ts-ignore"),
        ("pkg/core_test.go", '\tt.Skip("later")'),
        ("tests/core.rs", "#[ignore]"),
        ("src/test/java/CoreTest.java", "    @Disabled"),
    ],
)
def test_an_added_skip_in_any_test_file_fails(path, line):
    r = guard([_f(path, [], [line])])
    assert not r.ok, f"{line!r} in {path} slipped through"


def test_a_skip_in_source_code_is_not_a_test_skip():
    r = guard([_f("src/app/runner.py", [], ["    if skip: return"])])
    assert r.ok


# -- configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", ".github/dependabot.yml"],
)
def test_anything_under_dot_github_fails(path):
    r = guard([_f(path, ["runs-on: ubuntu-22.04"], ["runs-on: ubuntu-24.04"])])
    assert ".github/" in r.violations[0]


@pytest.mark.parametrize(
    "path",
    ["pytest.ini", "tox.ini", "jest.config.ts", "mypy.ini", ".flake8", "ruff.toml", "tsconfig.json"],
)
def test_test_and_lint_config_files_fail(path):
    assert not guard([_f(path, ["a = 1"], ["a = 2"])]).ok


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("pyproject.toml", 'addopts = "-q --deselect tests/test_core.py::test_add"'),
        ("pyproject.toml", "[tool.pytest.ini_options]"),
        ("pyproject.toml", 'ignore = ["E501"]'),
        ("setup.cfg", "[tool:pytest]"),
        ("package.json", '    "test": "jest --testPathIgnorePatterns core",'),
        ("package.json", '    "testPathIgnorePatterns": ["core"],'),
    ],
)
def test_test_settings_inside_shared_files_fail(path, line):
    assert not guard([_f(path, [], [line])]).ok


def test_a_collection_hook_in_conftest_fails():
    r = guard([_f("tests/conftest.py", [], [
        "def pytest_collection_modifyitems(config, items):",
        "    items[:] = [i for i in items if 'core' not in i.nodeid]",
    ])])
    assert any("collection hook" in v for v in r.violations)


# -- what cannot be seen ---------------------------------------------------


def test_a_file_without_a_patch_fails_closed():
    r = guard([ChangedFile("src/big_generated.py", "modified", None, changes=40000)])
    assert "no patch" in r.violations[0]


def test_a_truncated_file_list_fails_closed():
    files = [_f(f"src/m{i}.py", ["a"], ["b"]) for i in range(300)]
    r = guard(files)
    assert not r.ok
    assert "limit" in r.violations[0]


def test_an_empty_diff_is_not_a_fix():
    assert not guard([]).ok


# -- flags -----------------------------------------------------------------


def test_assertions_removed_from_a_cited_test_file_are_flagged_not_failed():
    f = _f("tests/test_core.py", ["    assert add(1, 2) == 3", "    assert add(0, 0) == 0"],
           ["    assert add(1, 2) == 3"])
    r = guard([f], cited="FAILED tests/test_core.py::test_add - assert 4 == 3")
    assert r.ok
    assert "assertion" in r.flags[0]


def test_assertions_removed_from_an_uncited_file_are_not_flagged():
    f = _f("tests/test_other.py", ["    assert x"], [])
    assert guard([f], cited="tests/test_core.py").flags == ()


def test_is_test_file():
    assert is_test_file("tests/helpers.py")
    assert is_test_file("src/foo.spec.tsx")
    assert is_test_file("pkg/server_test.go")
    assert not is_test_file("src/app/core.py")


def test_violations_are_reported_once_each():
    f = _f("tests/test_core.py", [], ["@pytest.mark.skip", "@pytest.mark.skip"])
    r = guard([f])
    assert len(r.violations) == 1
    assert r.to_json()["ok"] is False
    assert "FAILED" in r.render()


@pytest.mark.parametrize(
    ("path", "why"),
    [
        (".github/workflows/ci.yml", ".github/"),
        ("pytest.ini", "configuration"),
        ("sub/jest.config.ts", "configuration"),
        ("src/checkout/cart.py", None),
        ("tests/test_cart.py", None),
        ("pyproject.toml", None),  # only some of its lines are configuration
    ],
)
def test_forbidden_is_one_rule_for_plans_and_diffs(path: str, why: str | None):
    got = forbidden(path)
    assert (got is None) if why is None else (why in (got or ""))


def test_deleting_a_config_file_is_a_violation():
    result = guard([ChangedFile("pytest.ini", "removed")])
    assert not result.ok


# -- pinning actions, for a dependency fix ---------------------------------

SHA = "8e5e7e5ab8b370d6c329ec480221332ada57f0ab"
CI = ".github/workflows/ci.yml"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("      - uses: actions/setup-python@v5", "      - uses: actions/setup-python@v5.1.0"),
        ("      - uses: actions/setup-python@v5", f"      - uses: actions/setup-python@{SHA} # v5.1.0"),
        ("        uses: 'org/action/sub@main'", f"        uses: 'org/action/sub@{SHA}'"),
        ("      - uses: actions/cache@v4  # cache", "      - uses: actions/cache@4.0.2"),
    ],
)
def test_pinning_an_action_passes_for_a_dependency_fix(old, new):
    r = guard([_f(CI, [old], [new])], allow_pins=True)
    assert r.ok, r.violations


def test_pinning_an_action_still_fails_for_any_other_fix():
    r = guard([_f(CI, ["  - uses: a/b@v1"], ["  - uses: a/b@v1.2.3"])])
    assert not r.ok and ".github/" in r.violations[0]


def test_a_pin_and_a_code_change_pass_together():
    r = guard([
        _f(CI, ["  - uses: a/b@v1"], [f"  - uses: a/b@{SHA}"]),
        _f("requirements.txt", ["foo>=1"], ["foo==1.4.2"]),
    ], allow_pins=True)
    assert r.ok, r.violations


@pytest.mark.parametrize(
    ("removed", "added", "why"),
    [
        (["  - uses: a/b@v1"], ["  - uses: a/b@v2"], "not pinned"),
        (["  - uses: a/b@v1"], ["  - uses: a/b@main"], "not pinned"),
        (["  - uses: a/b@v1"], [f"  - uses: evil/b@{SHA}"], "which action"),
        (["  - uses: a/b@v1"], ["  - uses: a/b@v1.2.3", "    continue-on-error: true"], "more than"),
        (["    run: pytest"], ["    run: pytest || true"], "more than"),
        (["    run: pytest -q"], ["    run: pytest -q -k 'not slow'"], "more than"),
        ([], ["    continue-on-error: true"], "more than"),
        (["    if: always()"], [], "more than"),
        (["  - uses: a/b@v1"], ["  - run: echo a/b@v1.2.3"], "more than"),
        (["  - uses: a/b@v1"], ["  - uses: a/b@v1.2.3", ], None),
    ],
)
def test_anything_but_a_pin_in_a_workflow_fails(removed, added, why):
    r = guard([_f(CI, removed, added)], allow_pins=True)
    if why is None:
        assert r.ok
    else:
        assert not r.ok and why in r.violations[0]


@pytest.mark.parametrize("status", ["added", "removed", "renamed"])
def test_a_workflow_file_may_only_be_modified(status):
    f = _f(CI, ["  - uses: a/b@v1"], [f"  - uses: a/b@{SHA}"], status=status,
           previous_filename=".github/workflows/old.yml" if status == "renamed" else None)
    assert not guard([f], allow_pins=True).ok


@pytest.mark.parametrize(
    "path",
    [".github/actions/setup/action.yml", ".github/CODEOWNERS", ".github/workflows/sub/x.yml",
     ".github/dependabot.yml"],
)
def test_pins_open_workflow_files_and_nothing_else_under_dot_github(path):
    r = guard([_f(path, ["  - uses: a/b@v1"], [f"  - uses: a/b@{SHA}"])], allow_pins=True)
    assert not r.ok


def test_a_workflow_file_without_a_patch_fails_closed():
    f = ChangedFile(CI, "modified", None, changes=4)
    assert pin_only(f) is not None
    assert not guard([f], allow_pins=True).ok


def test_forbidden_with_pins_opens_workflow_files_only():
    assert forbidden(CI, allow_pins=True) is None
    assert forbidden(".github/workflows/release.yaml", allow_pins=True) is None
    assert forbidden(".github/CODEOWNERS", allow_pins=True) is not None
    assert forbidden("pytest.ini", allow_pins=True) is not None
    assert forbidden(CI) is not None
