#!/usr/bin/env python3
"""Resume las corridas del banco: mediana, mínimo y máximo, y compara antes y después.

Uso:  python3 bench/summarize.py results/antes-develop [results/despues] [--all]
      python3 bench/summarize.py --lab6 results/lab6 [segundos_de_meseta]   # curva por VUs y knee (Laboratorio 6)

Cada directorio de etiqueta contiene `run-N/` con `api.json`, `burst.json` y `sync.json` (`k6 --summary-export`)
y `stats-<fase>.csv` (líneas `contenedor,cpu%,memoria` de `docker stats`).
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

STAT_NAMES = {"med": "p50", "p(90)": "p90", "p(95)": "p95", "p(99)": "p99"}
UNITS = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "kB": 1 / 1048.576, "MB": 0.9537, "GB": 976.56}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STATS_LINE = re.compile(r"^([^,]+),([\d.]+)%,([\d.]+)\s*([A-Za-z]+)\s*/")


def aggregate(runs: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """Mediana, mínimo y máximo de cada métrica entre corridas (una corrida puede no tenerla)."""
    out: dict[str, dict[str, float]] = {}
    for name in sorted({k for run in runs for k in run}):
        values = [run[name] for run in runs if name in run]
        out[name] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
    return out


def is_conclusive(before: dict[str, float], after: dict[str, float]) -> bool:
    """La diferencia entre medianas debe superar la variación entre corridas de ambos lados."""
    spread = max(before["max"] - before["min"], after["max"] - after["min"])
    return abs(after["median"] - before["median"]) > spread


def k6_values(data: dict) -> dict[str, float]:
    """Aplana un `--summary-export` de k6 a `metrica.estadistico` (acepta ambos formatos)."""
    flat: dict[str, float] = {}
    for metric, body in data.get("metrics", {}).items():
        values = body.get("values", body)
        for stat, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                flat[f"{metric}.{STAT_NAMES.get(stat, stat)}"] = float(value)
    return flat


def parse_stats_line(line: str) -> tuple[str, float, float] | None:
    """`nombre,12.5%,93MiB / 7GiB` -> (nombre, cpu %, memoria en MiB)."""
    match = STATS_LINE.match(ANSI.sub("", line).strip())
    if not match or match.group(4) not in UNITS:
        return None
    name, cpu, mem, unit = match.groups()
    return name, float(cpu), float(mem) * UNITS[unit]


def summarize_resources(lines: list[str]) -> dict[str, dict[str, float]]:
    rows: dict[str, list[tuple[float, float]]] = {}
    for line in lines:
        parsed = parse_stats_line(line)
        if parsed:
            rows.setdefault(parsed[0], []).append(parsed[1:])
    return {
        name: {
            "cpu_mean": round(statistics.fmean(c for c, _ in samples), 2),
            "cpu_max": max(c for c, _ in samples),
            "mem_max_mib": round(max(m for _, m in samples), 2),
        }
        for name, samples in rows.items()
    }


def load_run(run_dir: Path) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for prefix in ("api", "burst", "sync"):
        path = run_dir / f"{prefix}.json"
        if path.exists():
            metrics.update({f"{prefix}.{k}": v for k, v in k6_values(json.loads(path.read_text())).items()})
    for stats in sorted(run_dir.glob("stats-*.csv")):
        phase = stats.stem.removeprefix("stats-")
        for name, summary in summarize_resources(stats.read_text().splitlines()).items():
            metrics.update({f"res.{phase}.{name}.{k}": v for k, v in summary.items()})
    return metrics


def load_label(label_dir: Path) -> dict[str, dict[str, float]]:
    runs = [load_run(d) for d in sorted(label_dir.glob("run-*")) if d.is_dir()]
    if not runs:
        raise SystemExit(f"{label_dir}: no hay corridas run-*")
    return aggregate(runs)


LEVEL_TAG = re.compile(r"^(?P<metric>[a-z_]+)\{(?P<tags>[^}]*)\}$")


def level_curve(data: dict, hold_s: float, tag: str | None = None) -> list[dict]:
    """Curva de rendimiento del Laboratorio 6: un punto por nivel de VUs.

    `bench/k6/lab6.js` etiqueta cada petición con `level:<vus>` solo durante la meseta de cada escalón y declara
    umbrales triviales para que k6 exporte esas submétricas. `tag` filtra por una etiqueta extra (p. ej. `layer:auth`).
    """
    points: dict[int, dict] = {}
    for name, body in data.get("metrics", {}).items():
        match = LEVEL_TAG.match(name)
        if not match:
            continue
        tags = dict(t.split(":", 1) for t in match["tags"].split(",") if ":" in t)
        if "level" not in tags or not tags["level"].isdigit():
            continue
        extra = {f"{k}:{v}" for k, v in tags.items() if k != "level"}
        if (tag and extra != {tag}) or (not tag and extra):
            continue
        values = body.get("values", body)
        point = points.setdefault(int(tags["level"]), {"vus": int(tags["level"])})
        if match["metric"] == "http_req_duration":
            point.update(avg_ms=values.get("avg", 0.0), p95_ms=values.get("p(95)", 0.0), p99_ms=values.get("p(99)", 0.0))
        elif match["metric"] == "http_req_failed":
            point["failed"] = values.get("value", values.get("rate", 0.0))
        elif match["metric"] == "http_reqs":
            point["reqs"] = int(values.get("count", 0))
            point["rps"] = round(values.get("count", 0) / hold_s, 2)
    return [points[k] for k in sorted(points) if "p95_ms" in points[k]]


def find_knee(curve: list[dict], latency_factor: float = 10.0) -> dict:
    """Knee: último nivel antes del primero con fallos (> 0 %) o con p95 > `latency_factor` × el p95 base.

    La base es el menor p95 de los niveles sanos anteriores: un arranque en frío en el primer nivel no la infla.
    """
    base = curve[0]["p95_ms"] if curve else 0.0
    knee = curve[0]["vus"] if curve else None
    for point in curve:
        base = min(base, point["p95_ms"])
        failed = point.get("failed", 0.0)
        if failed > 0:
            return {"knee_vus": knee, "degraded_vus": point["vus"], "reason": f"fallos {failed:.2%}"}
        if base and point["p95_ms"] > latency_factor * base:
            return {"knee_vus": knee, "degraded_vus": point["vus"],
                    "reason": f"p95 {point['p95_ms']:.0f} ms > {latency_factor:g}× el p95 base ({base:.0f} ms)"}
        knee = point["vus"]
    return {"knee_vus": knee, "degraded_vus": None, "reason": "sin degradación en los niveles medidos"}


def curve_markdown(curve: list[dict], knee: dict) -> str:
    rows = ["| VUs | avg (ms) | p95 (ms) | p99 (ms) | req/s | Peticiones | Fallos |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for p in curve:
        mark = " ← knee" if p["vus"] == knee["knee_vus"] else (" ← degrada" if p["vus"] == knee["degraded_vus"] else "")
        rows.append(f"| {p['vus']}{mark} | {p['avg_ms']:.2f} | {p['p95_ms']:.2f} | {p.get('p99_ms', 0.0):.2f} | "
                    f"{p.get('rps', 0.0):.1f} | {p.get('reqs', 0)} | {p.get('failed', 0.0):.2%} |")
    rows.append("")
    rows.append(f"Knee: {knee['knee_vus']} VUs; primer nivel degradado: {knee['degraded_vus'] or '-'} ({knee['reason']}).")
    return "\n".join(rows) + "\n"


def plot_curve(curves: dict[str, list[dict]], out: Path) -> bool:
    """Curva avg y p95 vs. VUs (una serie por prueba). Devuelve False si matplotlib no está instalado."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, (lat, thr) = plt.subplots(1, 2, figsize=(12, 4.5))
    for name, curve in curves.items():
        vus = [p["vus"] for p in curve]
        line = lat.plot(vus, [p["avg_ms"] for p in curve], marker="o", label=f"{name} avg")[0]
        lat.plot(vus, [p["p95_ms"] for p in curve], marker="x", linestyle="--", color=line.get_color(), label=f"{name} p95")
        thr.plot(vus, [p.get("rps", 0.0) for p in curve], marker="o", color=line.get_color(), label=name)
    lat.set(xlabel="Usuarios concurrentes (VUs)", ylabel="http_req_duration (ms)", title="Latencia", xscale="log", yscale="log")
    thr.set(xlabel="Usuarios concurrentes (VUs)", ylabel="req/s", title="Throughput", xscale="log")
    for ax in (lat, thr):
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return True


