#!/usr/bin/env python3
"""Laboratorio 6 antes vs. después: knee, throughput máximo y primer síntoma por prueba, lado a lado.

  python3 bench/lab6_compare.py bench/results/evo/lab6-antes bench/results/evo/lab6-e4 [--png docs/img/x.png]

Usa la misma regla de knee que `summarize.py --lab6` (fallos > 0 o p95 > 10× el p95 base) sobre la curva
mediana entre corridas, y además el knee de cada corrida.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import summarize  # noqa: E402

ORDER = ["sync-ticket", "playback", "gateway/caddy", "gateway/proxy", "gateway/auth", "claim", "rooms", "ssr"]


def per_run_knees(label_dir: Path, hold_s: float) -> dict[str, list]:
    out: dict[str, list] = {}
    for path in sorted(label_dir.glob("run-*/lab6-*.json")):
        target = path.stem.removeprefix("lab6-")
        data = json.loads(path.read_text())
        if target == "gateway":
            for layer in summarize.LAB6_LAYERS:
                curve = summarize.level_curve(data, hold_s, tag=f"layer:{layer}")
                if curve:
                    out.setdefault(f"gateway/{layer}", []).append(summarize.find_knee(curve)["knee_vus"])
        else:
            out.setdefault(target, []).append(summarize.find_knee(summarize.level_curve(data, hold_s))["knee_vus"])
    return out


def symptom(curve: list[dict], knee: dict) -> str:
    if knee["degraded_vus"] is None:
        return f"sin degradación hasta {curve[-1]['vus']} VUs (p95 {curve[-1]['p95_ms']:.0f} ms)"
    p = next(p for p in curve if p["vus"] == knee["degraded_vus"])
    text = f"{p['vus']} VUs: p95 {p['p95_ms']:.0f} ms, fallos {p.get('failed', 0.0):.2%}"
    if p.get("s429"):
        text += f" ({p['s429']:.0f} × 429)"
    return text


def summary(before: Path, after: Path, hold_s: float = 30.0) -> tuple[str, dict, dict]:
    cb, ca = summarize.lab6_curves(before, hold_s), summarize.lab6_curves(after, hold_s)
    kb, ka = per_run_knees(before, hold_s), per_run_knees(after, hold_s)
    rows = ["| Prueba | Knee antes | Knee después | Knee por corrida (antes → después) | req/s máx (fallos < 1 %) antes | después | Primer síntoma antes | Primer síntoma después |",
            "| :--- | ---: | ---: | :--- | ---: | ---: | :--- | :--- |"]
    for name in ORDER:
        if name not in cb and name not in ca:
            continue
        cells = []
        for curves in (cb, ca):
            curve = curves.get(name)
            cells.append((curve, summarize.find_knee(curve)) if curve else (None, None))
        (b, kbn), (a, kan) = cells
        fmt_knee = lambda k: str(k["knee_vus"]) if k else "pendiente"  # noqa: E731
        fmt_rps = lambda c: f"{max((p.get('rps', 0.0) for p in c if p.get('failed', 0.0) < 0.01), default=0):.0f}" if c else "pendiente"  # noqa: E731
        runs = f"{', '.join(map(str, kb.get(name, [])))} → {', '.join(map(str, ka.get(name, []))) or 'pendiente'}"
        rows.append(f"| {name} | {fmt_knee(kbn)} | {fmt_knee(kan)} | {runs} | {fmt_rps(b)} | {fmt_rps(a)} | "
                    f"{symptom(b, kbn) if b else 'pendiente'} | {symptom(a, kan) if a else 'pendiente'} |")
    return "\n".join(rows) + "\n", cb, ca


def plot(cb: dict, ca: dict, out: Path, names: list[str]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, len(names), figsize=(3.6 * len(names), 6.4), sharex=True)
    for col, name in enumerate(names):
        lat, thr = axes[0][col], axes[1][col]
        for curves, label, color, style in ((cb, "antes (7362449)", "#6b7280", "--"), (ca, "después (E4)", "#2563eb", "-")):
            curve = curves.get(name)
            if not curve:
                continue
            vus = [p["vus"] for p in curve]
            lat.plot(vus, [p["p95_ms"] for p in curve], style, marker="o", color=color, label=label)
            thr.plot(vus, [p.get("rps", 0.0) for p in curve], style, marker="o", color=color, label=label)
            fails = [(p["vus"], p.get("rps", 0.0)) for p in curve if p.get("failed", 0.0) > 0]
            if fails:
                thr.scatter(*zip(*fails), marker="x", s=60, color="#dc2626", zorder=5)
        lat.set(title=name, xscale="log", yscale="log")
        thr.set(xscale="log", xlabel="VUs")
        for ax in (lat, thr):
            ax.grid(True, alpha=0.3)
            ax.spines[["top", "right"]].set_visible(False)
    axes[0][0].set_ylabel("p95 (ms)")
    axes[1][0].set_ylabel("req/s en la meseta (× = con fallos)")
    axes[0][0].legend(fontsize=8)
    fig.suptitle("Laboratorio 6: antes (2026-11-16, 7362449) vs. después (E4)")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)


def main(argv: list[str]) -> int:
    png = None
    if "--png" in argv:
        i = argv.index("--png")
        png = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    table, cb, ca = summary(Path(argv[0]), Path(argv[1]))
    print(table)
    if png:
        plot(cb, ca, png, [n for n in ORDER if n in cb or n in ca])
        print(f"gráfica: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
