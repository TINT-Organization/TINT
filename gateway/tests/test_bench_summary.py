"""Pruebas del banco de medición (bench/): estadística de corridas y override de Compose."""

from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "bench"))

import security_checks  # noqa: E402
import summarize  # noqa: E402


class AggregateTests(unittest.TestCase):
    def test_median_of_three_runs_with_min_and_max(self):
        agg = summarize.aggregate([{"p95": 120.0}, {"p95": 100.0}, {"p95": 140.0}])

        self.assertEqual(agg["p95"], {"median": 120.0, "min": 100.0, "max": 140.0})

    def test_metric_missing_in_a_run_is_ignored(self):
        agg = summarize.aggregate([{"p95": 10.0, "rps": 5.0}, {"p95": 14.0}])

        self.assertEqual(agg["p95"]["median"], 12.0)
        self.assertEqual(agg["rps"]["median"], 5.0)

    def test_difference_larger_than_both_ranges_is_conclusive(self):
        before = {"median": 100.0, "min": 95.0, "max": 105.0}
        after = {"median": 150.0, "min": 148.0, "max": 152.0}

        self.assertTrue(summarize.is_conclusive(before, after))

    def test_difference_inside_the_run_to_run_range_is_not_conclusive(self):
        before = {"median": 100.0, "min": 80.0, "max": 120.0}
        after = {"median": 110.0, "min": 100.0, "max": 125.0}

        self.assertFalse(summarize.is_conclusive(before, after))


class K6SummaryTests(unittest.TestCase):
    def test_reads_flat_summary_export(self):
        data = {"metrics": {"http_req_duration": {"med": 4.0, "p(95)": 9.0, "p(99)": 20.0},
                            "http_reqs": {"count": 100, "rate": 50.0}}}

        flat = summarize.k6_values(data)

        self.assertEqual(flat["http_req_duration.p95"], 9.0)
        self.assertEqual(flat["http_req_duration.p50"], 4.0)
        self.assertEqual(flat["http_reqs.rate"], 50.0)

    def test_reads_summary_export_with_values_nesting(self):
        data = {"metrics": {"ws_connecting": {"type": "trend", "values": {"p(95)": 30.0}}}}

        self.assertEqual(summarize.k6_values(data)["ws_connecting.p95"], 30.0)


def _level_export(levels: dict[int, tuple[float, float, float, int]]) -> dict:
    """Export de k6 con submétricas por nivel: {vus: (avg, p95, tasa de fallos, peticiones)}."""
    metrics = {"http_req_duration": {"avg": 1.0, "p(95)": 1.0}}
    for vus, (avg, p95, failed, count) in levels.items():
        metrics[f"http_req_duration{{level:{vus}}}"] = {"avg": avg, "med": avg, "p(95)": p95, "p(99)": p95, "thresholds": {}}
        metrics[f"http_req_failed{{level:{vus}}}"] = {"value": failed, "passes": 0, "fails": count}
        metrics[f"http_reqs{{level:{vus}}}"] = {"count": count, "rate": 1.0}
    return {"metrics": metrics}


class LevelCurveTests(unittest.TestCase):
    def test_extracts_one_point_per_vu_level_sorted_by_level(self):
        data = _level_export({50: (12.0, 20.0, 0.0, 1500), 1: (9.0, 11.0, 0.0, 30)})

        curve = summarize.level_curve(data, hold_s=30)

        self.assertEqual([p["vus"] for p in curve], [1, 50])
        self.assertEqual(curve[1]["avg_ms"], 12.0)
        self.assertEqual(curve[1]["p95_ms"], 20.0)
        self.assertEqual(curve[1]["rps"], 50.0)
        self.assertEqual(curve[1]["failed"], 0.0)

    def test_ignores_metrics_of_other_tags(self):
        data = _level_export({1: (9.0, 11.0, 0.0, 30)})
        data["metrics"]["http_req_duration{layer:caddy}"] = {"avg": 1.0, "p(95)": 2.0}

        self.assertEqual(len(summarize.level_curve(data, hold_s=30)), 1)

    def test_curve_can_be_filtered_by_an_extra_tag(self):
        data = {"metrics": {
            "http_req_duration{layer:caddy,level:50}": {"avg": 2.0, "p(95)": 3.0},
            "http_req_duration{layer:auth,level:50}": {"avg": 9.0, "p(95)": 15.0},
        }}

        curve = summarize.level_curve(data, hold_s=30, tag="layer:auth")

        self.assertEqual(curve[0]["avg_ms"], 9.0)