LAB6_LAYERS = ("caddy", "proxy", "auth")


def _median_curve(curves: list[list[dict]]) -> list[dict]:
    """Mediana por nivel de VUs entre corridas, campo por campo."""
    by_level: dict[int, list[dict]] = {}
    for curve in curves:
        for point in curve:
            by_level.setdefault(point["vus"], []).append(point)
    out = []
    for vus in sorted(by_level):
        points = by_level[vus]
        keys = {k for p in points for k in p if k != "vus"}
        out.append({"vus": vus, **{k: statistics.median(p[k] for p in points if k in p) for k in keys}})
    return out


def lab6_curves(label_dir: Path, hold_s: float) -> dict[str, list[dict]]:
    """Curva mediana por prueba (`run-*/lab6-<target>.json`); `gateway` se separa por capa."""
    per_target: dict[str, list[list[dict]]] = {}
    for path in sorted(label_dir.glob("run-*/lab6-*.json")):
        target = path.stem.removeprefix("lab6-")
        data = json.loads(path.read_text())
        if target == "gateway":
            for layer in LAB6_LAYERS:
                curve = level_curve(data, hold_s, tag=f"layer:{layer}")
                if curve:
                    per_target.setdefault(f"gateway/{layer}", []).append(curve)
        else:
            per_target.setdefault(target, []).append(level_curve(data, hold_s))
    return {name: _median_curve(curves) for name, curves in per_target.items()}


