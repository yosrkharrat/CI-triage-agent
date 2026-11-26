"""The diff guard: what a fix handed to Claude may not do to pass CI.

A green run proves the checks passed, not that the code was fixed. The cheapest
way to turn a red test green is to stop running it: delete it, skip it, rename
it out of collection, or tell the runner to leave it out. Each of those makes CI
pass and fixes nothing, and a model asked to "make CI pass" with write access
will find them sooner or later.

So a delegated fix is held to the same standard as a citation: checked
mechanically, against the diff itself, and failing closed. When the diff cannot
be seen in full (GitHub leaves out the patch of a large or binary file, and
stops listing files after 300), that is a violation, not a pass, because what
was not seen cannot be vouched for.

Two levels:

* **violations** fail the fix. Deleted or uncollected tests, added skips,
  any change under `.github/`, test or lint configuration, an incomplete diff.
* **flags** are reported for a human but do not fail it on their own. Today
  that is one thing: assertions removed from a test file the log cited, which
  is sometimes the fix (the test was wrong) and sometimes the cheat.

Pure: the input is the file list of GitHub's compare API, already fetched.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

#: GitHub's compare API lists at most this many files.
COMPARE_FILE_LIMIT = 300


@dataclass(frozen=True)
class ChangedFile:
    """One entry of the compare API's `files`, reduced to what the guard reads."""

    filename: str
    status: str  # added, removed, modified, renamed, copied, changed, unchanged
    patch: str | None = None
    previous_filename: str | None = None
    changes: int = 0

    @classmethod
    def from_api(cls, f: dict) -> ChangedFile:
        return cls(
            filename=f["filename"],
            status=f.get("status", "modified"),
            patch=f.get("patch"),
            previous_filename=f.get("previous_filename"),
            changes=int(f.get("changes", 0)),
        )

    def lines(self) -> tuple[list[str], list[str]]:
        """(removed, added) lines of the patch, without their +/- prefix."""
        removed: list[str] = []
        added: list[str] = []
        for line in (self.patch or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                removed.append(line[1:])
        return removed, added


@dataclass(frozen=True)
class GuardResult:
    violations: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    files: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "flags": list(self.flags),
            "files": self.files,
        }

    def render(self) -> str:
        if self.ok and not self.flags:
            return f"guard passed ({self.files} file(s) checked)"
        lines = [f"guard {'passed' if self.ok else 'FAILED'} ({self.files} file(s) checked)"]
        lines += [f"- violation: {v}" for v in self.violations]
        lines += [f"- flag: {f}" for f in self.flags]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# What counts as a test, a skip, and configuration
# --------------------------------------------------------------------------

_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
_TEST_NAME = re.compile(
    r"(^test_.*\.py$|.*_test\.py$|^conftest\.py$|.*_test\.go$|.*\.(test|spec)\.[cm]?[jt]sx?$"
    r"|.*Test\.(java|kt)$|.*Tests?\.cs$|.*_spec\.rb$)"
)


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(_TEST_NAME.match(p.name)) or any(part in _TEST_DIRS for part in p.parts[:-1])


#: Definitions a test runner collects, as (kind, name). A name that leaves a
#: file's multiset without coming back is a test that no longer runs, whether
#: it was deleted or renamed (`test_x` → `_test_x`, `it(` → `xit(`).
_TEST_DEFS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\("),  # pytest / unittest
    re.compile(r"^\s*class\s+(Test\w*)\b"),  # pytest / unittest classes
    re.compile(r"^\s*func\s+(Test\w+|Benchmark\w+|Fuzz\w+)\s*\("),  # go
    re.compile(r"""(?:^|[\s;(])(?:it|test)\s*\(\s*(['"`].+?['"`])"""),  # jest / mocha / vitest
    re.compile(r"""(?:^|[\s;(])describe\s*\(\s*(['"`].+?['"`])"""),
    re.compile(r"^\s*(#\[(?:tokio::)?test\])"),  # rust: the attribute is the test
    re.compile(r"^\s*(@Test)\b"),  # junit
]