class KneeTests(unittest.TestCase):
    def test_knee_is_the_last_level_before_the_first_failures(self):
        curve = [{"vus": 1, "p95_ms": 10.0, "failed": 0.0, "rps": 1.0},
                 {"vus": 200, "p95_ms": 20.0, "failed": 0.0, "rps": 190.0},
                 {"vus": 500, "p95_ms": 40.0, "failed": 0.02, "rps": 300.0}]

        knee = summarize.find_knee(curve)

        self.assertEqual(knee["knee_vus"], 200)
        self.assertEqual(knee["degraded_vus"], 500)
        self.assertIn("fallos", knee["reason"])

    def test_latency_explosion_also_marks_degradation(self):
        curve = [{"vus": 1, "p95_ms": 10.0, "failed": 0.0, "rps": 1.0},
                 {"vus": 50, "p95_ms": 30.0, "failed": 0.0, "rps": 49.0},
                 {"vus": 200, "p95_ms": 500.0, "failed": 0.0, "rps": 120.0}]

        knee = summarize.find_knee(curve, latency_factor=10)

        self.assertEqual(knee["knee_vus"], 50)
        self.assertIn("p95", knee["reason"])

    def test_cold_start_on_the_first_level_does_not_inflate_the_latency_base(self):
        curve = [{"vus": 1, "p95_ms": 120.0, "failed": 0.0, "rps": 1.0},
                 {"vus": 50, "p95_ms": 12.0, "failed": 0.0, "rps": 49.0},
                 {"vus": 200, "p95_ms": 300.0, "failed": 0.0, "rps": 150.0}]

        self.assertEqual(summarize.find_knee(curve, latency_factor=10)["degraded_vus"], 200)

    def test_no_degradation_means_knee_is_above_the_last_level(self):
        curve = [{"vus": 1, "p95_ms": 10.0, "failed": 0.0, "rps": 1.0},
                 {"vus": 50, "p95_ms": 12.0, "failed": 0.0, "rps": 49.0}]

        knee = summarize.find_knee(curve)

        self.assertEqual(knee["knee_vus"], 50)
        self.assertIsNone(knee["degraded_vus"])

    def test_curve_markdown_has_one_row_per_level_and_marks_the_knee(self):
        curve = [{"vus": 1, "avg_ms": 9.0, "p95_ms": 10.0, "p99_ms": 11.0, "failed": 0.0, "rps": 1.0, "reqs": 30},
                 {"vus": 50, "avg_ms": 12.0, "p95_ms": 20.0, "p99_ms": 25.0, "failed": 0.0, "rps": 49.0, "reqs": 1470}]

        text = summarize.curve_markdown(curve, summarize.find_knee(curve))

        self.assertEqual(text.count("\n| "), 3)  # separador y una fila por nivel
        self.assertIn("knee", text)