def _resources_section(label_dir: Path, target: str) -> list[str]:
    lines = []
    cpu = [float(row.split(",")[1]) for f in sorted(label_dir.glob(f"run-*/cpu-portatil-{target}.csv"))
           for row in f.read_text().splitlines()[1:] if "," in row]
    if cpu:
        flag = " (posible límite del generador)" if max(cpu) > 80 else ""
        lines.append(f"CPU del portátil: media {statistics.fmean(cpu):.1f} %, máx {max(cpu):.1f} %{flag}.")
    stats = [line for f in sorted(label_dir.glob(f"run-*/stats-lab6-{target}.csv")) for line in f.read_text().splitlines()]
    resources = summarize_resources(stats)
    if resources:
        lines += ["", "| Contenedor | CPU media | CPU máx | Memoria máx |", "| --- | ---: | ---: | ---: |"]
        for name, r in sorted(resources.items(), key=lambda kv: -kv[1]["cpu_max"]):
            if re.search(r"gateway|identity-api|postgres|redis|web", name):
                lines.append(f"| {name} | {r['cpu_mean']:.1f} % | {r['cpu_max']:.1f} % | {r['mem_max_mib']:.0f} MiB |")
    return lines


def lab6_report(label_dir: Path, hold_s: float, plot: bool = True) -> str:
    curves = lab6_curves(label_dir, hold_s)
    out = []
    for name, curve in curves.items():
        if not curve:
            continue
        out += [f"## {name}", "", curve_markdown(curve, find_knee(curve)).rstrip()]
        resources = _resources_section(label_dir, name.split("/")[0])
        if resources and (not name.startswith("gateway/") or name == "gateway/auth"):
            out += [""] + resources
        out.append("")
    if plot and curves:
        png = label_dir / "lab6-curva.png"
        if plot_curve({k: v for k, v in curves.items() if v}, png):
            out.append(f"Curva: {png}")
    return "\n".join(out) + "\n"


KEY_METRICS = re.compile(
    r"^(api\.http_req_duration\.p(50|95|99)"
    r"|api\.http_req_failed\.(rate|value)"
    r"|api\.http_reqs\.rate"
    r"|api\.dropped_iterations\.count"
    r"|burst\.(status_429|status_2xx|status_5xx|dropped_iterations)\.count"
    r"|burst\.http_req_duration\.p95"
    r"|sync\.(sync_broadcast_latency_ms|sync_drift_ms)\.p99"
    r"|sync\.ws_connecting\.p95"
    r"|res\.[^.]+\.[^.]*(gateway|identity-api|sync-core|postgres|redis)[^.]*\.(cpu_mean|cpu_max|mem_max_mib))$"
)


def is_key_metric(name: str) -> bool:
    return bool(KEY_METRICS.match(name))


def fmt(stat: dict[str, float]) -> str:
    return f"{stat['median']:.2f} ({stat['min']:.2f}-{stat['max']:.2f})"


def markdown(before: dict, after: dict | None = None, all_metrics: bool = False) -> str:
    header = "| Métrica | Antes: mediana (mín-máx) |"
    sep = "| --- | --- |"
    if after is not None:
        header += " Después: mediana (mín-máx) | Concluyente |"
        sep += " --- | --- |"
    rows = [header, sep]
    for name in sorted(n for n in before if all_metrics or is_key_metric(n)):
        row = f"| {name} | {fmt(before[name])} |"
        if after is not None:
            if name in after:
                row += f" {fmt(after[name])} | {'sí' if is_conclusive(before[name], after[name]) else 'no'} |"
            else:
                row += " - | - |"
        rows.append(row)
    return "\n".join(rows) + "\n"


def main(argv: list[str]) -> int:
    if argv[:1] == ["--lab6"] and len(argv) in (2, 3):
        print(lab6_report(Path(argv[1]), hold_s=float(argv[2]) if len(argv) == 3 else 30.0))
        return 0
    all_metrics = "--all" in argv
    argv = [a for a in argv if a != "--all"]
    if not 1 <= len(argv) <= 2:
        print(__doc__)
        return 2
    before = load_label(Path(argv[0]))
    after = load_label(Path(argv[1])) if len(argv) == 2 else None
    print(markdown(before, after, all_metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
