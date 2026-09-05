"""The image's build stamp must not sit above the layers that cost minutes to build.

CI builds this image twice on every push to main: once inside ci.yml to test it, and once
in release.yml to publish it. The two pass different build arguments -- ``ci-<sha>`` against
``main``, plus a ``BUILD_DATE`` that is a fresh timestamp every run -- so wherever those
values are consumed, that layer and every layer beneath it miss the cache.

They used to be consumed at the top of the runtime stage. The publish build therefore shared
no cached layer with the test build that had just finished: it re-ran the apt install,
re-fetched the Intel compute runtime and rebuilt the virtualenv, about ninety seconds of
work already done minutes earlier, on the critical path every single time. ``BUILD_DATE``
alone also guaranteed a miss on the second instruction of every build, so no run ever reused
the previous one's cache either.

Labels and environment do not care where they are declared. The cache does. These tests pin
that, because the fix is one an ordinary edit would undo without anything looking wrong --
moving a LABEL back to the top of a Dockerfile reads as tidying.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"

#: Build arguments that change on essentially every build, and so must be consumed last.
VOLATILE_ARGS = ("VERSION", "VCS_REF", "BUILD_DATE")


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return DOCKERFILE.read_text(encoding="utf-8").splitlines()


def _index(lines: list[str], pattern: str) -> int:
    """Line number of the first instruction matching ``pattern``."""
    for number, line in enumerate(lines):
        if re.match(pattern, line):
            return number
    raise AssertionError(f"no line in the Dockerfile matches {pattern!r}")


def _runtime_stage_start(lines: list[str]) -> int:
    return _index(lines, r"FROM .* AS runtime\b")


class TestTheBuildStampIsLast:
    @pytest.mark.parametrize("arg", VOLATILE_ARGS)
    def test_it_is_declared_after_the_expensive_layers(self, lines, arg):
        """Every layer that takes real time must be cacheable across the two builds."""
        declared = _index(lines, rf"ARG {arg}\b")

        # The three that dominate the build: the runtime apt install, the Intel compute
        # runtime download, and copying in the virtualenv built by the pydeps stage.
        apt_install = _index(lines, r"RUN set -eux;")
        intel_runtime = _index(lines, r"ARG INTEL_COMPUTE_RUNTIME")
        venv_copy = _index(lines, r"COPY --from=pydeps /opt/venv /opt/venv")

        assert declared > apt_install, f"{arg} invalidates the runtime apt install"
        assert declared > intel_runtime, f"{arg} invalidates the Intel compute runtime fetch"
        assert declared > venv_copy, f"{arg} invalidates the virtualenv copy"

    def test_nothing_reads_them_before_they_are_declared(self, lines):
        """A reference above the ARG would reintroduce the miss and still build fine.

        Docker leaves an undeclared build argument empty rather than failing, so this
        mistake produces a working image with an empty version label -- and a slow build.
        """
        for arg in VOLATILE_ARGS:
            declared = _index(lines, rf"ARG {arg}\b")
            for number, line in enumerate(lines):
                if number < declared and f"${{{arg}}}" in line:
                    raise AssertionError(
                        f"line {number + 1} reads ${{{arg}}} before it is declared "
                        f"on line {declared + 1}: {line.strip()}"
                    )

    def test_the_stamp_still_reaches_the_image(self, lines):
        """Moving it must not quietly drop it: the app reports this version at runtime."""
        text = "\n".join(lines)

        assert "ENV DASHCAM_VERSION=${VERSION}" in text
        assert 'org.opencontainers.image.version="${VERSION}"' in text
        assert 'org.opencontainers.image.revision="${VCS_REF}"' in text
        assert 'org.opencontainers.image.created="${BUILD_DATE}"' in text

    def test_the_stamp_sits_in_the_runtime_stage(self, lines):
        """An earlier stage would put it back above everything that stage builds."""
        runtime = _runtime_stage_start(lines)

        for arg in VOLATILE_ARGS:
            assert _index(lines, rf"ARG {arg}\b") > runtime


class TestBothBuildsShareOneCache:
    """The layer order only pays off if the two builds actually read the same cache."""

    def test_the_ci_build_and_the_publish_build_both_use_it(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        for name, text in (("ci.yml", ci), ("release.yml", release)):
            assert "cache-from: type=gha" in text, f"{name} does not read the shared cache"
            assert "cache-to: type=gha,mode=max" in text, f"{name} does not write it"
