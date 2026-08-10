"""Ruff runs here too, so formatting drift fails locally instead of on GitHub.

CI ran `ruff format --check` from the start, and it worked exactly as designed: it went red
and stayed red for three commits. What it could not do was tell anyone *before* the push,
because nothing in the local loop ran it. `pytest` is what gets run locally, so this is
where the check belongs as well.

The failure that prompted this is worth stating, because the obvious reading of it is
wrong. The formatting was not sloppy; the *formatters differed*. `requirements-dev.txt`
asked for ``ruff>=0.8,<0.15`` and CI resolved 0.14.14, while the machine the code was
written on had 0.16.2 installed globally. The two disagree about a blank line after a
function signature, so both were correct and they still could not agree. A version range
is fine for a linter, where new rules are additive and visible; for a formatter it means
"correctly formatted" is not a property of the code at all. Hence the exact pin, and hence
this test checking that the pin is what is actually installed -- otherwise the check below
would be measuring a different tool from the one that guards the branch.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "backend" / "requirements-dev.txt"

#: The same paths .github/workflows/ci.yml passes, so this cannot pass while CI fails.
CHECKED = ("backend/app", "backend/scripts", "tests")


def pinned_version() -> str | None:
    """The exact ruff version this project builds against, read from the requirements."""
    if not REQUIREMENTS.is_file():
        return None
    match = re.search(
        r"^ruff==([0-9][^\s#]*)", REQUIREMENTS.read_text(encoding="utf-8"), re.MULTILINE
    )
    return match.group(1) if match else None


def run_ruff(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def ruff() -> str:
    result = run_ruff("--version")
    if result.returncode != 0:
        pytest.skip("ruff is not installed; pip install -r backend/requirements-dev.txt")
    return result.stdout.strip().removeprefix("ruff ").strip()


class TestTheFormatterIsTheOneCiUses:
    def test_the_installed_version_matches_the_pin(self, ruff):
        """A formatting check against a different version is not a check.

        This fails rather than skips on purpose. Skipping is what let the mismatch through
        the first time: everything local was green while CI was red, and nothing anywhere
        said the two were running different tools.
        """
        expected = pinned_version()
        assert expected, "backend/requirements-dev.txt no longer pins ruff to an exact version"
        assert ruff == expected, (
            f"ruff {ruff} is installed but this project pins {expected}. Formatting differs "
            f"between versions, so this check and CI would disagree.\n"
            f"    pip install ruff=={expected}"
        )


class TestTheTreeIsClean:
    def test_it_is_formatted(self, ruff):
        result = run_ruff("format", "--check", *CHECKED)
        if result.returncode != 0:
            diff = run_ruff("format", "--diff", *CHECKED)
            pytest.fail(
                "ruff format would change these files, so CI will reject them:\n"
                f"{result.stdout}{result.stderr}\n{diff.stdout[:4000]}"
            )

    def test_it_lints(self, ruff):
        result = run_ruff("check", *CHECKED)
        if result.returncode != 0:
            pytest.fail(f"ruff check found problems:\n{result.stdout}{result.stderr}")
