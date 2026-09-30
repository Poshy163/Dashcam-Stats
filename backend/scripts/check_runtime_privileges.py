"""Fail closed unless every running application process has dropped root."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path


def check_app_processes(proc_root: Path = Path("/proc")) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for process in proc_root.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if not any(flag == b"-m" and module == b"app.main" for flag, module in pairwise(argv)):
                continue
            status = (process / "status").read_text("utf-8")
        except FileNotFoundError:
            # A process that exited during enumeration cannot be the healthy app.
            continue
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        uids = [int(value) for value in uid_line.split()[1:]]
        if len(uids) != 4 or any(uid <= 0 for uid in uids):
            raise RuntimeError(f"application process {process.name} has unsafe or missing UIDs")
        found.append((int(process.name), uids[1]))
    if not found:
        raise RuntimeError("no application process was found; privilege drop is unverified")
    return found


if __name__ == "__main__":
    for pid, uid in check_app_processes():
        print(f"application process {pid}: effective UID {uid}")
