#!/usr/bin/env python3
"""Resume las corridas del banco: mediana, mínimo y máximo, y compara antes y después.

Uso:  python3 bench/summarize.py results/antes-develop [results/despues] [--all]

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
