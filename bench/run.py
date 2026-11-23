#!/usr/bin/env python3
"""Orquestador del banco de medición de seguridad vs. rendimiento (Prototipo 3).

Corre en el portátil (generador de carga) y maneja el servidor por SSH. Solo usa la biblioteca estándar.

  python3 bench/run.py prepare                     # para el stack público y deja listo el clon aislado
  python3 bench/run.py measure --label antes-develop
  python3 bench/run.py security --label antes-develop
  python3 bench/run.py segmentation --label antes-develop   # escenario 3: alcanzabilidad desde web e identity-api
  python3 bench/run.py restore                     # baja el banco y devuelve el servidor a como estaba
  python3 bench/summarize.py bench/results/antes-develop [bench/results/despues]

Detalles y método en docs/pruebas-seguridad.md. El servidor del que parte es Windows con Docker Desktop;
el stack de pruebas corre con el proyecto de Compose `tintbench` (volúmenes propios) y no toca los
volúmenes del stack público.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import segmentation_checks  # noqa: E402
import summarize  # noqa: E402

BENCH = Path(__file__).parent
ROOT = BENCH.parent
RESULTS = BENCH / "results"
REMOTE_BENCH = r"C:\tint\bench"
REMOTE_REPO = REMOTE_BENCH + r"\tint-monorepo"
PUBLIC_REPO = r"C:\tint\tint-monorepo"
PUBLIC_OVERRIDE = r"C:\tint\compose.server.yml"
TASK = "TINT Deploy"
SSH_NOISE = ("post-quantum", "store now, decrypt later", "may need to be upgraded", "openssh.com/pq")
CALIBRATION_RATES = [25, 50, 100, 200, 400, 800, 1200]


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.host = args.host
        self.ip = args.ip
        self.port = args.https_port
        self.image_tag = getattr(args, "image_tag", "")

    @property
    def base(self) -> str:
        # El sitio de Caddy es https://localhost: k6 y curl resuelven `localhost` a la IP del servidor.
        return f"https://localhost:{self.port}"


def ssh(ctx: Ctx, command: str, check: bool = True, timeout: int = 900) -> str:
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", ctx.host, command], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    noise = lambda text: "\n".join(line for line in text.splitlines() if not any(n in line for n in SSH_NOISE))  # noqa: E731
    if check and result.returncode != 0:
        raise RuntimeError(f"ssh falló ({result.returncode}): {command[:80]}\n{noise(result.stderr)[:500]}")
    return noise(result.stdout).strip()


def scp(ctx: Ctx, local: Path, remote_dir: str) -> None:
    subprocess.run(["scp", "-o", "BatchMode=yes", "-q", str(local), f"{ctx.host}:{remote_dir}"], check=True, capture_output=True)


def compose_services(text: str) -> list[str]:
    """Nombres de los servicios de un docker-compose.yml, en orden."""
    names, inside = [], False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            break
        elif inside and (m := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)):
            names.append(m.group(1))
    return names


def filter_bench_override(text: str, keep: list[str]) -> str:
    """Quita de compose.bench.yml los servicios que el commit medido no tiene (p. ej. audit-log en 5767874)."""
    out, skipping, inside = [], False, False
    for line in text.splitlines(keepends=True):
        if re.match(r"^services:\s*$", line):
            inside = True
        elif inside and (m := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)):
            skipping = m.group(1) not in keep
        elif inside and re.match(r"^\S", line):
            inside = skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def compose_file_value(bench_dir: str, extra: str = "") -> str:
    """COMPOSE_FILE del banco: el Compose del repo, el override del banco y, si se pide, otro más."""
    files = ["docker-compose.yml", f"{bench_dir}\\compose.bench.yml"]
    if extra:
        files.append(f"{bench_dir}\\{extra}")
    return ";".join(files)


def compose(ctx: Ctx, args: str, **kwargs) -> str:
    prefix = f"set BENCH_IMAGE_TAG={ctx.image_tag}&& " if ctx.image_tag else ""
    return ssh(ctx, f"{prefix}cd /d {REMOTE_REPO} && docker compose {args}", **kwargs)


def public_compose(ctx: Ctx, args: str) -> str:
    return ssh(ctx, f'cd /d {PUBLIC_REPO} && docker compose -f docker-compose.yml -f "{PUBLIC_OVERRIDE}" {args}')


def http_status(ctx: Ctx, path: str) -> int:
    result = subprocess.run(["curl", "-sk", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}",
                             "--resolve", f"localhost:{ctx.port}:{ctx.ip}", f"{ctx.base}{path}"],
                            capture_output=True, text=True)
    return int(result.stdout) if result.stdout.isdigit() else 0


def wait_ready(ctx: Ctx, timeout: int = 360) -> None:
    deadline = time.time() + timeout
    stable = 0
    while time.time() < deadline:
        ready = http_status(ctx, "/api/v1/healthz") == 200
        pending = ssh(ctx, 'docker ps --filter "health=starting" --filter "health=unhealthy" --format "{{.Names}}"', check=False)
        stable = stable + 1 if ready and not pending else 0
        if stable >= 3:
            return
        time.sleep(5)
    raise RuntimeError("el stack del banco no quedó sano a tiempo")


def reset(ctx: Ctx) -> None:
    compose(ctx, "down -v --remove-orphans")
    compose(ctx, "up -d --no-build")
    wait_ready(ctx)
    time.sleep(20)


def remote_jwt_secret(ctx: Ctx) -> str:
    out = ssh(ctx, f"findstr /B JWT_SECRET= {REMOTE_REPO}\\.env")
    return out.split("=", 1)[1].strip()


# --- prepare / restore ---------------------------------------------------------------------------


def cmd_prepare(ctx: Ctx, args: argparse.Namespace) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    state = RESULTS / "_estado_servidor.json"
    if not state.exists():  # no sobrescribe el estado original si se repite prepare
        state.write_text(json.dumps({
            "contenedores": ssh(ctx, 'docker ps --format "{{.Names}}"').splitlines(),
            "tarea": ssh(ctx, f'schtasks /Query /TN "{TASK}" /FO LIST').splitlines(),
        }, indent=2))
    commit = args.commit or ssh(ctx, f"git -C {PUBLIC_REPO} rev-parse HEAD")
    ssh(ctx, f'schtasks /Change /TN "{TASK}" /DISABLE')
    public_compose(ctx, "stop")
    ssh(ctx, f"if not exist {REMOTE_BENCH} mkdir {REMOTE_BENCH}")
    scp(ctx, BENCH / "remote" / "setup.ps1", "C:/tint/bench/")
    # El override del banco puede nombrar servicios que el commit medido aún no tenía: se filtra.
    shown = subprocess.run(["git", "show", f"{commit}:docker-compose.yml"], cwd=ROOT, capture_output=True, text=True)
    override = (BENCH / "compose.bench.yml").read_text(encoding="utf-8")
    if shown.returncode == 0:
        override = filter_bench_override(override, compose_services(shown.stdout))
    with tempfile.TemporaryDirectory() as tmp:
        filtered = Path(tmp) / "compose.bench.yml"
        filtered.write_text(override, encoding="utf-8")
        scp(ctx, filtered, "C:/tint/bench/")
    extra = args.extra_compose
    if extra:
        scp(ctx, BENCH / extra, "C:/tint/bench/")
    secret = secrets.token_hex(24)
    print(ssh(ctx, f"powershell -NoProfile -ExecutionPolicy Bypass -File {REMOTE_BENCH}\\setup.ps1 "
                   f"-Commit {commit} -Secret {secret}" + (f" -ExtraCompose {extra}" if extra else "")))


def cmd_restore(ctx: Ctx, _args: argparse.Namespace) -> None:
    compose(ctx, "down -v --remove-orphans", check=False)
    public_compose(ctx, "start")
    ssh(ctx, f'schtasks /Change /TN "{TASK}" /ENABLE')
    print("stack público arrancado y tarea habilitada. Estado original guardado en bench/results/_estado_servidor.json")
    print(ssh(ctx, 'docker ps --format "{{.Names}} {{.Status}}"'))


# --- medición ------------------------------------------------------------------------------------


def k6(ctx: Ctx, script: str, out: Path, env: dict[str, str]) -> int:
    cmd = ["k6", "run", "--insecure-skip-tls-verify", "--summary-trend-stats=min,med,p(90),p(95),p(99),max",
           "--summary-export", str(out)]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    result = subprocess.run([*cmd, str(ROOT / script)], capture_output=True, text=True, cwd=ROOT)
    out.with_suffix(".log").write_text(result.stdout + result.stderr)
    return result.returncode


class Sampler:
    """`docker stats` del servidor a un CSV mientras dura el bloque `with`."""

    def __init__(self, ctx: Ctx, path: Path):
        self.ctx, self.path = ctx, path

    def __enter__(self):
        self.file = self.path.open("w")
        fmt = '"{{.Name}},{{.CPUPerc}},{{.MemUsage}}"'
        self.proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", self.ctx.host, f"docker stats --format {fmt}"],
                                     stdout=self.file, stderr=subprocess.DEVNULL)
        time.sleep(3)
        return self

    def __exit__(self, *exc):
        time.sleep(2)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.file.close()


def one(values: dict[str, float], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in values:
            return values[key]
    return default


def calibrate(ctx: Ctx, label_dir: Path, hosts: int) -> int:
    """Sube la tasa por escalones de 30 s hasta que p95 > 500 ms, errores > 1 % o se pierden iteraciones."""
    reset(ctx)
    steps, last_ok = [], 0
    for rate in CALIBRATION_RATES:
        out = label_dir / f"calibration-{rate}.json"
        k6(ctx, "bench/k6/api.js", out, {"BASE_URL": ctx.base, "SERVER_IP": ctx.ip, "RATE": str(rate), "DURATION": "30s", "HOSTS": str(hosts)})
        values = summarize.k6_values(json.loads(out.read_text()))
        p95 = one(values, "http_req_duration.p95")
        failed = one(values, "http_req_failed.rate", "http_req_failed.value")
        dropped = one(values, "dropped_iterations.count") / (rate * 30)
        ok = p95 <= 500 and failed <= 0.01 and dropped <= 0.01
        steps.append({"rate": rate, "p95_ms": p95, "failed": failed, "dropped": round(dropped, 4), "ok": ok})
        print(f"  calibración {rate:>5} req/s: p95={p95:.0f} ms fallos={failed:.3f} perdidas={dropped:.3f} -> {'ok' if ok else 'satura'}")
        if not ok:
            break
        last_ok = rate
    result = {"max_rps": last_ok, "rate_used": max(1, round(last_ok * 0.5)), "steps": steps}
    (label_dir / "calibration.json").write_text(json.dumps(result, indent=2))
    return result["rate_used"]


def capture_env(ctx: Ctx, rate: int) -> dict:
    return {
        "fecha": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit_servidor": ssh(ctx, f"git -C {REMOTE_REPO} rev-parse HEAD"),
        "docker": ssh(ctx, 'docker version --format "{{.Server.Version}}"'),
        "k6": subprocess.run(["k6", "version"], capture_output=True, text=True).stdout.strip(),
        "ruta_portatil": subprocess.run(["ip", "route", "get", ctx.ip], capture_output=True, text=True).stdout.splitlines()[0],
        "tasa_api_req_s": rate,
        "nota": "k6 usa --insecure-skip-tls-verify y resuelve localhost a la IP del servidor (certificado interno de Caddy)",
    }


def idle_check(ctx: Ctx) -> str:
    """CPU de contenedores ajenos al banco (p. ej. transcriptor) antes de medir."""
    out = ssh(ctx, 'docker stats --no-stream --format "{{.Name}},{{.CPUPerc}},{{.MemUsage}}" transcriptor', check=False)
    parsed = summarize.parse_stats_line(out.splitlines()[0]) if out else None
    if parsed and parsed[1] > 5.0:
        raise RuntimeError(f"transcriptor no está en reposo ({parsed[1]}% CPU): se descarta la corrida")
    return out


def cmd_measure(ctx: Ctx, args: argparse.Namespace) -> None:
    label_dir = RESULTS / args.label
    label_dir.mkdir(parents=True, exist_ok=True)
    if args.smoke:
        args.runs, args.api_duration, args.burst_duration, args.sync_vus, args.sync_duration, args.warmup = 1, "15s", "5s", 5, "10s", 0
        args.rate, args.skip_calibration, args.hosts = args.rate or 10, True, 2
    if args.rate:
        rate = args.rate
    elif args.skip_calibration:
        rate = json.loads((label_dir / "calibration.json").read_text())["rate_used"]
    else:
        rate = calibrate(ctx, label_dir, args.hosts)
    print(f"Tasa de la fase de latencia: {rate} req/s")
    secret = remote_jwt_secret(ctx)

    for n in range(1, args.runs + 1):
        run_dir = label_dir / f"run-{n}"
        run_dir.mkdir(exist_ok=True)
        print(f"== corrida {n}/{args.runs}")
        reset(ctx)
        idle = idle_check(ctx)
        api_env = {"BASE_URL": ctx.base, "SERVER_IP": ctx.ip, "HOSTS": str(args.hosts)}
        if args.warmup:
            k6(ctx, "bench/k6/api.js", run_dir / "warmup.json", {**api_env, "RATE": str(rate), "DURATION": f"{args.warmup}s"})
        codes = {}
        with Sampler(ctx, run_dir / "stats-api.csv"):
            codes["api"] = k6(ctx, "bench/k6/api.js", run_dir / "api.json",
                              {**api_env, "RATE": str(rate), "DURATION": args.api_duration})
        with Sampler(ctx, run_dir / "stats-burst.csv"):
            codes["burst"] = k6(ctx, "bench/k6/api.js", run_dir / "burst.json",
                                {**api_env, "PHASE": "burst", "RATE": str(args.burst_rate), "DURATION": args.burst_duration})
        with Sampler(ctx, run_dir / "stats-sync.csv"):
            codes["sync"] = k6(ctx, "bench/k6/sync-gateway.js", run_dir / "sync.json", {
                "BASE_URL": f"wss://localhost:{ctx.port}", "SERVER_IP": ctx.ip, "JWT_SECRET": secret, "VUS": str(args.sync_vus),
                "DURATION": args.sync_duration, "ROOM_ID": f"bench-{args.label}-{n}",
            })
        meta = {**capture_env(ctx, rate), "transcriptor_antes": idle, "k6_exit": codes}
        (run_dir / "env.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        print(f"   k6 exit codes (0 = umbrales cumplidos): {codes}")

    print("\n" + summarize.markdown(summarize.load_label(label_dir)))


def cmd_security(ctx: Ctx, args: argparse.Namespace) -> None:
    out = RESULTS / args.label / "security.json"
    subprocess.run([sys.executable, str(BENCH / "security_checks.py"), "--ip", ctx.ip, "--https-port", str(ctx.port),
                    "--out", str(out)], check=True)


def cmd_up(ctx: Ctx, _args: argparse.Namespace) -> None:
    """Recrea el stack del banco desde cero (down -v, up) y espera a que quede sano."""
    reset(ctx)
    print("stack del banco sano")


def cmd_build(ctx: Ctx, _args: argparse.Namespace) -> None:
    """Construye en el servidor las imágenes del commit fijado con la etiqueta --image-tag."""
    if not ctx.image_tag:
        raise SystemExit("build necesita --image-tag (p. ej. --image-tag antes)")
    print(compose(ctx, "build", timeout=3600))


def cmd_segmentation(ctx: Ctx, args: argparse.Namespace) -> None:
    """Escenario 3: qué almacenes alcanzan `web` e `identity-api` y la latencia de PING a Redis."""
    out = RESULTS / args.label / "segmentation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = segmentation_checks.run_checks(
        lambda service, js: compose(ctx, segmentation_checks.exec_command(service, js), check=False, timeout=120))
    result["taken_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="escritorio", help="alias SSH del servidor")
    parser.add_argument("--ip", default="192.168.1.7")
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--image-tag", default="", help="BENCH_IMAGE_TAG para el 'después' (por defecto: latest)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--commit", default="", help="commit a medir (por defecto el del clon público)")
    prepare.add_argument("--extra-compose", default="", help="override de bench/ que se suma al COMPOSE_FILE (p. ej. compose.segmentation.yml)")
    sub.add_parser("restore")

    measure = sub.add_parser("measure")
    measure.add_argument("--label", required=True)
    measure.add_argument("--runs", type=int, default=3)
    measure.add_argument("--rate", type=int, default=0, help="req/s fijos de la fase de latencia (si no, calibra)")
    measure.add_argument("--skip-calibration", action="store_true", help="reutiliza calibration.json de la etiqueta")
    measure.add_argument("--hosts", type=int, default=20)
    measure.add_argument("--warmup", type=int, default=30, help="segundos de calentamiento sin medir")
    measure.add_argument("--api-duration", default="120s")
    measure.add_argument("--burst-rate", type=int, default=300)
    measure.add_argument("--burst-duration", default="15s")
    measure.add_argument("--sync-vus", type=int, default=200)
    measure.add_argument("--sync-duration", default="60s")
    measure.add_argument("--smoke", action="store_true", help="corrida corta de verificación del banco")

    security = sub.add_parser("security")
    security.add_argument("--label", required=True)

    sub.add_parser("up", help="recrea el stack del banco desde cero y espera a que quede sano")
    sub.add_parser("build", help="construye las imágenes del commit fijado con --image-tag")

    segmentation = sub.add_parser("segmentation", help="escenario 3: alcanzabilidad de almacenes desde web e identity-api")
    segmentation.add_argument("--label", required=True)

    args = parser.parse_args()
    ctx = Ctx(args)
    {"prepare": cmd_prepare, "restore": cmd_restore, "measure": cmd_measure, "security": cmd_security,
     "segmentation": cmd_segmentation, "build": cmd_build, "up": cmd_up}[args.command](ctx, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