class Lab6ReportTests(unittest.TestCase):
    def _write_runs(self, root: Path) -> None:
        import json

        for n, p95 in ((1, 20.0), (2, 24.0), (3, 22.0)):
            run = root / f"run-{n}"
            run.mkdir()
            data = _level_export({1: (9.0, 10.0, 0.0, 30), 50: (12.0, p95, 0.0, 1500)})
            (run / "lab6-playback.json").write_text(json.dumps(data))
            (run / "cpu-portatil-playback.csv").write_text("epoch,cpu_pct\n1,20.0\n2,85.5\n")
            (run / "stats-lab6-playback.csv").write_text("tintbench-identity-api-1,40.00%,100MiB / 8GiB\n")

    def test_median_curve_per_target_across_runs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_runs(Path(tmp))

            curves = summarize.lab6_curves(Path(tmp), hold_s=30)

        self.assertEqual([p["vus"] for p in curves["playback"]], [1, 50])
        self.assertEqual(curves["playback"][1]["p95_ms"], 22.0)

    def test_report_has_knee_generator_cpu_and_container_resources(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_runs(Path(tmp))

            text = summarize.lab6_report(Path(tmp), hold_s=30, plot=False)

        self.assertIn("## playback", text)
        self.assertIn("Knee", text)
        self.assertIn("85.5", text)
        self.assertIn("identity-api", text)

    def test_gateway_target_is_split_by_layer(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-1"
            run.mkdir()
            run.joinpath("lab6-gateway.json").write_text(json.dumps({"metrics": {
                "http_req_duration{layer:caddy,level:1}": {"avg": 1.0, "p(95)": 2.0},
                "http_req_duration{layer:auth,level:1}": {"avg": 8.0, "p(95)": 12.0},
            }}))

            curves = summarize.lab6_curves(Path(tmp), hold_s=30)

        self.assertEqual(curves["gateway/caddy"][0]["p95_ms"], 2.0)
        self.assertEqual(curves["gateway/auth"][0]["avg_ms"], 8.0)


class DockerStatsTests(unittest.TestCase):
    def test_parses_one_sample_line(self):
        row = summarize.parse_stats_line("tint-gateway,12.50%,93.25MiB / 7.69GiB")

        self.assertEqual(row, ("tint-gateway", 12.5, 93.25))

    def test_converts_gib_and_kib_to_mib(self):
        self.assertEqual(summarize.parse_stats_line("a,0.00%,1GiB / 8GiB")[2], 1024.0)
        self.assertEqual(summarize.parse_stats_line("a,0.00%,512KiB / 8GiB")[2], 0.5)

    def test_strips_ansi_sequences_from_streaming_output(self):
        line = "\x1b[2J\x1b[Htint-gateway,3.00%,10MiB / 8GiB"

        self.assertEqual(summarize.parse_stats_line(line), ("tint-gateway", 3.0, 10.0))

    def test_ignores_malformed_lines(self):
        self.assertIsNone(summarize.parse_stats_line("basura"))

    def test_resource_summary_gives_mean_and_max_per_container(self):
        lines = ["gw,10.00%,100MiB / 8GiB", "gw,30.00%,140MiB / 8GiB", "db,1.00%,50MiB / 8GiB"]

        out = summarize.summarize_resources(lines)

        self.assertEqual(out["gw"], {"cpu_mean": 20.0, "cpu_max": 30.0, "mem_max_mib": 140.0})
        self.assertEqual(out["db"]["cpu_max"], 1.0)


class KeyMetricsTests(unittest.TestCase):
    def test_keeps_latency_throughput_sync_and_resource_metrics_only(self):
        keep = ["api.http_req_duration.p95", "api.http_reqs.rate", "burst.status_429.count",
                "sync.sync_broadcast_latency_ms.p99", "sync.ws_connecting.p95", "res.api.tintbench-gateway-1.cpu_max"]
        drop = ["sync.vus.max", "api.http_req_duration.min", "api.checks.passes", "sync.ws_session_duration.p95", "res.api.tintbench-coturn-1.cpu_max"]

        for name in keep:
            self.assertTrue(summarize.is_key_metric(name), name)
        for name in drop:
            self.assertFalse(summarize.is_key_metric(name), name)


class RunLoadingTests(unittest.TestCase):
    def test_loads_k6_files_and_per_phase_resource_samples(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "api.json").write_text(json.dumps({"metrics": {"http_req_duration": {"p(95)": 9.0}}}))
            (run / "burst.json").write_text(json.dumps({"metrics": {"status_429": {"count": 7}}}))
            (run / "stats-api.csv").write_text("gw,10.00%,100MiB / 8GiB\n")

            metrics = summarize.load_run(run)

        self.assertEqual(metrics["api.http_req_duration.p95"], 9.0)
        self.assertEqual(metrics["burst.status_429.count"], 7.0)
        self.assertEqual(metrics["res.api.gw.cpu_max"], 10.0)


class SecurityParsersTests(unittest.TestCase):
    def test_nmap_grepable_open_tcp_ports_only(self):
        out = "Host: 1.2.3.4 ()\tPorts: 22/open/tcp//ssh///, 5432/open/tcp//postgresql///, 8087/filtered/tcp//x///, 53/open/udp//d///"

        self.assertEqual(security_checks.parse_nmap_open_ports(out), [22, 5432])

    def test_testssl_flags_legacy_protocols(self):
        findings = [
            {"id": "TLS1_1", "finding": "offered (deprecated)"},
            {"id": "TLS1_2", "finding": "offered"},
            {"id": "TLS1_3", "finding": "offered with final"},
            {"id": "SSLv3", "finding": "not offered"},
        ]

        proto = security_checks.testssl_protocols(findings)

        self.assertEqual(proto["offered"], ["TLS1_1", "TLS1_2", "TLS1_3"])
        self.assertEqual(proto["legacy_offered"], ["TLS1_1"])

    def test_dns_shim_answers_with_the_target_ip_and_is_executable(self):
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shim = security_checks.make_host_shim(Path(tmp), "10.1.2.3")

            self.assertTrue(os.access(shim, os.X_OK))
            out = subprocess.run([str(shim), "localhost"], capture_output=True, text=True).stdout
            self.assertIn("10.1.2.3", out)

    def test_host_token_is_unsigned_emulator_token_for_the_given_project(self):
        import base64
        import json

        head, body, sig = security_checks.host_token("demo-tint").split(".")
        pad = lambda part: part + "=" * (-len(part) % 4)

        self.assertEqual(sig, "")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head)))["alg"], "none")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(body)))["aud"], "demo-tint")


