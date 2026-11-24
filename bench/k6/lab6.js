// Laboratorio 6 (Performance): curva de rendimiento y knee por usuarios concurrentes (VUs), a través del gateway.
//
// A diferencia de api.js (tasa constante), aquí la carga es por VUs con `stages`, como pide el laboratorio. Cada
// nivel tiene una rampa corta y una meseta; solo las peticiones de la meseta se etiquetan `level:<vus>`, así cada
// punto de la curva mide un nivel estable. Los umbrales triviales de abajo obligan a k6 a exportar esas submétricas
// en `--summary-export`; `bench/summarize.py` las convierte en la tabla, la curva y el knee.
//
// TARGET (-e TARGET=...):
//   sync-ticket  POST /rooms/:id/sync-ticket (host). Puerta de sync, social, voz y transmisión. Doble autenticación
//                del host (gateway + ruta): 2 upserts sobre la misma fila de `users` por petición. Con HOSTS=1 todos
//                los VUs compiten por esa fila.
//   playback     GET /rooms/:id/playback (invitado). Lo sondea cada espectador; camino HS256 del forward_auth.
//   gateway      Descomposición por capas en cada iteración: /healthz (Caddy), /api/v1/healthz (proxy sin auth)
//                y playback (proxy + forward_auth). Tag `layer`.
//   claim        POST /invitations/claim (público, sin rate limit): avalancha de invitados; INSERT + XADD.
//   rooms        GET /rooms (host) con muchas salas sembradas (ROOMS): consultas sin índice en host_id/room_id.
//   ssr          GET /invite/:code: SSR de la web (Node) que consulta identity-api; cubre el front-end.
//
// Variables: BASE_URL, SERVER_IP, TARGET, LEVELS (1,50,200,500,1000,2000), RAMP (5s), HOLD (30s), SLEEP (1),
//            HOSTS (1), ROOMS (3), FIREBASE_PROJECT_ID.
// Ejemplo: k6 run -e TARGET=sync-ticket -e SERVER_IP=192.168.1.7 --summary-export out.json bench/k6/lab6.js

import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';
import { BASE_URL, authHeaders, jsonHeaders, seed } from './lib.js';

const TARGET = __ENV.TARGET || 'sync-ticket';
const LEVELS = (__ENV.LEVELS || '1,50,200,500,1000,2000').split(',').map((v) => parseInt(v, 10));
const RAMP_S = parseInt(__ENV.RAMP || '5', 10);
const HOLD_S = parseInt(__ENV.HOLD || '30', 10);
const SLEEP_S = parseFloat(__ENV.SLEEP || '1');
const HOSTS = parseInt(__ENV.HOSTS || '1', 10);
const ROOMS = parseInt(__ENV.ROOMS || '3', 10);
const LAYERS = ['caddy', 'proxy', 'auth'];

const stages = [];
for (const vus of LEVELS) {
  stages.push({ duration: `${RAMP_S}s`, target: vus }, { duration: `${HOLD_S}s`, target: vus });
}
stages.push({ duration: '5s', target: 0 });

const thresholds = {};
for (const vus of LEVELS) {
  const tags = TARGET === 'gateway' ? LAYERS.map((l) => `layer:${l},level:${vus}`) : [`level:${vus}`];
  for (const t of tags) {
    thresholds[`http_req_duration{${t}}`] = ['max>=0'];
    thresholds[`http_req_failed{${t}}`] = ['rate>=0'];
    thresholds[`http_reqs{${t}}`] = ['count>=0'];
    // 429 del límite de tasa (desde el escenario 2, ADR 0028): cuentan como fallos para el knee y se
    // desglosan aquí para distinguir "el límite actuó" de "el sistema se saturó".
    thresholds[`status_429{${t}}`] = ['count>=0'];
    thresholds[`status_5xx{${t}}`] = ['count>=0'];
  }
}

export const options = {
  insecureSkipTLSVerify: true,
  hosts: __ENV.SERVER_IP ? { localhost: __ENV.SERVER_IP } : {},
  setupTimeout: '300s',
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: { lab6: { executor: 'ramping-vus', startVUs: 0, stages, gracefulRampDown: '5s' } },
  thresholds,
};

export function setup() {
  return { seeded: seed(HOSTS, ROOMS) };
}

// Nivel de la meseta en curso, o 'ramp' durante las rampas (esas peticiones no entran en la curva).
function currentLevel() {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  let t = 0;
  for (const vus of LEVELS) {
    t += RAMP_S;
    if (elapsed < t) return 'ramp';
    t += HOLD_S;
    if (elapsed < t) return String(vus);
  }
  return 'ramp';
}

const status429 = new Counter('status_429');
const status5xx = new Counter('status_5xx');

const ok = { 'respuesta 2xx': (r) => r.status >= 200 && r.status < 300 };

function get(path, params, tags) {
  return http.get(`${BASE_URL}${path}`, { ...params, tags });
}

const TARGETS = {
  'sync-ticket': (s, tags) => {
    const roomId = s.roomIds[exec.vu.idInTest % s.roomIds.length];
    return [http.post(`${BASE_URL}/api/v1/rooms/${roomId}/sync-ticket`, null, { ...authHeaders(s.token), tags })];
  },
  playback: (s, tags) => [get(`/api/v1/rooms/${s.guestRoomId}/playback`, authHeaders(s.guestToken), tags)],
  gateway: (s, tags) => [
    get('/healthz', {}, { ...tags, layer: 'caddy' }),
    get('/api/v1/healthz', {}, { ...tags, layer: 'proxy' }),
    get(`/api/v1/rooms/${s.guestRoomId}/playback`, authHeaders(s.guestToken), { ...tags, layer: 'auth' }),
  ],
  claim: (s, tags) => {
    const body = JSON.stringify({ code: s.code, nick: `vu-${exec.vu.idInTest}-${exec.vu.iterationInScenario}` });
    return [http.post(`${BASE_URL}/api/v1/invitations/claim`, body, { ...jsonHeaders(), tags })];
  },
  rooms: (s, tags) => [get('/api/v1/rooms', authHeaders(s.token), tags)],
  ssr: (s, tags) => [get(`/invite/${s.code}`, {}, tags)],
};

if (!TARGETS[TARGET]) {
  throw new Error(`TARGET desconocido: ${TARGET} (opciones: ${Object.keys(TARGETS).join(', ')})`);
}

export default function (data) {
  const s = data.seeded[exec.vu.idInTest % data.seeded.length];
  const tags = { level: currentLevel(), target: TARGET };
  TARGETS[TARGET](s, tags).forEach((res, i) => {
    check(res, ok, tags);
    const t = TARGET === 'gateway' ? { ...tags, layer: LAYERS[i] } : tags;
    if (res.status === 429) status429.add(1, t);
    else if (res.status >= 500 || res.status === 0) status5xx.add(1, t);
  });
  sleep(SLEEP_S);
}
