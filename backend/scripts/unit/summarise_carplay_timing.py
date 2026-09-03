"""Summarise a pulled dashcam_carplay_timing.log into per-minute figures.

Usage: python summarise_carplay_timing.py <log file> [--csv out.csv]

Each log line looks like:
  2026-09-03T06:59:01Z acc=1 phone=1 load=21.8 soc=74.6 zlink_cpu=55 rx_kbit=1631
      sta=RSSI:-33/Frequency:5520MHz/ ap=5180 | layer=#104 fps=23.4 med=35.3 p95=70.6
      max=88.2 late=38% n=126 period=17.5
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"^(?P<ts>\S+) (?P<head>.*?) \| (?P<tail>.*)$")
KV = re.compile(r"(\w+)=(\S+)")


def parse(path: Path):
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        head = dict(KV.findall(m.group("head")))
        tail = m.group("tail")
        rec = {"ts": m.group("ts"), **head}
        if tail.startswith("layer="):
            rec.update(dict(KV.findall(tail)))
            rec["kind"] = "video"
        elif "no phone" in tail:
            rec["kind"] = "idle"
        else:
            rec["kind"] = "other"
        rows.append(rec)
    return rows


def num(v, default=None):
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return default


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows = parse(Path(argv[1]))
    video = [r for r in rows if r.get("kind") == "video" and num(r.get("fps")) is not None]
    print(
        f"lines: {len(rows)} | video samples: {len(video)} | idle heartbeats: {sum(1 for r in rows if r.get('kind') == 'idle')}"
    )
    if not video:
        return 0
    # Per-minute aggregation across all surfaces.
    by_min: dict[str, list[dict]] = defaultdict(list)
    for r in video:
        by_min[r["ts"][:16]].append(r)
    print()
    print(
        f"{'minute (UTC)':<17}{'samples':>8}{'fps':>7}{'late%':>7}{'p95ms':>7}{'maxms':>7}{'soc°C':>7}{'load':>7}{'zlink%':>8}{'rxkbit':>8}  sta/ap"
    )
    late_all, fps_all = [], []
    for minute in sorted(by_min):
        rs = by_min[minute]
        fps = sum(num(r["fps"], 0) for r in rs) / len(rs)
        late = sum(num(r["late"], 0) for r in rs) / len(rs)
        p95 = max(num(r.get("p95"), 0) for r in rs)
        mx = max(num(r.get("max"), 0) for r in rs)
        soc = max((num(r.get("soc"), 0) for r in rs), default=0)
        load = max((num(r.get("load"), 0) for r in rs), default=0)
        z = max((num(r.get("zlink_cpu"), 0) for r in rs), default=0)
        rx = max((num(r.get("rx_kbit"), 0) for r in rs), default=0)
        sta = rs[-1].get("sta", "")
        ap = rs[-1].get("ap", "")
        fps_all.append(fps)
        late_all.append(late)
        print(
            f"{minute:<17}{len(rs):>8}{fps:>7.1f}{late:>7.0f}{p95:>7.1f}{mx:>7.1f}{soc:>7.1f}{load:>7.1f}{z:>8.0f}{rx:>8.0f}  {sta} / {ap}"
        )
    print()
    print(
        f"overall: mean fps {sum(fps_all) / len(fps_all):.1f} | mean late {sum(late_all) / len(late_all):.0f}% | worst minute late {max(late_all):.0f}% | minutes with video {len(by_min)}"
    )
    if "--csv" in argv:
        out = Path(argv[argv.index("--csv") + 1])
        keys = [
            "ts",
            "acc",
            "phone",
            "load",
            "soc",
            "zlink_cpu",
            "rx_kbit",
            "sta",
            "ap",
            "layer",
            "fps",
            "med",
            "p95",
            "max",
            "late",
            "n",
            "period",
        ]
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(",".join(keys) + "\n")
            for r in video:
                fh.write(",".join(str(r.get(k, "")) for k in keys) + "\n")
        print("csv:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