#: Added anywhere in a test file, each of these stops a test from failing
#: without making it pass.
_SKIPS = [
    (re.compile(r"\bpytest\.mark\.(skip|skipif|xfail)\b"), "pytest skip/xfail marker"),
    (re.compile(r"\bpytest\.(skip|xfail|importorskip)\s*\("), "pytest.skip/xfail call"),
    (re.compile(r"@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b"), "unittest skip"),
    (re.compile(r"\bself\.skipTest\s*\("), "unittest skipTest"),
    (re.compile(r"(?:^|[\s;(])(?:it|test|describe)\.(skip|only|todo)\s*\("), "jest/mocha .skip/.only"),
    (re.compile(r"(?:^|[\s;(])x(?:it|test|describe)\s*\("), "jest/mocha xit/xdescribe"),
    (re.compile(r"(?:^|[\s;(])f(?:it|describe)\s*\("), "jest fit/fdescribe"),
    (re.compile(r"\bt\.(Skip|SkipNow|Skipf)\s*\("), "go t.Skip"),
    (re.compile(r"#\[ignore\b"), "rust #[ignore]"),
    (re.compile(r"@(Disabled|Ignore)\b"), "junit @Disabled/@Ignore"),
    (re.compile(r"#\s*type:\s*ignore"), "# type: ignore"),
    (re.compile(r"#\s*noqa\b"), "# noqa"),
    (re.compile(r"//\s*(eslint-disable|@ts-ignore|@ts-expect-error|nolint)"), "lint suppression"),
]

#: Files that decide what runs, or how strictly. Any change to one fails.
_CONFIG_NAMES = re.compile(
    r"^(pytest\.ini|tox\.ini|noxfile\.py|\.coveragerc|mypy\.ini|\.mypy\.ini|ruff\.toml|\.ruff\.toml"
    r"|\.flake8|\.pylintrc|pylintrc|\.pre-commit-config\.ya?ml|\.golangci\.ya?ml"
    r"|jest\.config\.[cm]?[jt]s(on)?|vitest\.config\.[cm]?[jt]s|karma\.conf\.js|\.mocharc\.\w+"
    r"|\.eslintrc(\.\w+)?|eslint\.config\.[cm]?[jt]s|\.eslintignore|tsconfig\.json|codecov\.ya?ml)$"
)
#: Shared files where only some lines are configuration. A changed line that
#: matches here fails; the rest of the file (a dependency, a version) does not.
_CONFIG_LINES: dict[str, re.Pattern[str]] = {
    "pyproject.toml": re.compile(
        r"^\s*\[tool\.(pytest|ruff|mypy|coverage|pyright|black|isort)"
        r"|\b(addopts|testpaths|python_files|python_functions|python_classes|norecursedirs"
        r"|filterwarnings|xfail_strict|ignore|extend-ignore|per-file-ignores|exclude"
        r"|extend-exclude|select|ignore_errors|ignore_missing_imports|strict)\s*="
        r"|--deselect|--ignore|\s-k\s"
    ),
    "setup.cfg": re.compile(
        r"^\s*\[(tool:pytest|flake8|mypy|coverage)"
        r"|\b(addopts|testpaths|python_files|norecursedirs|ignore|exclude|per-file-ignores)\s*="
        r"|--deselect|--ignore|\s-k\s"
    ),
    "package.json": re.compile(
        r'"(test|test:\w+|lint|typecheck|check|ci|jest|mocha|vitest|eslintConfig)"\s*:'
        r'|testPathIgnorePatterns|testMatch|testRegex|modulePathIgnorePatterns|"skip'
    ),
    "Cargo.toml": re.compile(r"^\s*\[(lints|\[test\]|workspace\.lints)|\btest\s*=\s*false"),
    "Makefile": re.compile(r"^\s*(test|lint|check)\s*:|pytest|go test|cargo test|npm test"),
}
#: Collection hooks: Python code in conftest.py that removes tests at runtime.
_COLLECTION_HOOK = re.compile(
    r"\b(pytest_collection_modifyitems|pytest_ignore_collect|pytest_collect_file"
    r"|pytest_runtest_setup|collect_ignore(_glob)?|deselect)\b"
)
_ASSERTION = re.compile(
    r"\bassert\b|\bexpect\s*\(|\.assert\w*\s*\(|\bassert\w*!?\s*\(|\bt\.(Error|Fatal|Fail)\w*\s*\("
    r"|\bshould\b|\brequire\.\w+\s*\("
)


