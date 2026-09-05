"""Scripts that get executed must not carry carriage returns into the thing that runs them.

A shell interpreter reads a shebang byte for byte, and a carriage return is not whitespace
to it -- it is part of the interpreter's name. ``#!/usr/bin/env bash\\r`` asks the kernel for
a program called ``bash\\r``, and the failure is::

    /usr/bin/env: 'bash\\r': No such file or directory

with the container exiting 127 before one line of the application runs.

That happened. Building the image from a Windows checkout produced exactly it: git's
``core.autocrlf=true`` rewrote ``docker/entrypoint.sh`` to CRLF on the way out of the object
store, the Dockerfile copied it in byte for byte, and the image could not start. Every check
in CI passed throughout, because a Linux runner checks the same commit out with LF -- so the
defect was invisible to automation and reachable only by a contributor on Windows.

These tests pin both halves of the fix, because they cover different routes in: the
``.gitattributes`` rules keep CRLF out of a clone, and the Dockerfile's ``sed`` keeps it out
of everything else -- a source zip from the Releases page, an editor that rewrites on save, a
build context copied from a Windows host that never went through git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Scripts whose bytes are handed to an interpreter rather than read by a parser that
#: tolerates stray carriage returns.
EXECUTED_SCRIPTS = (
    "docker/entrypoint.sh",
    "backend/app/ingest/carplay_timing.sh",
    "android/obd-logger/gradlew",
)


def _tracked_blob(path: str) -> bytes:
    """The bytes git actually stores, which is what a fresh clone starts from."""
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("path", EXECUTED_SCRIPTS)
def test_the_stored_blob_has_no_carriage_returns(path):
    """The repository's own copy must be clean, whatever a checkout then does to it."""
    blob = _tracked_blob(path)

    assert b"\r\n" not in blob, f"{path} is stored with CRLF; a clone cannot fix that"


@pytest.mark.parametrize("path", EXECUTED_SCRIPTS)
def test_the_working_copy_has_no_carriage_returns(path):
    """What is on disk right now is what a local `docker build` will bake in."""
    data = (ROOT / path).read_bytes()

    assert b"\r\n" not in data, (
        f"{path} has CRLF in the working tree. On Windows this is what git's "
        f"core.autocrlf does without a .gitattributes rule, and the image built from it "
        f"will exit 127 with \"/usr/bin/env: 'bash\\r': No such file or directory\"."
    )


@pytest.mark.parametrize("path", EXECUTED_SCRIPTS)
def test_git_is_told_to_check_it_out_with_lf(path):
    """The rule that stops the next Windows clone reintroducing this.

    Asserted through ``git check-attr`` rather than by reading .gitattributes, because what
    matters is the rule that actually resolves for this path -- a later pattern can override
    an earlier one, and a glob can miss a file whose name does not match it.
    """
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().endswith(": lf"), (
        f"no .gitattributes rule pins {path} to LF on checkout: {result.stdout.strip()}"
    )


class TestTheImageDefendsItself:
    """.gitattributes fixes `git clone`. It does not fix a source zip or a stray editor."""

    def test_the_entrypoint_is_stripped_before_it_is_made_executable(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        assert "sed -i 's/\\r$//' /usr/local/bin/entrypoint.sh" in dockerfile, (
            "the Dockerfile must strip carriage returns from the entrypoint; a build "
            "context that never went through git has no .gitattributes to protect it"
        )

    def test_it_happens_in_the_same_layer_as_chmod(self):
        """Ordering, not just presence: stripping after chmod would still ship a broken one.

        Kept as one RUN so the two cannot drift into separate layers where a later edit
        reorders them without anything looking wrong.
        """
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        strip = dockerfile.index("sed -i 's/\\r$//' /usr/local/bin/entrypoint.sh")
        chmod = dockerfile.index("chmod +x /usr/local/bin/entrypoint.sh")

        assert strip < chmod
