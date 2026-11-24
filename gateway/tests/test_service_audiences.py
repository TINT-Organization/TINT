"""Escenario 4 (ADR 0030): audiencias de los tickets en Compose.

Transición: cada servicio que verifica tickets recibe JWT_LEGACY_AUDIENCE, así los PRs de
identity-api y de cada servicio se pueden mergear en cualquier orden sin romper la web actual.
"""

from pathlib import Path
import unittest

from test_network_segmentation import load

ROOT = Path(__file__).parents[2]
VERIFIERS = ("identity-api", "sync-core", "social-core", "voice-core", "stream-core", "analytics")


class ServiceAudienceTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = load(ROOT / "docker-compose.yml")["services"]
        cls.env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    def test_every_verifier_receives_the_legacy_audience(self):
        for name in VERIFIERS:
            with self.subTest(service=name):
                env = self.services[name]["environment"]
                self.assertEqual(env["JWT_LEGACY_AUDIENCE"], "${JWT_LEGACY_AUDIENCE:-tint:room}")

    def test_every_verifier_has_its_own_audience(self):
        expected = {
            "identity-api": "tint:api",
            "sync-core": "tint:sync",
            "social-core": "tint:social",
            "voice-core": "tint:voice",
            "stream-core": "tint:stream",
            "analytics": "tint:analytics",
        }
        for name, audience in expected.items():
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["environment"]["JWT_AUDIENCE"], audience)

    def test_identity_api_receives_the_service_ticket_ttl(self):
        env = self.services["identity-api"]["environment"]
        self.assertEqual(env["SERVICE_TICKET_TTL_SECONDS"], "${SERVICE_TICKET_TTL_SECONDS:-600}")

    def test_new_variables_are_documented_in_env_example(self):
        self.assertIn("JWT_LEGACY_AUDIENCE=tint:room", self.env_example)
        self.assertIn("SERVICE_TICKET_TTL_SECONDS=600", self.env_example)


if __name__ == "__main__":
    unittest.main()
