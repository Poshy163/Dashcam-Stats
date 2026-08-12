#!/usr/bin/env python
"""On-network check of the head-unit ingest. Needs a real unit; CI never runs this.

    python backend/scripts/ingest_smoke.py --address 192.168.1.122:5555 [--pull]

Without ``--pull`` it only looks: connect, resolve the card, list it, and report what a
transfer would fetch. That is safe to run at any time and answers the question that
actually goes wrong in practice -- whether the control channel and the card path are
healthy -- without moving a byte.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest import adb, transport
from app.ingest.puller import commit, delta


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="192.168.1.122:5555")
    parser.add_argument("--footage", default="/dashcam", help="where recordings already live")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--source", default="", help="override the card path")
    parser.add_argument("--pull", action="store_true", help="actually transfer")
    parser.add_argument("--limit", type=int, default=0, help="only fetch this many files")
    args = parser.parse_args()

    footage = Path(args.footage)
    print(f"connecting to {args.address} ...")
    info = await adb.describe(args.address, args.source)
    print(f"  state:  {info.state.value}")
    print(f"  card:   {info.source}")
    if not info.online or not info.source:
        print("the unit is not reachable; start the car and try again")
        return 1

    remote = await adb.inventory(info.address, info.source)
    total = sum(item.size for item in remote)
    print(f"  on card: {len(remote)} files, {total / 1e9:.2f} GB")

    plan = delta(remote, footage, skip_active_s=15, camera="both")
    print(f"  to fetch: {len(plan.files)} files, {plan.bytes / 1e9:.2f} GB")
    if plan.active_skipped:
        print(f"  skipped {plan.active_skipped} file(s) the camera is still writing")
    if not args.pull or not plan.files:
        return 0

    wanted = plan.files[: args.limit] if args.limit else plan.files
    staging = footage / ".ingest_staging"
    staging.mkdir(parents=True, exist_ok=True)

    print(f"starting the listener and pulling {len(wanted)} file(s) ...")
    await adb.launch_listener(
        info.address, info.source, [item.name for item in wanted], port=args.port, timeout_s=600
    )

    started = time.monotonic()
    result = await asyncio.to_thread(
        transport.receive,
        info.address.split(":", 1)[0],
        args.port,
        staging,
        on_file_done=lambda name: print(f"  {name}"),
    )
    elapsed = time.monotonic() - started

    committed = commit(staging, footage, {item.name: item.size for item in wanted})
    print(
        f"received {result.bytes_received / 1e6:.0f} MB in {elapsed:.1f}s "
        f"({result.throughput_mbs:.1f} MB/s); committed {len(committed)} file(s)"
    )
    if result.error:
        print(f"ended with: {result.error}")
    return 0 if committed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
