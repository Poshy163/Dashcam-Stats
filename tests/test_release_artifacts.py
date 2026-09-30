"""Release gates preserve the exact tested image and reproducible Python versions."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text("utf-8"))


def _locked_versions(name):
    text = (ROOT / "backend" / name).read_text("utf-8")
    return {
        canonicalize_name(package): version
        for package, version in re.findall(r"^([\w.-]+)==([^\s;]+)", text, re.MULTILINE)
    }


def test_release_downloads_this_runs_tested_image_without_rebuilding():
    ci = _workflow("ci.yml")
    release = _workflow("release.yml")
    publish = release["jobs"]["publish"]
    assert publish["needs"] == "ci"
    assert "github.event_name != 'pull_request'" in publish["if"]
    assert "refs/heads/main" in publish["if"]
    assert "refs/tags/v" in publish["if"]
    assert release["jobs"]["ci"]["with"]["export-image"] is True
    assert not any("build-push-action" in step.get("uses", "") for step in publish["steps"])

    steps = ci["jobs"]["docker"]["steps"]
    upload = next(step for step in steps if "upload-artifact" in step.get("uses", ""))
    download = next(
        step for step in publish["steps"] if "download-artifact" in step.get("uses", "")
    )
    assert upload["with"]["name"] == download["with"]["name"]
    assert "github.run_id" in upload["with"]["name"]
    assert "github.run_attempt" in upload["with"]["name"]
    assert "run-id" not in download["with"]
    assert "github.event_name != 'pull_request'" in upload["if"]
    assert steps.index(upload) > next(
        index for index, step in enumerate(steps) if step.get("name") == "Smoke test the image"
    )
    verification = next(
        step for step in publish["steps"] if step.get("name") == "Verify and load the tested image"
    )
    assert verification["env"]["EXPECTED_IMAGE_ID"] == "${{ needs.ci.outputs.image-id }}"
    assert 'test "$actual" = "$EXPECTED_IMAGE_ID"' in verification["run"]
    assert 'test "$revision" = "$EXPECTED_REVISION"' in verification["run"]


def test_every_docker_base_is_digest_pinned_and_python_install_requires_hashes():
    text = (ROOT / "Dockerfile").read_text("utf-8")
    bases = re.findall(r"^FROM (\S+)", text, re.MULTILINE)
    assert len(bases) == 3
    assert all(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", base) for base in bases)
    assert "--require-hashes --only-binary=:all:" in text
    assert "requirements-linux-py312.lock" in text


def test_linux_development_and_runtime_locks_use_identical_runtime_versions():
    runtime = _locked_versions("requirements-linux-py312.lock")
    development = _locked_versions("requirements-dev-linux-py312.lock")
    assert runtime
    assert {name: development.get(name) for name in runtime} == runtime


def test_runtime_lock_and_package_metadata_match_direct_requirements():
    raw = (ROOT / "backend/requirements.txt").read_text("utf-8")
    direct = [Requirement(line) for line in raw.splitlines() if line and not line.startswith("#")]
    lock = _locked_versions("requirements-linux-py312.lock")
    project = tomllib.loads((ROOT / "backend/pyproject.toml").read_text("utf-8"))
    metadata = {str(Requirement(value)) for value in project["project"]["dependencies"]}
    assert metadata == {str(requirement) for requirement in direct}
    for requirement in direct:
        assert lock[canonicalize_name(requirement.name)] in requirement.specifier


def test_development_package_metadata_matches_the_tested_tools():
    raw = (ROOT / "backend/requirements-dev.txt").read_text("utf-8")
    direct = {
        str(Requirement(line))
        for line in raw.splitlines()
        if line and not line.startswith(("#", "-r"))
    }
    project = tomllib.loads((ROOT / "backend/pyproject.toml").read_text("utf-8"))
    metadata = project["project"]["optional-dependencies"]["dev"]
    assert {str(Requirement(value)) for value in metadata} == direct
