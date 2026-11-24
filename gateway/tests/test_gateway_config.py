from pathlib import Path
import unittest
import yaml


CONFIG = Path(__file__).parents[1] / "Caddyfile"
COMPOSE = Path(__file__).parents[2] / "docker-compose.yml"
PRODUCTION_COMPOSE = Path(__file__).parents[2] / "docker-compose.production.yml"


class GatewayConfigTests(unittest.TestCase):
    def test_gateway_config_exists_with_https_redirect_and_frontend_fallback(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("http://{$GATEWAY_HOST:localhost}", caddyfile)
        self.assertIn("redir https://{host}:{$GATEWAY_HTTPS_PORT:443}{uri} 308", caddyfile)
        self.assertIn("https://{$GATEWAY_HOST:localhost}", caddyfile)
        self.assertIn("@health path /healthz", caddyfile)
        self.assertIn("@frontend not path /api/* /ws/*", caddyfile)
        self.assertIn("reverse_proxy @frontend web:5173", caddyfile)

    def test_gateway_redirects_plain_http_to_https_with_308(self):
        """OPS-03/AC3: redirige HTTP plano a HTTPS con código 308 conservando host y URI."""
        caddyfile = CONFIG.read_text(encoding="utf-8")
        self.assertIn("http://{$GATEWAY_HOST:localhost}", caddyfile)
        self.assertIn("redir https://{host}:{$GATEWAY_HTTPS_PORT:443}{uri} 308", caddyfile)

    def test_gateway_routes_all_public_service_connectors(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("path /api/* /ws/*", caddyfile)
        self.assertIn("not path /api/v1/healthz /api/v1/invitations/claim", caddyfile)
        self.assertIn("forward_auth @protected {$IDENTITY_API_UPSTREAM:identity-api:8081}", caddyfile)
        self.assertIn("uri /api/v1/internal/auth/check", caddyfile)
        self.assertIn("copy_headers X-User-Id X-User-Sub X-User-Role X-User-Room-Id X-User-Nick", caddyfile)
        self.assertIn("import service_timeout", caddyfile)
        self.assertIn(
            "reverse_proxy @identity {$IDENTITY_API_UPSTREAM:identity-api:8081}",
            caddyfile,
        )
        self.assertIn("reverse_proxy @sync {$SYNC_CORE_UPSTREAM:sync-core:8080}", caddyfile)
        self.assertIn("reverse_proxy @social {$SOCIAL_CORE_UPSTREAM:social-core:8082}", caddyfile)
        self.assertIn("reverse_proxy @analytics {$ANALYTICS_UPSTREAM:analytics:8084}", caddyfile)
        self.assertIn("@voice path /ws/voice /ws/voice/*", caddyfile)
        self.assertIn("reverse_proxy @voice {$VOICE_CORE_UPSTREAM:voice-core:8085}", caddyfile)
        self.assertIn("@voice_api path /api/v1/voice/*", caddyfile)
        self.assertIn("reverse_proxy @voice_api {$VOICE_CORE_UPSTREAM:voice-core:8085}", caddyfile)

    def test_gateway_routes_social_rest_before_identity_catch_all(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")
        self.assertIn("@social_api path /api/v1/social/*", caddyfile)
        self.assertIn("reverse_proxy @social_api {$SOCIAL_CORE_UPSTREAM:social-core:8082}", caddyfile)
        self.assertLess(caddyfile.index("@social_api path"), caddyfile.index("@identity path /api/*"))

    def test_gateway_routes_stream_signaling_before_identity(self):
        # WHIP/WHEP por HTTP a stream-core (HU-LIVE-06, R8). Debe ir antes del catch-all /api/*.
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("@stream_api path /api/v1/stream/*", caddyfile)
        self.assertIn("reverse_proxy @stream_api {$STREAM_CORE_UPSTREAM:stream-core:8086}", caddyfile)
        self.assertLess(
            caddyfile.index("@stream_api path"), caddyfile.index("@identity path /api/*")
        )

    def test_gateway_hides_internal_identity_routes(self):
        # /api/v1/internal/* solo para la red interna (ADR 0019). forward_auth llama a auth/check
        # directo al upstream, sin pasar por las rutas públicas, así que también se oculta.
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("@internal_api path /api/v1/internal/*", caddyfile)
        self.assertIn("respond @internal_api 404", caddyfile)

    def test_internal_routes_skip_forward_auth_so_they_answer_404_without_token(self):
        # ADR 0028: en Caddy el orden lo fija la directiva, no el texto. forward_auth corre antes que
        # respond, así que sin token la ruta interna daba 401 (línea base, P10). Se saca de @protected.
        caddyfile = CONFIG.read_text(encoding="utf-8")
        protected = caddyfile.split("@protected {", 1)[1].split("}", 1)[0]

        self.assertIn("/api/v1/internal/*", protected)
        self.assertIn("not path", protected)

    def test_gateway_lets_cors_preflight_through_without_token(self):
        # El navegador no manda Authorization en el preflight OPTIONS del WHIP/WHEP.
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("not method OPTIONS", caddyfile)

    def test_gateway_declares_configurable_service_upstreams(self):
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertIn("SYNC_CORE_UPSTREAM:", compose)
        self.assertIn("SOCIAL_CORE_UPSTREAM:", compose)
        self.assertIn("ANALYTICS_UPSTREAM:", compose)
        self.assertIn("VOICE_CORE_UPSTREAM:", compose)
        self.assertIn("STREAM_CORE_UPSTREAM:", compose)

    def test_gateway_returns_clear_503_for_unavailable_upstreams(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("handle_errors", caddyfile)
        self.assertIn("http.error.status_code", caddyfile)
        self.assertIn("respond @upstream_unavailable", caddyfile)
        self.assertIn('"error":"service_unavailable"', caddyfile)
        self.assertIn(" 503", caddyfile)

    def test_gateway_limits_upstream_waits_to_five_seconds(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertGreaterEqual(caddyfile.count("dial_timeout 5s"), 1)
        self.assertGreaterEqual(caddyfile.count("response_header_timeout 5s"), 1)

    def test_gateway_service_publishes_http_and_https_and_restarts(self):
        compose = COMPOSE.read_text(encoding="utf-8")

        self.assertIn("gateway:", compose)
        self.assertIn('"${GATEWAY_HTTP_PORT:-80}:80"', compose)
        self.assertIn('"${GATEWAY_HTTPS_PORT:-443}:443"', compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("- gateway-data:/data", compose)

    def test_gateway_sends_hsts_header_on_https(self):
        """SEC/Escenario 1 (Secure Channel): el bloque HTTPS emite Strict-Transport-Security.

        max-age de un año, includeSubDomains y preload para que el navegador fije HTTPS
        tras la primera visita. La cabecera vive solo en el sitio HTTPS, nunca en el
        redirector HTTP plano (ADR 0029).
        """
        caddyfile = CONFIG.read_text(encoding="utf-8")

        hsts = "Strict-Transport-Security \"max-age=31536000; includeSubDomains; preload\""
        self.assertIn(f"header {hsts}", caddyfile)

        https_block_start = caddyfile.index("https://{$GATEWAY_HOST:localhost}")
        http_block = caddyfile[: caddyfile.index("(service_timeout)")]
        self.assertNotIn("Strict-Transport-Security", http_block)
        self.assertGreater(caddyfile.index("Strict-Transport-Security"), https_block_start)

    def test_gateway_preserves_or_generates_correlation_id(self):
        caddyfile = CONFIG.read_text(encoding="utf-8")

        self.assertIn("@missing_correlation not header X-Correlation-ID *", caddyfile)
        self.assertIn("request_header @missing_correlation X-Correlation-ID {http.request.uuid}", caddyfile)
        self.assertIn("header X-Correlation-ID {http.request.header.X-Correlation-ID}", caddyfile)
        self.assertIn("format json", caddyfile)
        self.assertIn("log_append correlation_id {http.request.header.X-Correlation-ID}", caddyfile)


class GatewayReverseProxyHardeningTests(unittest.TestCase):
    """Prototipo 3, escenario 2 (ADR 0028): límites de tasa y tamaño, cabeceras y orden de la entrada."""

    def setUp(self):
        self.caddyfile = CONFIG.read_text(encoding="utf-8")

    def route_block(self) -> str:
        start = self.caddyfile.index("    route {")
        end = self.caddyfile.index("\n    }\n", start)
        return self.caddyfile[start:end]

    def test_auth_and_rate_limits_run_inside_a_route_in_written_order(self):
        # rate_limit se ordena por defecto antes de forward_auth; dentro de route se respeta el texto.
        block = self.route_block()
        steps = [
            "request_header -X-User-Id",
            "zone per_ip_without_session",
            "forward_auth @protected",
            "zone per_user",
        ]
        positions = [block.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

    def test_client_cannot_inject_identity_headers(self):
        # copy_headers solo escribe lo que devuelve identity-api; lo que mande el cliente se borra antes.
        block = self.route_block()
        for header in ("X-User-Id", "X-User-Sub", "X-User-Role", "X-User-Room-Id", "X-User-Nick"):
            self.assertIn(f"request_header -{header}", block)
            self.assertLess(block.index(f"request_header -{header}"), block.index("forward_auth @protected"))

    def test_per_user_limit_uses_verified_identity_and_skips_websockets(self):
        block = self.route_block()
        zone = block.split("zone per_user {", 1)[1]

        self.assertIn("key {http.request.header.X-User-Id}", zone)
        self.assertIn("header X-User-Id *", zone)
        self.assertIn("path /api/*", zone)
        self.assertNotIn("/ws/", zone)
        self.assertIn("window 10s", zone)
        self.assertIn("events 100", zone)

    def test_per_ip_limit_covers_invitation_routes_with_client_ip(self):
        block = self.route_block()
        zone = block.split("zone per_ip_without_session {", 1)[1].split("forward_auth", 1)[0]

        self.assertIn("path /api/v1/invitations/*", zone)
        self.assertIn("key {client_ip}", zone)
        self.assertIn("window 10s", zone)
        self.assertIn("events 600", zone)

    def test_rate_limited_and_oversized_requests_get_json_errors(self):
        errors = self.caddyfile.split("handle_errors {", 1)[1]

        self.assertIn("{http.error.status_code} == 429", errors)
        self.assertIn('"error":"rate_limited"', errors)
        self.assertIn('"error":"payload_too_large"', self.caddyfile)
        self.assertIn(" 413", self.caddyfile)

    def test_request_body_is_limited_before_reading_it(self):
        # request_body solo da 502 al enviar el cuerpo al upstream; Content-Length permite un 413 temprano.
        self.assertIn("header_regexp Content-Length ^[0-9]{7,}$", self.caddyfile)
        self.assertIn("respond @body_too_large", self.caddyfile)
        self.assertIn("max_size 1MB", self.caddyfile)

    def test_responses_hide_server_and_send_security_headers(self):
        self.assertIn("-Server", self.caddyfile)
        self.assertIn("X-Content-Type-Options nosniff", self.caddyfile)
        self.assertIn("X-Frame-Options DENY", self.caddyfile)
        self.assertIn("Referrer-Policy strict-origin-when-cross-origin", self.caddyfile)
        self.assertIn(
            "Content-Security-Policy \"frame-ancestors 'none'; object-src 'none'; base-uri 'self'\"",
            self.caddyfile,
        )

    def test_hsts_is_preserved_from_scenario_1(self):
        # HSTS es de Secure Channel (escenario 1) y se conserva junto con las medidas del escenario 2.
        self.assertIn("Strict-Transport-Security", self.caddyfile)


class GatewayImageTests(unittest.TestCase):
    """ADR 0028: el gateway se compila con caddy-ratelimit; versiones fijas."""

    DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"

    def test_gateway_is_built_from_its_own_dockerfile(self):
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        gateway = data["services"]["gateway"]

        self.assertEqual(gateway.get("build"), "./gateway")
        self.assertNotIn("image", gateway)

    def test_dockerfile_pins_caddy_and_the_rate_limit_module(self):
        text = self.DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("ARG CADDY_VERSION=2.9.1", text)
        self.assertIn("FROM caddy:${CADDY_VERSION}-builder-alpine AS builder", text)
        self.assertIn("FROM caddy:${CADDY_VERSION}-alpine", text)
        self.assertIn("github.com/mholt/caddy-ratelimit@v0.1.0", text)
        self.assertIn("http.handlers.rate_limit", text)

    def test_ci_validates_the_caddyfile_with_the_built_image(self):
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci-pipeline.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker build -t tint-gateway:ci gateway", workflow)
        self.assertIn("tint-gateway:ci caddy validate", workflow)
        self.assertNotIn("caddy:2.9-alpine caddy validate", workflow)


class ComposeStructureTests(unittest.TestCase):
    def test_production_override_keeps_gateway_as_the_only_public_web_api_ws_entry(self):
        """OPS-03/AC1: en producción web, API y WS no se publican fuera del gateway."""
        compose = COMPOSE.read_text(encoding="utf-8")
        production = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

        self.assertIn('"${GATEWAY_HTTP_PORT:-80}:80"', compose)
        self.assertIn('"${GATEWAY_HTTPS_PORT:-443}:443"', compose)
        self.assertNotIn("  gateway:\n    ports: !reset []", production)
        for service in ("web", "identity-api", "sync-core", "social-core", "analytics", "voice-core"):
            self.assertIn(
                f"  {service}:\n    ports: !reset []",
                production,
                msg=f"{service} debe dejar de publicar puertos en producción",
            )

    def test_compose_web_build_args_use_relative_endpoints(self):
        """OPS-03/AC1: build args de web en docker-compose.yml usan rutas relativas al gateway."""
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("VITE_IDENTITY_API_URL: ${VITE_IDENTITY_API_URL:-/api/v1}", compose)
        self.assertIn("VITE_SYNC_WS_URL: ${VITE_SYNC_WS_URL:-/ws/sync}", compose)
        self.assertIn("VITE_SOCIAL_WS_URL: ${VITE_SOCIAL_WS_URL:-/ws/social}", compose)
        self.assertIn("VITE_VOICE_WS_URL: ${VITE_VOICE_WS_URL:-/ws/voice}", compose)
        self.assertIn("VITE_ANALYTICS_API_URL: ${VITE_ANALYTICS_API_URL:-/api/v1}", compose)

    def test_gateway_persists_certificates_and_restarts_on_failure(self):
        """OPS-03/AC2: Caddy conserva certificados en gateway-data y se reinicia automáticamente."""
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        gateway = data.get("services", {}).get("gateway", {})
        self.assertEqual(gateway.get("restart"), "unless-stopped")
        volumes = gateway.get("volumes", [])
        self.assertTrue(
            any("gateway-data:/data" in str(v) for v in volumes),
            "El gateway debe montar gateway-data en /data para preservar certificados",
        )
        self.assertIn("gateway-data", data.get("volumes", {}))

    def test_compose_lists_all_required_services(self):
        """OPS-02/AC1: gateway, web, servicios, bases de datos, MongoDB y coturn en el Compose."""
        compose = COMPOSE.read_text(encoding="utf-8")
        required = [
            "gateway:",
            "web:",
            "identity-api:",
            "sync-core:",
            "social-core:",
            "media-worker:",
            "analytics:",
            "voice-core:",
            "postgres:",
            "redis:",
            "minio:",
            "mongodb:",
            "coturn:",
        ]
        for svc in required:
            self.assertIn(svc, compose, msg=f"Falta el servicio {svc!r} en docker-compose.yml")

    def test_compose_images_use_pinned_versions(self):
        """OPS-02/AC2: ninguna imagen de infraestructura usa :latest."""
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        for name, svc in data.get("services", {}).items():
            image = svc.get("image", "")
            if image:
                self.assertFalse(
                    image.endswith(":latest"),
                    msg=f"Servicio {name!r} usa imagen con :latest -> {image!r}",
                )

    def test_compose_coturn_is_declared_as_container(self):
        """OPS-02/AC3: coturn declarado como servicio Compose (no proceso host)."""
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("coturn:", compose)
        self.assertIn("coturn/coturn:", compose)

    def test_compose_minio_version_is_pinned(self):
        """OPS-02/AC2: las imágenes de MinIO y mc deben tener tag semver, no :latest."""
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        for svc_name in ("minio", "minio-init"):
            image = data["services"][svc_name].get("image", "")
            self.assertNotEqual(image, "", msg=f"{svc_name} no tiene imagen declarada")
            self.assertFalse(
                image.endswith(":latest"),
                msg=f"{svc_name} usa :latest -> {image!r}",
            )

    def test_compose_mongodb_service_exists_with_pinned_image(self):
        """SOC-07/DAT-02 prerequisito: mongodb declarado con imagen mongo:<tag> fijo."""
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        self.assertIn("mongodb", data["services"], "Falta el servicio mongodb")
        image = data["services"]["mongodb"].get("image", "")
        self.assertFalse(image.endswith(":latest"), f"mongodb usa :latest -> {image!r}")
        self.assertTrue(image.startswith("mongo:"), f"Imagen de mongodb debe ser mongo:<tag>, no {image!r}")

    def test_compose_mongodb_has_volume_and_healthcheck(self):
        """SOC-02/AC3: MongoDB debe tener volumen persistente y healthcheck."""
        data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        svc = data["services"].get("mongodb", {})
        volumes = svc.get("volumes", [])
        self.assertTrue(
            any("mongo" in str(v) for v in volumes),
            "mongodb debe montar un volumen nombrado para persistencia",
        )
        self.assertIn("healthcheck", svc, "mongodb debe tener healthcheck")

    def test_env_example_declares_mongo_uri(self):
        """SOC-07: MONGO_URI debe estar declarado en .env.example."""
        env_example = Path(__file__).parents[2] / ".env.example"
        content = env_example.read_text(encoding="utf-8")
        self.assertIn("MONGO_URI", content, "MONGO_URI debe estar en .env.example")


class DockerfileTests(unittest.TestCase):
    def test_voice_core_runtime_image_installs_curl_for_healthcheck(self):
        """OPS-02/AC2: la etapa final de voice-core debe tener curl para su HEALTHCHECK."""
        dockerfile = Path(__file__).parents[2] / "services" / "voice-core" / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        runtime_section = text.split("FROM debian:trixie-slim\n")[-1]
        self.assertIn("curl", runtime_section)
        self.assertIn("HEALTHCHECK", runtime_section)

    def test_identity_api_dockerfile_is_multistage(self):
        """OPS-02/AC2: identity-api debe usar build multi-stage (no devDependencies en prod)."""
        dockerfile = Path(__file__).parents[2] / "services" / "identity-api" / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        from_count = sum(
            1 for line in text.splitlines() if line.strip().upper().startswith("FROM")
        )
        self.assertGreaterEqual(
            from_count,
            2,
            "Dockerfile de identity-api debe tener al menos 2 etapas (multi-stage)",
        )

    def test_web_dockerfile_defaults_use_relative_endpoints(self):
        """OPS-03/AC1: el Dockerfile de web no debe hornear puertos localhost:808x directos."""
        dockerfile = Path(__file__).parents[2] / "apps" / "web" / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        self.assertNotIn("localhost:808", text)
        self.assertIn("ARG VITE_IDENTITY_API_URL=/api/v1", text)
        self.assertIn("ARG VITE_SYNC_WS_URL=/ws/sync", text)
        self.assertIn("ARG VITE_SOCIAL_WS_URL=/ws/social", text)
        self.assertIn("ARG VITE_VOICE_WS_URL=/ws/voice", text)
        self.assertIn("ARG VITE_ANALYTICS_API_URL=/api/v1", text)
