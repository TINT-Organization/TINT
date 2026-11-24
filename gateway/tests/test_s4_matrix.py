"""Pruebas de la lógica pura de bench/s4_matrix.py, bench/failover.py y el .env local de bench/run.py."""

from pathlib import Path
import base64
import hashlib
import hmac
import json
import struct
import sys
import unittest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "bench"))

import evolution  # noqa: E402
import failover  # noqa: E402
import run  # noqa: E402
import s4_matrix  # noqa: E402


def close_frame(code: int) -> bytes:
    payload = struct.pack(">H", code) + b"bye"
    return bytes([0x88, len(payload)]) + payload


class FrameTests(unittest.TestCase):
    def test_parses_a_text_frame_and_a_close_frame(self):
        text = bytes([0x81, 2]) + b"hi"

        frames, rest = s4_matrix.parse_frames(text + close_frame(4003))

        self.assertEqual(frames[0], (0x1, b"hi"))
        self.assertEqual(frames[1][0], 0x8)
        self.assertEqual(s4_matrix.close_code(frames[1][1]), 4003)
        self.assertEqual(rest, b"")

    def test_incomplete_frame_stays_in_the_buffer(self):
        frames, rest = s4_matrix.parse_frames(bytes([0x81, 5]) + b"ab")

        self.assertEqual(frames, [])
        self.assertEqual(rest, bytes([0x81, 5]) + b"ab")

    def test_extended_16_bit_length(self):
        payload = b"x" * 300

        frames, _ = s4_matrix.parse_frames(bytes([0x82, 126]) + struct.pack(">H", 300) + payload)

        self.assertEqual(frames, [(0x2, payload)])

    def test_close_without_code_is_1005(self):
        self.assertEqual(s4_matrix.close_code(b""), 1005)


class ClassifyTests(unittest.TestCase):
    def test_http(self):
        self.assertEqual(s4_matrix.classify_http(401), "rechazado")
        self.assertEqual(s4_matrix.classify_http(403), "rechazado")
        self.assertEqual(s4_matrix.classify_http(200), "aceptado")
        # WHEP en una sala sin transmisión: pasó la autenticación y respondió 404.
        self.assertEqual(s4_matrix.classify_http(404), "aceptado")
        self.assertEqual(s4_matrix.classify_http(503), "error")
        self.assertEqual(s4_matrix.classify_http(429), "error")
        self.assertEqual(s4_matrix.classify_http(0), "error")

    def test_websocket(self):
        self.assertEqual(s4_matrix.classify_ws(101, None), "aceptado")
        self.assertEqual(s4_matrix.classify_ws(101, 4001), "rechazado")
        self.assertEqual(s4_matrix.classify_ws(101, 4003), "rechazado")
        self.assertEqual(s4_matrix.classify_ws(101, 4009), "cerrado")
        self.assertEqual(s4_matrix.classify_ws(401, None), "rechazado")


class CrossedSummaryTests(unittest.TestCase):
    def test_counts_only_tickets_of_another_service(self):
        acc, rej = {"resultado": "aceptado"}, {"resultado": "rechazado"}
        matrix = {
            "ticket:sync": {"sync": acc, "social": rej, "analytics": rej},
            "ticket:analytics": {"sync": acc, "analytics": acc},
            "sesion-invitado": {"sync": acc},  # sin dueño: no cuenta
        }

        summary = s4_matrix.crossed_summary(matrix)

        self.assertEqual(summary["cruzados"], 3)
        self.assertEqual(summary["cruzados_rechazados"], 2)
        self.assertEqual(summary["pct_cruzados_rechazados"], 66.7)
        self.assertEqual((summary["propios"], summary["propios_aceptados"]), (2, 2))

    def test_without_crossed_cells_the_percentage_is_none(self):
        self.assertIsNone(s4_matrix.crossed_summary({})["pct_cruzados_rechazados"])


