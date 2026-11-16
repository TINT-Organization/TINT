// Carga HTTP de la API de TINT a través del gateway (bench/).
//
// Mezcla ponderada de operaciones reales de un host y de un invitado. Usa `constant-arrival-rate`:
// la tasa de llegada es fija, así que si el sistema se vuelve lento el p95 sube y las iteraciones
// perdidas quedan en `dropped_iterations`, en vez de esconderse como pasaría con VUs fijos.
//
// Variables (-e):  BASE_URL (https://localhost:8443), SERVER_IP (IP a la que resuelve localhost), RATE (req/s, por defecto 50), DURATION (por defecto 60s),
//                  PHASE=mix|burst, HOSTS (20), ROOMS (3), FIREBASE_PROJECT_ID.
// PHASE=mix mide latencia; PHASE=burst manda RATE req/s a un solo endpoint y cuenta los 429.
// Ejemplo: k6 run --insecure-skip-tls-verify -e RATE=100 bench/k6/api.js

import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { BASE_URL, authHeaders, jsonHeaders, seed } from './lib.js';

const RATE = parseInt(__ENV.RATE || '50', 10);
const DURATION = __ENV.DURATION || '60s';
const PHASE = __ENV.PHASE || 'mix';
const HOSTS = parseInt(__ENV.HOSTS || '20', 10);
const ROOMS = parseInt(__ENV.ROOMS || '3', 10);

export const options = {
  insecureSkipTLSVerify: true,
  hosts: __ENV.SERVER_IP ? { localhost: __ENV.SERVER_IP } : {},
  summaryTrendStats: ['min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    api: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(20, Math.ceil(RATE / 5)),
      maxVUs: Math.max(200, RATE * 2),
    },
  },
};

const status429 = new Counter('status_429');
const status2xx = new Counter('status_2xx');
const status5xx = new Counter('status_5xx');

export function setup() {
  return { seeded: seed(HOSTS, ROOMS) };
}

function count(res) {
  if (res.status === 429) status429.add(1);
  else if (res.status >= 200 && res.status < 300) status2xx.add(1);
  else if (res.status >= 500) status5xx.add(1);
}

const MIX = [
  [10, healthz],
  [30, listRooms],
  [25, syncTicket],
  [20, playback],
  [15, claim],
];

function pick() {
  let r = Math.random() * 100;
  for (const [weight, fn] of MIX) {
    if ((r -= weight) < 0) return fn;
  }
  return healthz;
}

function healthz() {
  return http.get(`${BASE_URL}/healthz`, { tags: { endpoint: 'healthz' } });
}

function listRooms(s) {
  return http.get(`${BASE_URL}/api/v1/rooms`, { ...authHeaders(s.token), tags: { endpoint: 'rooms' } });
}

function syncTicket(s) {
  const roomId = s.roomIds[Math.floor(Math.random() * s.roomIds.length)];
  return http.post(`${BASE_URL}/api/v1/rooms/${roomId}/sync-ticket`, null, {
    ...authHeaders(s.token),
    tags: { endpoint: 'sync-ticket' },
  });
}

function playback(s) {
  return http.get(`${BASE_URL}/api/v1/rooms/${s.guestRoomId}/playback`, {
    ...authHeaders(s.guestToken),
    tags: { endpoint: 'playback' },
  });
}

function claim(s) {
  const body = JSON.stringify({ code: s.code, nick: `carga-${Math.floor(Math.random() * 1e6)}` });
  return http.post(`${BASE_URL}/api/v1/invitations/claim`, body, { ...jsonHeaders(), tags: { endpoint: 'claim' } });
}

export default function (data) {
  const s = data.seeded[Math.floor(Math.random() * data.seeded.length)];
  const res = PHASE === 'burst' ? listRooms(s) : pick()(s);
  count(res);
  if (PHASE !== 'burst') {
    check(res, { 'respuesta 2xx': (r) => r.status >= 200 && r.status < 300 });
  }
}
