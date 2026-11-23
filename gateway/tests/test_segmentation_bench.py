"""Banco de medición del escenario 3 (segmentación de red): comprobaciones desde dentro de los contenedores."""

import base64
from pathlib import Path
import re
import sys
import unittest

import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "bench"))

import run  # noqa: E402
import segmentation_checks as seg  # noqa: E402


class ComposeTagLoader(yaml.SafeLoader):
    """Entiende `!reset` y `!override` de Compose."""


ComposeTagLoader.add_multi_constructor(
    "!",
    lambda ld, _s, node: ld.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else ld.construct_mapping(node)
    if isinstance(node, yaml.MappingNode)
    else ld.construct_scalar(node),
)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeTagLoader)


class ClassifyTests(unittest.TestCase):
    def test_maps_node_error_codes_to_outcomes(self):
        self.assertEqual(seg.classify("ENOTFOUND"), "dns")
        self.assertEqual(seg.classify("EAI_AGAIN"), "dns")
        self.assertEqual(seg.classify("ECONNREFUSED"), "refused")
        self.assertEqual(seg.classify("ETIMEDOUT"), "timeout")
        self.assertEqual(seg.classify("timeout"), "timeout")
        self.assertEqual(seg.classify("EHOSTUNREACH"), "error")


class ProbeParsingTests(unittest.TestCase):
    def test_reads_the_last_json_line_and_ignores_noise(self):
        out = 'aviso de node\n{"ok":true,"result":"connected","ms":3}\n'
        self.assertEqual(seg.parse_json_line(out), {"ok": True, "result": "connected", "ms": 3})

    def test_unparseable_output_is_an_error_not_a_crash(self):
        parsed = seg.parse_json_line("OCI runtime exec failed")
        self.assertEqual(parsed["result"], "error")
        self.assertIn("OCI", parsed["detail"])


class PercentileTests(unittest.TestCase):
    def test_summary_of_samples(self):
        samples = [float(n) for n in range(1, 101)]
        summary = seg.summarize_samples(samples)
        self.assertEqual(summary["n"], 100)
        self.assertAlmostEqual(summary["p50"], 50.5)
        self.assertAlmostEqual(summary["p95"], 95.05)
        self.assertEqual(summary["max"], 100.0)

    def test_empty_samples_do_not_crash(self):
        self.assertEqual(seg.summarize_samples([]), {"n": 0})


class CommandTests(unittest.TestCase):
    def test_js_travels_base64_so_cmd_exe_quoting_cannot_break_it(self):
        cmd = seg.exec_command("web", "console.log('a' && \"b\" | c > d)")
        self.assertRegex(cmd, r"^exec -T web node -e \"[^\"&|<>^%]*\"$")
        payload = re.search(r"from\('([A-Za-z0-9+/=]+)'", cmd).group(1)
        self.assertEqual(base64.b64decode(payload).decode(), "console.log('a' && \"b\" | c > d)")


class RunChecksTests(unittest.TestCase):
    def fake_runner(self, reach):
        def runner(service, js):
            if "PING" in js:
                return '{"result":"ok","samples":[0.2,0.3,0.4,0.5]}'
            port = re.search(r"const PORT=(\d+)", js).group(1)
            host = re.search(r"const HOST='([^']+)'", js).group(1)
            outcome = reach.get((service, host), "connected")
            return f'{{"result":"{outcome}","ms":2,"host":"{host}","port":{port}}}'

        return runner

    def test_matrix_covers_both_clients_and_the_four_stores(self):
        result = seg.run_checks(self.fake_runner({}))
        self.assertEqual(set(result["reachability"]), {"web", "identity-api"})
        for client in result["reachability"].values():
            self.assertEqual(set(client), {"postgres", "redis", "mongodb", "minio"})

    def test_summary_flags_stores_reachable_from_web(self):
        before = seg.run_checks(self.fake_runner({}))
        self.assertTrue(before["summary"]["web_reaches_a_store"])
        self.assertTrue(before["summary"]["identity_api_reaches_all_stores"])

        reach = {("web", s): "dns" for s in ("postgres", "redis", "mongodb", "minio")}
        after = seg.run_checks(self.fake_runner(reach))
        self.assertFalse(after["summary"]["web_reaches_a_store"])
        self.assertTrue(after["summary"]["identity_api_reaches_all_stores"])

    def test_records_redis_ping_latency_from_the_api(self):
        result = seg.run_checks(self.fake_runner({}))
        self.assertEqual(result["redis_ping_ms_from_identity_api"]["n"], 4)

    def test_probe_script_targets_the_requested_host_and_port(self):
        js = seg.probe_js("postgres", 5432)
        self.assertIn("const HOST='postgres'", js)
        self.assertIn("const PORT=5432", js)