class TokenTests(unittest.TestCase):
    def test_hs256_signature_verifies_with_the_secret(self):
        token = s4_matrix.sign_hs256({"aud": "tint:room", "sub": "x"}, "secreto")
        head, body, sig = token.split(".")
        expected = hmac.new(b"secreto", f"{head}.{body}".encode(), hashlib.sha256).digest()

        self.assertEqual(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), expected)
        self.assertEqual(s4_matrix.jwt_claims(token), {"aud": "tint:room", "sub": "x"})

    def test_emulator_token_is_alg_none(self):
        head = s4_matrix.emulator_token("h").split(".")[0]

        self.assertEqual(json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))["alg"], "none")

    def test_percentile_interpolates(self):
        self.assertEqual(s4_matrix.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(s4_matrix.percentile(list(range(1, 101)), 95), 95.05)


class FailoverTests(unittest.TestCase):
    def test_probe_summary_window_and_recovery(self):
        samples = [(0.0, 200), (3.1, 502), (3.2, 0), (3.5, 200), (4.0, 200)]

        out = failover.summarize_probes(samples, kill_at=3.0)

        self.assertEqual((out["ok"], out["fallidas"]), (3, 2))
        self.assertEqual(out["primer_fallo_s"], 0.1)
        self.assertEqual(out["ultimo_fallo_s"], 0.2)
        self.assertEqual(out["recupera_s"], 0.5)
        self.assertEqual(out["estados"], {"200": 3, "502": 1, "0": 1})

    def test_no_failures(self):
        out = failover.summarize_probes([(0.0, 200), (1.0, 200)], kill_at=0.5)

        self.assertEqual(out["fallidas"], 0)
        self.assertNotIn("recupera_s", out)

    def test_failures_per_second_from_the_kill(self):
        rows = [(100.2, False), (100.9, True), (101.5, False), (99.5, False)]

        self.assertEqual(failover.failures_per_second(rows, start=100.0), [
            {"s": -1, "total": 1, "fallidas": 0}, {"s": 0, "total": 2, "fallidas": 1}, {"s": 1, "total": 1, "fallidas": 0}])


class UnbuiltImageTests(unittest.TestCase):
    COMPOSE = "services:\n  gateway:\n    image: caddy:2.9-alpine\n  web:\n    build:\n      context: ./apps/web\nvolumes:\n  x:\n"

    def test_built_services(self):
        self.assertEqual(run.built_services(self.COMPOSE), ["web"])

    def test_override_keeps_image_only_for_built_services(self):
        override = ("services:\n  gateway:\n    container_name: !reset null\n    image: tint-monorepo-gateway:x\n"
                    "  web:\n    container_name: !reset null\n    image: tint-monorepo-web:x\n")

        out = run.drop_unbuilt_images(override, ["web"])

        self.assertNotIn("tint-monorepo-gateway", out)
        self.assertIn("tint-monorepo-web:x", out)
        self.assertEqual(out.count("container_name: !reset null"), 2)


class EvolutionTests(unittest.TestCase):
    def test_replicas_are_summed_per_service_and_phase(self):
        run = {"res.api.tintevo-identity-api-1.cpu_mean": 40.0, "res.api.tintevo-identity-api-2.cpu_mean": 35.5,
               "res.api.tintevo-gateway-1.cpu_mean": 20.0, "res.api.tintevo-identity-api-1.mem_max_mib": 100.0,
               "res.api.tintevo-identity-api-2.mem_max_mib": 90.0, "api.http_req_duration.p95": 12.0}

        out = evolution.derived(run)

        self.assertEqual(out["cpu.api.identity-api"], 75.5)
        self.assertEqual(out["cpu.api.gateway"], 20.0)
        self.assertEqual(out["mem.api.identity-api"], 190.0)
        self.assertEqual(out["api.http_req_duration.p95"], 12.0)

    def test_format_median_and_range(self):
        self.assertEqual(evolution.fmt({"median": 12.34, "min": 11.0, "max": 13.0}, 1), "12.3 (11.0–13.0)")
        self.assertEqual(evolution.fmt({"median": 0.001, "min": 0.0, "max": 0.002}, 100), "0.10 (0.00–0.20)")
        self.assertEqual(evolution.fmt(None, 1), "pendiente")


class LocalEnvTests(unittest.TestCase):
    def test_overrides_replace_keys_and_keep_the_rest(self):
        example = "# comentario\nJWT_SECRET=dev-secret\nPOSTGRES_USER=tint\nJWT_SECRET_OTHER=x\n"

        env = run.local_env(example, {"JWT_SECRET": "abc", "GATEWAY_HTTPS_PORT": "8443"})

        self.assertIn("POSTGRES_USER=tint", env)
        self.assertIn("JWT_SECRET_OTHER=x", env)
        self.assertNotIn("dev-secret", env)
        self.assertEqual(env.count("JWT_SECRET="), 1)
        self.assertTrue(env.endswith("JWT_SECRET=abc\nGATEWAY_HTTPS_PORT=8443\n"))


if __name__ == "__main__":
    unittest.main()