def forbidden(path: str) -> str | None:
    """Why a fix may not touch `path` at all, or None when it may.

    Only the rules that need nothing but the path: anything under `.github/`,
    and files whose whole purpose is deciding what runs or how strictly. The
    guard checks a diff against it, and the planner checks a plan's steps
    against it, so the two are held to one definition.
    """
    if path.startswith(".github/"):
        return "changes under .github/ are not allowed"
    if _CONFIG_NAMES.match(PurePosixPath(path).name):
        return "test, lint or CI configuration changed"
    return None


def _test_defs(lines: list[str]) -> Counter[str]:
    found: Counter[str] = Counter()
    for line in lines:
        for pattern in _TEST_DEFS:
            if m := pattern.search(line):
                found[m.group(1)] += 1
                break
    return found


def _cited(path: str, cited: str) -> bool:
    """Whether a log quote names this file, by path or by a distinctive basename."""
    if not cited:
        return False
    p = PurePosixPath(path)
    return path in cited or (len(p.name) > 6 and p.name in cited)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


def guard(
    files: list[ChangedFile], *, truncated: bool = False, cited: str = ""
) -> GuardResult:
    """Check a fix's diff against the rules a fix must keep.

    `files` is the compare API's file list between the PR head the fix started
    from and the fix branch. `truncated` says GitHub cut the list short. `cited`
    is the text of the log quotes the diagnosis rested on, used to tell which
    test files the failure pointed at.
    """
    violations: list[str] = []
    flags: list[str] = []

    if truncated or len(files) >= COMPARE_FILE_LIMIT:
        violations.append(
            f"the diff lists {len(files)} files, GitHub's limit; what it left out cannot be checked"
        )
    if not files:
        violations.append("the fix branch changes nothing")

    for f in files:
        path = f.filename
        name = PurePosixPath(path).name
        before = f.previous_filename or path
        test = is_test_file(path) or is_test_file(before)

        if why := forbidden(path) or forbidden(before):
            violations.append(f"{path}: {why}")
            continue

        if f.status == "removed":
            if test:
                violations.append(f"{path}: test file deleted")
            continue

        # By name, not directory: `tests/test_core.py` → `tests/core_checks.py`
        # stays under tests/ and is still no longer collected.
        if f.status == "renamed" and (
            (is_test_file(before) and not is_test_file(path))
            or (_TEST_NAME.match(PurePosixPath(before).name)
                and not _TEST_NAME.match(PurePosixPath(path).name))
        ):
            violations.append(f"{before} → {path}: test file renamed out of collection")
            continue

        if f.patch is None:
            # A pure rename with no content change has no patch, legitimately.
            if f.status == "renamed" and f.changes == 0:
                continue
            violations.append(f"{path}: GitHub returned no patch (large or binary); cannot check it")
            continue

        removed, added = f.lines()

        if (pattern := _CONFIG_LINES.get(name)) and any(
            pattern.search(line) for line in removed + added
        ):
            violations.append(f"{path}: test, lint or CI settings changed")
            continue

        if not test:
            continue

        lost = _test_defs(removed) - _test_defs(added)
        if lost:
            names = ", ".join(sorted(lost))
            violations.append(f"{path}: test(s) no longer collected: {names}")

        for line in added:
            for pattern, what in _SKIPS:
                if pattern.search(line):
                    violations.append(f"{path}: added {what}: {line.strip()[:120]}")
                    break

        if name == "conftest.py" and any(_COLLECTION_HOOK.search(line) for line in added):
            violations.append(f"{path}: collection hook added or changed in conftest.py")

        if _cited(path, cited):
            gone = sum(bool(_ASSERTION.search(line)) for line in removed)
            came = sum(bool(_ASSERTION.search(line)) for line in added)
            if gone > came:
                flags.append(
                    f"{path}: {gone - came} assertion(s) removed from a test file the log cited"
                )

    return GuardResult(tuple(dict.fromkeys(violations)), tuple(flags), files=len(files))


# Exposed for the brief, so the rules Claude is given and the rules it is held
# to are written in one place.
RULES = (
    "Do not delete, skip, xfail, rename or otherwise stop collecting any test.",
    "Do not edit anything under .github/, or any test, lint, coverage or CI configuration "
    "(pytest.ini, [tool.pytest] / [tool.ruff] / [tool.mypy], conftest collection hooks, "
    "jest config, the test script in package.json, ...).",
    "Do not add `# type: ignore`, `noqa`, `@ts-ignore` or other suppressions in test files.",
    "Keep the change minimal: fix the cause the log shows, nothing else.",
)

