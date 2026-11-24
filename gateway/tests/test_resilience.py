"""Resiliencia del despliegue (Prototipo 3, ADR 0032).

- identity-api es el cuello de botella medido en el Laboratorio 6 (un solo proceso Node al
  108-128 % de CPU, del que depende todo /api y /ws por forward_auth): corre con réplicas y Caddy
  reparte la carga, las sondea y las saca de rotación si fallan.
- Cada servicio de larga vida se reinicia solo, tiene healthcheck y un tope de memoria, y arranca
  después de que sus dependencias estén sanas.
"""

from pathlib import Path
import re
import unittest

from test_network_segmentation import load

ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "docker-compose.yml"
CADDYFILE = ROOT / "gateway" / "Caddyfile"
ADR = ROOT / "docs" / "adr" / "0032-resiliencia-replicas-y-salud.md"
ADR_INDEX = ROOT / "docs" / "adr" / "README.md"

ONE_SHOT = {"env-check", "minio-init", "mongo-init", "identity-migrate"}
STORES = {"postgres", "redis", "mongodb", "minio"}


class IdentityReplicasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = load(COMPOSE)["services"]["identity-api"]

    def test_identity_api_runs_with_replicas(self):
        self.assertEqual(str(self.identity["deploy"]["replicas"]), "${IDENTITY_API_REPLICAS:-2}")

    def test_replicas_cannot_have_a_fixed_name_or_host_port(self):
        self.assertNotIn("container_name", self.identity)
        self.assertNotIn("ports", self.identity)


class MigrationsRunOnceTests(unittest.TestCase):
    """Con réplicas, cada una corría `npm run migrate` al arrancar y en un volumen vacío chocaban
    (una quedaba unhealthy y `up` fallaba; medido 1 de cada 18 arranques). Migra un contenedor
    de una sola vez y las réplicas solo arrancan."""

    @classmethod
    def setUpClass(cls):
        cls.services = load(COMPOSE)["services"]

    def test_a_one_shot_container_migrates_with_the_identity_image(self):
        migrate = self.services["identity-migrate"]
        self.assertEqual(migrate["build"], self.services["identity-api"]["build"])
        self.assertIn("npm run migrate", " ".join(migrate["command"]))
        self.assertEqual(migrate["restart"], "no")
        self.assertEqual(migrate["depends_on"]["postgres"]["condition"], "service_healthy")

    def test_replicas_wait_for_the_migration_and_do_not_migrate(self):
        identity = self.services["identity-api"]
        self.assertEqual(identity["depends_on"]["identity-migrate"]["condition"], "service_completed_successfully")
        self.assertNotIn("migrate", " ".join(identity["command"]))


class CaddyLoadBalancingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caddyfile = CADDYFILE.read_text(encoding="utf-8")
        start = cls.caddyfile.index("(identity_upstream) {")
        cls.snippet = cls.caddyfile[start: cls.caddyfile.index("\n}\n", start)]

    def test_identity_upstream_discovers_every_replica_by_dns(self):
        self.assertRegex(self.snippet, r"dynamic a \{\$IDENTITY_API_HOST:identity-api\} \{\$IDENTITY_API_PORT:8081\}")

    def test_identity_upstream_balances_and_retries_on_another_replica(self):
        self.assertIn("lb_policy least_conn", self.snippet)
        self.assertRegex(self.snippet, r"lb_try_duration \d+s")

    def test_identity_upstream_has_passive_health_checks(self):
        # Caddy no corre chequeos activos sobre upstreams dinámicos: los pasivos sacan de rotación
        # la réplica que falla y lb_try_duration reintenta en otra (la salud activa la da Compose).
        self.assertRegex(self.snippet, r"fail_duration \d+s")
        self.assertRegex(self.snippet, r"max_fails \d+")
        self.assertIn("unhealthy_status 5xx", self.snippet)

    def test_forward_auth_and_identity_routes_use_the_balanced_upstream(self):
        forward_auth = self.caddyfile[self.caddyfile.index("forward_auth @protected"):]
        forward_auth = forward_auth[: forward_auth.index("}") + 1]
        self.assertIn("import identity_upstream", forward_auth)
        identity_route = self.caddyfile[self.caddyfile.index("reverse_proxy @identity"):]
        identity_route = identity_route[: identity_route.index("}") + 1]
        self.assertIn("import identity_upstream", identity_route)

    def test_every_upstream_fails_fast_when_down(self):
        timeout = self.caddyfile[self.caddyfile.index("(service_timeout) {"):]
        timeout = timeout[: timeout.index("\n}\n")]
        self.assertIn("dial_timeout 5s", timeout)
        self.assertRegex(timeout, r"fail_duration \d+s")


class ServiceHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = load(COMPOSE)["services"]
        cls.long_running = {n: s for n, s in cls.services.items() if n not in ONE_SHOT}

    def test_every_long_running_service_restarts_on_failure(self):
        for name, service in self.long_running.items():
            with self.subTest(service=name):
                self.assertEqual(service.get("restart"), "unless-stopped")

    def test_every_long_running_service_has_a_healthcheck(self):
        for name, service in self.long_running.items():
            with self.subTest(service=name):
                self.assertIn("healthcheck", service)

    def test_every_long_running_service_has_a_memory_cap(self):
        for name, service in self.long_running.items():
            with self.subTest(service=name):
                memory = service["deploy"]["resources"]["limits"]["memory"]
                self.assertRegex(str(memory), r"^\d+[MG]$")

    def test_services_wait_for_healthy_stores(self):
        for name, service in self.long_running.items():
            depends = service.get("depends_on") or {}
            for store in STORES & set(depends):
                with self.subTest(service=name, store=store):
                    self.assertEqual(depends[store]["condition"], "service_healthy")

    def test_gateway_starts_after_a_healthy_identity_api(self):
        """Sin identity-api no hay forward_auth: el gateway espera a que alguna réplica esté sana."""
        depends = self.services["gateway"]["depends_on"]
        self.assertEqual(depends["identity-api"]["condition"], "service_healthy")


class ResilienceDocsTests(unittest.TestCase):
    def test_adr_0032_is_written_and_indexed(self):
        text = ADR.read_text(encoding="utf-8")
        for fragment in ("réplicas", "least_conn", "fail_duration", "unless-stopped", "Laboratorio 6"):
            self.assertIn(fragment, text)
        self.assertIn("0032-resiliencia-replicas-y-salud.md", ADR_INDEX.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
