"""nginx en el borde y Caddy como router interno (Prototipo 3, ADR 0031).

- `reverse-proxy` (nginx) es la única entrada pública: 80/443, TLS 1.2+, HSTS, cabeceras de
  seguridad, límite por IP, 1 MB de cuerpo y `/tint-media/*` hacia MinIO por TLS (escenario 1).
- `gateway` (Caddy) ya no publica puertos ni termina TLS: escucha HTTP en :8080 dentro de las
  redes, enruta, hace `forward_auth` y el límite por usuario, y confía en la IP que pasa nginx.
"""

from pathlib import Path
import re
import unittest

from test_network_segmentation import load, networks_of

ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "docker-compose.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
NGINX_DIR = ROOT / "reverse-proxy"
NGINX_CONF = NGINX_DIR / "templates" / "default.conf.template"
NGINX_MAIN = NGINX_DIR / "nginx.conf"
DOCKERFILE = NGINX_DIR / "Dockerfile"
PROXY_HEADERS = NGINX_DIR / "snippets" / "proxy-headers.conf"
CERT_SCRIPT = NGINX_DIR / "docker-entrypoint.d" / "15-self-signed-cert.sh"
CADDYFILE = ROOT / "gateway" / "Caddyfile"
ADR = ROOT / "docs" / "adr" / "0031-nginx-en-el-borde-y-caddy-como-router.md"
ADR_INDEX = ROOT / "docs" / "adr" / "README.md"


def https_server_block(conf: str) -> str:
    start = conf.index("listen 443")
    return conf[start:]


def http_server_block(conf: str) -> str:
    start = conf.index("listen 80")
    end = conf.index("listen 443")
    return conf[start:end]


class ReverseProxyComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = load(COMPOSE)
        cls.services = cls.compose["services"]
        cls.production = load(PRODUCTION_COMPOSE)["services"]

    def test_reverse_proxy_is_built_from_its_directory(self):
        self.assertEqual(self.services["reverse-proxy"]["build"], "./reverse-proxy")

    def test_only_the_reverse_proxy_publishes_80_and_443(self):
        ports = self.services["reverse-proxy"]["ports"]
        self.assertIn("${GATEWAY_HTTP_PORT:-80}:80", ports)
        self.assertIn("${GATEWAY_HTTPS_PORT:-443}:443", ports)
        self.assertNotIn("ports", self.services["gateway"])

    def test_reverse_proxy_lives_in_edge_and_media_only(self):
        self.assertEqual(networks_of(self.services["reverse-proxy"]), {"edge", "media"})

    def test_gateway_leaves_media_because_nginx_serves_the_videos(self):
        self.assertEqual(networks_of(self.services["gateway"]), {"edge", "app"})

    def test_reverse_proxy_waits_for_a_healthy_gateway(self):
        depends = self.services["reverse-proxy"]["depends_on"]
        self.assertEqual(depends["gateway"]["condition"], "service_healthy")

    def test_reverse_proxy_has_a_healthcheck_and_restarts(self):
        service = self.services["reverse-proxy"]
        self.assertIn("healthz", " ".join(service["healthcheck"]["test"]))
        self.assertEqual(service["restart"], "unless-stopped")

    def test_gateway_healthcheck_uses_internal_http(self):
        test = " ".join(self.services["gateway"]["healthcheck"]["test"])
        self.assertIn("http://localhost:8080/healthz", test)

    def test_signed_urls_point_to_the_public_https_entry(self):
        endpoint = self.services["identity-api"]["environment"]["MINIO_PUBLIC_ENDPOINT"]
        self.assertTrue(endpoint.startswith("${MINIO_PUBLIC_ENDPOINT:-https://"), endpoint)

    def test_production_closes_minio_9000(self):
        """Escenario 1: los vídeos ya no salen en claro; solo por /tint-media/* en 443."""
        self.assertEqual(self.production["minio"]["ports"], [])

    def test_production_publishes_only_the_edge_and_stream_udp(self):
        published = {
            name for name, svc in self.production.items() if svc.get("ports")
        }
        self.assertEqual(published, {"stream-core"})
        self.assertNotIn("ports", self.production.get("reverse-proxy", {}))


class NginxConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = NGINX_CONF.read_text(encoding="utf-8")
        cls.main = NGINX_MAIN.read_text(encoding="utf-8")
        cls.https = https_server_block(cls.conf)
        cls.http = http_server_block(cls.conf)

    def test_image_is_pinned_and_brings_openssl_for_the_dev_certificate(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"FROM nginx:1\.27\.\d+-alpine")
        self.assertIn("openssl", dockerfile)
        self.assertTrue(CERT_SCRIPT.exists())

    def test_dev_certificate_is_created_only_when_missing(self):
        script = CERT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("if [ ! -s", script)
        self.assertIn("${GATEWAY_HOST", script)

    def test_http_redirects_permanently_to_https_without_hsts(self):
        self.assertIn("return 308 https://", self.http)
        self.assertNotIn("Strict-Transport-Security", self.http)

    def test_https_offers_only_tls_1_2_and_1_3(self):
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", self.https)

    def test_https_sends_hsts_always(self):
        self.assertIn(
            'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;',
            self.https,
        )

    def test_security_headers_and_no_server_version(self):
        self.assertIn("server_tokens off;", self.main)
        for header in (
            "X-Content-Type-Options nosniff",
            "X-Frame-Options DENY",
            "Referrer-Policy strict-origin-when-cross-origin",
            "Content-Security-Policy",
        ):
            self.assertIn(f"add_header {header}", self.https)

    def test_body_limit_is_one_megabyte_outside_media(self):
        self.assertIn("client_max_body_size 1m;", self.https)

    def test_per_ip_rate_limit_answers_429_in_json(self):
        self.assertIn("limit_req_zone $binary_remote_addr zone=per_ip", self.conf)
        self.assertIn("${EDGE_RATE_PER_IP}", self.conf)
        self.assertIn("limit_req zone=per_ip", self.https)
        self.assertIn("limit_req_status 429;", self.main)
        self.assertIn('"error":"rate_limited"', self.https)
        self.assertIn("Retry-After", self.https)

    def test_too_large_answers_413_in_json(self):
        self.assertIn("error_page 413", self.https)
        self.assertIn('"error":"payload_too_large"', self.https)

    def test_proxy_forwards_the_client_ip_and_correlation_id(self):
        snippet = PROXY_HEADERS.read_text(encoding="utf-8")
        # nginx es el borde: sobrescribe X-Forwarded-For con la IP de la conexión. Si añadiera
        # ($proxy_add_x_forwarded_for), un cliente podría mandar una IP falsa y saltarse el límite
        # por IP de Caddy (verificado con curl contra la imagen real).
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", snippet)
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", snippet)
        self.assertIn("proxy_set_header X-Correlation-ID $correlation_id;", snippet)
        self.assertIn("proxy_set_header Host $http_host;", snippet)
        self.assertNotIn("$proxy_add_x_forwarded_for", self.conf + snippet)

    def test_every_proxied_location_includes_the_common_headers(self):
        """Un proxy_set_header dentro de un location borra los heredados del server: por eso cada
        location que hace proxy_pass incluye el snippet (bug encontrado con la imagen real)."""
        for block in re.findall(r"location [^{]+\{[^}]*\}", self.https):
            if "proxy_pass" in block:
                with self.subTest(block=block.splitlines()[0]):
                    self.assertIn("include /etc/nginx/snippets/proxy-headers.conf;", block)

    def test_websockets_are_upgraded_with_long_read_timeout(self):
        ws = self.https[self.https.index("location /ws/"):]
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", ws)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", ws)
        self.assertRegex(ws, r"proxy_read_timeout \d+[sm];")

    def test_media_goes_to_minio_keeping_the_signed_host(self):
        media = self.https[self.https.index("location /tint-media/"):]
        media = media[: media.index("}") + 1]
        self.assertIn("proxy_pass http://minio_upstream;", media)
        self.assertIn("include /etc/nginx/snippets/proxy-headers.conf;", media)
        self.assertIn("client_max_body_size 0;", media)
        self.assertIn("proxy_request_buffering off;", media)

    def test_upstreams_fail_over_passively_with_keepalive(self):
        self.assertRegex(self.conf, r"server gateway:8080 max_fails=\d+ fail_timeout=\d+s;")
        self.assertIn("keepalive", self.conf)
        self.assertIn("proxy_next_upstream error timeout", self.https)
        self.assertIn("proxy_connect_timeout", self.https)

    def test_logs_are_json_to_stdout(self):
        self.assertIn("log_format json escape=json", self.main)
        self.assertIn("access_log /dev/stdout json;", self.main)

    def test_edge_health_endpoint_follows_adr_0021(self):
        self.assertIn('"service":"reverse-proxy"', self.conf)


class CaddyBehindNginxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caddyfile = CADDYFILE.read_text(encoding="utf-8")

    def test_caddy_listens_plain_http_on_8080_only(self):
        self.assertIn("http://:8080 {", self.caddyfile)
        self.assertNotIn("https://{$GATEWAY_HOST", self.caddyfile)

    def test_caddy_trusts_the_client_ip_sent_by_nginx(self):
        self.assertRegex(self.caddyfile, r"trusted_proxies static private_ranges")
        self.assertIn("client_ip_headers X-Forwarded-For", self.caddyfile)
        # Sin modo estricto: nginx sobrescribe X-Forwarded-For con un solo valor (no se puede
        # falsificar) y el modo estricto trataría como proxy a un cliente con IP privada (LAN).
        self.assertNotIn("trusted_proxies_strict", self.caddyfile)

    def test_hsts_moves_to_the_edge(self):
        self.assertNotIn("Strict-Transport-Security", self.caddyfile)


class ReverseProxyDocsTests(unittest.TestCase):
    def test_adr_0031_is_written_and_indexed(self):
        text = ADR.read_text(encoding="utf-8")
        for fragment in ("nginx", "Caddy", "/tint-media/", "limit_req", "trusted_proxies"):
            self.assertIn(fragment, text)
        self.assertIn("0031-nginx-en-el-borde-y-caddy-como-router.md", ADR_INDEX.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
