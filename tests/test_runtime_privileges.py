"""The container privilege smoke check must fail when its evidence is missing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backend/scripts/check_runtime_privileges.py"
SPEC = importlib.util.spec_from_file_location("check_runtime_privileges", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _process(root, pid, *, argv=b"python\0-m\0app.main\0", uids="1000 1000 1000 1000"):
    process = root / str(pid)
    process.mkdir()
    (process / "cmdline").write_bytes(argv)
    (process / "status").write_text(f"Name:\tpython\nUid:\t{uids}\n", "utf-8")


def test_unprivileged_app_is_verified_without_ps(tmp_path):
    _process(tmp_path, 1, argv=b"tini\0", uids="0 0 0 0")
    _process(tmp_path, 8)
    assert MODULE.check_app_processes(tmp_path) == [(8, 1000)]


@pytest.mark.parametrize("uids", ["0 0 0 0", "1000 0 1000 1000", "1000 1000 0 1000", ""])
def test_root_or_missing_uid_evidence_fails(tmp_path, uids):
    _process(tmp_path, 8, uids=uids)
    with pytest.raises(RuntimeError, match="unsafe or missing"):
        MODULE.check_app_processes(tmp_path)


def test_no_application_process_fails(tmp_path):
    _process(tmp_path, 1, argv=b"tini\0", uids="0 0 0 0")
    with pytest.raises(RuntimeError, match="no application process"):
        MODULE.check_app_processes(tmp_path)


def test_one_unprivileged_process_does_not_hide_a_root_worker(tmp_path):
    _process(tmp_path, 8)
    _process(tmp_path, 9, uids="0 0 0 0")
    with pytest.raises(RuntimeError, match="unsafe or missing"):
        MODULE.check_app_processes(tmp_path)