class BenchWiringTests(unittest.TestCase):
    def test_run_exposes_the_segmentation_command_and_extra_compose(self):
        text = (ROOT / "bench" / "run.py").read_text(encoding="utf-8")
        self.assertIn("cmd_segmentation", text)
        self.assertIn("--extra-compose", text)

    def test_setup_script_accepts_an_extra_compose_file(self):
        text = (ROOT / "bench" / "remote" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("ExtraCompose", text)

    def test_run_exposes_a_build_command_for_tagged_images(self):
        text = (ROOT / "bench" / "run.py").read_text(encoding="utf-8")
        self.assertIn("cmd_build", text)
        self.assertIn("cmd_up", text)

    def test_compose_file_value_appends_the_extra_override(self):
        self.assertEqual(run.compose_file_value(r"C:\b", ""), r"docker-compose.yml;C:\b\compose.bench.yml")
        self.assertEqual(
            run.compose_file_value(r"C:\b", "compose.segmentation.yml"),
            r"docker-compose.yml;C:\b\compose.bench.yml;C:\b\compose.segmentation.yml",
        )


class OverrideFilterTests(unittest.TestCase):
    SAMPLE = (
        "# cabecera\nservices:\n  gateway:\n    container_name: !reset null\n"
        "  audit-log:\n    container_name: !reset null\n    image: x:${TAG:-latest}\n"
        "  redis:\n    container_name: !reset null\n"
    )

    def test_service_names_of_a_compose_file(self):
        compose = "x-env: &e\n  a: 1\n\nservices:\n  env-check:\n    image: a\n  redis:\n    image: b\n\nvolumes:\n  v:\n"
        self.assertEqual(run.compose_services(compose), ["env-check", "redis"])

    def test_drops_services_the_target_commit_does_not_have(self):
        kept = run.filter_bench_override(self.SAMPLE, ["gateway", "redis"])
        self.assertIn("  gateway:", kept)
        self.assertIn("  redis:", kept)
        self.assertNotIn("audit-log", kept)
        self.assertNotIn("image: x", kept)
        self.assertTrue(kept.startswith("# cabecera"))

    def test_keeps_everything_when_the_commit_has_every_service(self):
        self.assertEqual(run.filter_bench_override(self.SAMPLE, ["gateway", "audit-log", "redis"]), self.SAMPLE)

    def test_the_real_override_filters_down_to_the_services_of_5767874(self):
        import subprocess

        shown = subprocess.run(["git", "show", "5767874:docker-compose.yml"], cwd=ROOT, capture_output=True, text=True)
        if shown.returncode != 0:
            self.skipTest("5767874 no está en este clon")
        services = run.compose_services(shown.stdout)
        text = (ROOT / "bench" / "compose.bench.yml").read_text(encoding="utf-8")
        kept = run.filter_bench_override(text, services)
        self.assertNotIn("audit-log", kept)
        self.assertEqual(run.compose_services(kept), [s for s in run.compose_services(text) if s in services])


class SegmentationOverrideTests(unittest.TestCase):
    def setUp(self):
        self.override = load(ROOT / "bench" / "compose.segmentation.yml")
        self.production = load(ROOT / "docker-compose.production.yml")

    def test_data_network_is_internal_like_production(self):
        self.assertIs(self.override["networks"]["data"]["internal"], True)

    def test_stores_stop_publishing_ports_like_production(self):
        for name in ("postgres", "redis", "mongodb"):
            self.assertEqual(self.override["services"][name]["ports"], [], name)
            self.assertEqual(self.production["services"][name]["ports"], [], name)

    def test_minio_and_stream_core_keep_the_same_exceptions_as_production(self):
        for name in ("minio", "stream-core"):
            self.assertEqual(self.override["services"][name]["ports"], self.production["services"][name]["ports"], name)

    def test_override_does_not_force_production_mode(self):
        """NODE_ENV=production impide usar el emulador de Firebase con el que el banco fabrica tokens."""
        for name, service in self.override["services"].items():
            self.assertNotIn("environment", service, name)


if __name__ == "__main__":
    unittest.main()