class BenchComposeOverrideTests(unittest.TestCase):
    def test_override_resets_container_name_of_every_named_service(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        named = {n for n, s in compose["services"].items() if "container_name" in s}
        # Compose admite etiquetas como `!reset`; el cargador las deja pasar sin interpretarlas.
        loader = type("Loader", (yaml.SafeLoader,), {})
        loader.add_multi_constructor("!", lambda ld, suffix, node: None)
        override = yaml.load((ROOT / "bench" / "compose.bench.yml").read_text(encoding="utf-8"), Loader=loader)

        for service in named:
            self.assertIn(service, override["services"], service)
            self.assertIn("container_name", override["services"][service], service)

    def test_override_reuses_prebuilt_images_for_built_services(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        built = {n for n, s in compose["services"].items() if "build" in s}
        text = (ROOT / "bench" / "compose.bench.yml").read_text(encoding="utf-8")

        for service in built:
            self.assertIn(f"image: tint-monorepo-{service}:${{BENCH_IMAGE_TAG:-latest}}", text, service)



class BenchLoadScriptTests(unittest.TestCase):
    def test_burst_phase_comes_from_a_single_client(self):
        # Escenario 2: el estímulo es "un cliente" que manda una ráfaga. Con un token al azar entre 20,
        # cada uno haría ~15 req/s y el límite por usuario no se vería (ADR 0028).
        script = (ROOT / "bench" / "k6" / "api.js").read_text(encoding="utf-8")

        self.assertIn("PHASE === 'burst' ? data.seeded[0]", script)

if __name__ == "__main__":
    unittest.main()
