"""Segmentación de red (Prototipo 3, escenario 3, ADR 0027): las redes de Compose siguen las capas.

Capas de la vista en capas → redes de Docker:

- Presentación y Acceso → `edge` (gateway, web, emulador de login local).
- Servicios            → `app` (APIs y núcleos; el gateway los alcanza por aquí).
- Datos                → `data` (PostgreSQL, Redis, MongoDB, MinIO); en producción `internal: true`.
- Medios               → `media` (MinIO y gateway), para que el navegador llegue a las URLs firmadas.

coturn usa la red del host y env-check no necesita red.
"""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "docker-compose.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
ARQUITECTURA = ROOT / "docs" / "arquitectura.md"
ADR = ROOT / "docs" / "adr" / "0027-segmentacion-de-red-por-capas.md"
ADR_INDEX = ROOT / "docs" / "adr" / "README.md"

STORES = ("postgres", "redis", "mongodb", "minio")
DATA_HELPERS = ("minio-init", "mongo-init")
# Servicios que hablan con un almacén (y por eso viven también en `data`).
DATA_CLIENTS = (
    "identity-api",
    "media-worker",
    "analytics",
    "audit-log",
    "social-core",
    "sync-core",
    "voice-core",
    "stream-core",
)
# Nombres con los que las variables de entorno apuntan a cada almacén.
STORE_HOSTS = ("postgres:", "redis:", "mongodb:", "minio:")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader que entiende las etiquetas de Compose (!reset, !override)."""


def _compose_tag(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _compose_tag)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


def networks_of(service: dict) -> set:
    nets = service.get("networks") or []
    return set(nets.keys() if isinstance(nets, dict) else nets)


def env_values(service: dict) -> str:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return " ".join(env)
    return " ".join(str(v) for v in env.values())


class DeclaredNetworksTests(unittest.TestCase):
    def setUp(self):
        self.compose = load(COMPOSE)
        self.services = self.compose["services"]

    def test_four_networks_by_layer_are_declared(self):
        self.assertEqual(set(self.compose["networks"]), {"edge", "app", "data", "media"})

    def test_every_service_picks_its_networks_explicitly(self):
        """Sin `networks:` un servicio caería en la red `default` y vería a todos los demás."""
        for name, service in self.services.items():
            with self.subTest(service=name):
                if name == "coturn":
                    self.assertEqual(service.get("network_mode"), "host")
                elif name == "env-check":
                    self.assertEqual(service.get("network_mode"), "none")
                else:
                    self.assertTrue(networks_of(service), f"{name} no declara redes")

    def test_data_network_is_private_only_in_production(self):
        """En desarrollo se publican puertos de depuración; una red `internal` no los admite."""
        self.assertFalse((self.compose["networks"]["data"] or {}).get("internal", False))
        production = load(PRODUCTION_COMPOSE)
        self.assertIs(production["networks"]["data"]["internal"], True)


class LayerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.services = load(COMPOSE)["services"]

    def test_stores_live_in_data_and_never_in_edge_or_app(self):
        for name in STORES + DATA_HELPERS:
            with self.subTest(service=name):
                nets = networks_of(self.services[name])
                self.assertIn("data", nets)
                self.assertFalse(nets & {"edge", "app"}, f"{name} alcanzable desde edge o app")

    def test_minio_also_joins_media_so_the_browser_reaches_signed_urls(self):
        self.assertEqual(networks_of(self.services["minio"]), {"data", "media"})

    def test_presentation_and_access_layers_cannot_reach_the_stores(self):
        for name in ("web", "gateway", "firebase-auth-emulator"):
            with self.subTest(service=name):
                self.assertNotIn("data", networks_of(self.services[name]))

    def test_only_services_that_use_a_store_join_data(self):
        on_data = {n for n, s in self.services.items() if "data" in networks_of(s)}
        self.assertEqual(on_data, set(STORES + DATA_HELPERS + DATA_CLIENTS))
        for name in DATA_CLIENTS:
            with self.subTest(service=name):
                self.assertTrue(
                    any(host in env_values(self.services[name]) for host in STORE_HOSTS),
                    f"{name} está en data pero no usa ningún almacén",
                )

    def test_audit_log_and_media_worker_are_not_reachable_from_the_gateway(self):
        """Sin API pública ni ruta en el gateway: solo viven en `data`."""
        for name in ("audit-log", "media-worker"):
            with self.subTest(service=name):
                self.assertEqual(networks_of(self.services[name]), {"data"})

    def test_edge_holds_only_the_entry_points(self):
        on_edge = {n for n, s in self.services.items() if "edge" in networks_of(s)}
        self.assertEqual(on_edge, {"gateway", "web", "firebase-auth-emulator"})


class ReachabilityTests(unittest.TestCase):
    """Segmentar no puede romper el sistema: cada llamada legítima sigue teniendo una red común."""

    def setUp(self):
        self.services = load(COMPOSE)["services"]

    def shares_network(self, a: str, b: str) -> bool:
        return bool(networks_of(self.services[a]) & networks_of(self.services[b]))

    def test_every_client_shares_a_network_with_each_store_it_uses(self):
        host_to_service = {"postgres:": "postgres", "redis:": "redis", "mongodb:": "mongodb", "minio:": "minio"}
        for name in DATA_CLIENTS:
            env = env_values(self.services[name])
            for host, store in host_to_service.items():
                if host in env:
                    with self.subTest(service=name, store=store):
                        self.assertTrue(self.shares_network(name, store))
        for helper, store in (("minio-init", "minio"), ("mongo-init", "mongodb")):
            with self.subTest(service=helper):
                self.assertTrue(self.shares_network(helper, store))

    def test_gateway_reaches_every_upstream(self):
        for upstream in ("web", "identity-api", "sync-core", "social-core", "analytics", "voice-core", "stream-core"):
            with self.subTest(upstream=upstream):
                self.assertTrue(self.shares_network("gateway", upstream))

    def test_web_ssr_reaches_identity_api(self):
        """Salto de capa S1 de la vista en capas: el SSR llama a identity-api sin pasar por el gateway."""
        self.assertTrue(self.shares_network("web", "identity-api"))

    def test_identity_api_reaches_the_cores_it_coordinates(self):
        for core in ("sync-core", "social-core", "analytics", "stream-core"):
            with self.subTest(core=core):
                self.assertTrue(self.shares_network("identity-api", core))

    def test_stream_core_reaches_identity_api_for_room_closing(self):
        self.assertTrue(self.shares_network("stream-core", "identity-api"))


class PublishedPortsTests(unittest.TestCase):
    def setUp(self):
        self.base = load(COMPOSE)["services"]
        self.production = load(PRODUCTION_COMPOSE)["services"]

    def test_development_binds_store_ports_to_loopback_only(self):
        """Depurar desde el portátil sigue funcionando, pero la LAN ya no ve las bases de datos."""
        for name in ("postgres", "redis", "mongodb"):
            with self.subTest(service=name):
                for port in self.base[name]["ports"]:
                    self.assertTrue(str(port).startswith("127.0.0.1:"), f"{name} publica {port}")

    def test_development_publishes_minio_console_on_loopback_and_api_for_the_browser(self):
        ports = [str(p) for p in self.base["minio"]["ports"]]
        self.assertIn("127.0.0.1:9001:9001", ports)
        self.assertIn("9000:9000", ports)

    def test_production_stops_publishing_the_stores(self):
        production = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
        for name in ("postgres", "redis", "mongodb"):
            with self.subTest(service=name):
                self.assertIn(f"  {name}:\n    ports: !reset []", production)

    def test_production_minio_keeps_only_the_signed_url_api(self):
        """Excepción documentada hasta que el escenario 1 publique MinIO por el gateway."""
        self.assertEqual(self.production["minio"]["ports"], ["9000:9000"])

    def test_production_stream_core_keeps_only_its_udp_media_port(self):
        ports = self.production["stream-core"]["ports"]
        self.assertEqual(len(ports), 1)
        self.assertTrue(str(ports[0]).endswith("/udp"))

    def test_production_publishes_nothing_else_besides_the_gateway_and_exceptions(self):
        effective = {}
        for name, service in self.base.items():
            ports = service.get("ports", [])
            if name in self.production and "ports" in self.production[name]:
                ports = self.production[name]["ports"]
            effective[name] = [str(p) for p in ports]
        published = {n: p for n, p in effective.items() if p}
        self.assertEqual(set(published), {"gateway", "minio", "stream-core"})


class DocumentationTests(unittest.TestCase):
    def test_adr_0027_is_written_and_indexed(self):
        text = ADR.read_text(encoding="utf-8")
        for fragment in ("edge", "app", "data", "media", "coturn", "internal: true"):
            self.assertIn(fragment, text)
        self.assertRegex(ADR_INDEX.read_text(encoding="utf-8"), r"\[0027\]\(0027-segmentacion-de-red-por-capas\.md\)")

    def test_architecture_document_describes_the_networks(self):
        text = ARQUITECTURA.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?i)segmentaci[oó]n de red")
        for network in ("edge", "app", "data", "media"):
            self.assertRegex(text, rf"`{network}`")


if __name__ == "__main__":
    unittest.main()
