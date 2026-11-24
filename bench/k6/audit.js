// Acciones sensibles del escenario 5 (bitácora de auditoría) a través del gateway (bench/).
//
// Cada iteración es el recorrido de un host: crea una sala, la renombra, crea y revoca una invitación,
// bloquea y desbloquea a un invitado, lo retira, sufre dos accesos negados (403 de otro host y 401 sin
// token) y borra la sala. Cada acción que responde lo esperado suma 1 a
// `n_<acción>`: es lo "hecho" que bench/audit_run.py compara con lo registrado en `audit_entries`.
// La latencia de cada acción queda en `t_<acción>` y la de todas juntas en `t_sensible`.
// Fuera del recorrido: `room.end` (iniciar una sala live falla 1 de cada 2 veces por un error de
// analytics en `/internal/events/start`, ajeno a la bitácora) y `room.disable` (lo pide stream-core).
// Las dos quedan cubiertas por las pruebas de identity-api.
//
// Los hosts son `bench-audit-<RUN_ID>-<VU>` (y `...-x` el intruso del 403): así el conteo filtra por
// actor y no mezcla corridas. Tasa fija (`constant-arrival-rate`) para que antes y después reciban la
// misma carga.
//
// Variables (-e): BASE_URL (https://localhost:8443), RUN_ID (obligatorio, [a-z0-9-]), RATE (recorridos/s,
//                 por defecto 2), DURATION (por defecto 60s), FIREBASE_PROJECT_ID.
// Ejemplo: k6 run -e BASE_URL=https://localhost -e RUN_ID=prueba bench/k6/audit.js

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { BASE_URL, authHeaders, hostToken, jsonHeaders } from './lib.js';

const RUN_ID = __ENV.RUN_ID || '';
const RATE = parseInt(__ENV.RATE || '2', 10);
const DURATION = __ENV.DURATION || '60s';

if (!/^[a-z0-9-]{1,40}$/.test(RUN_ID)) {
  throw new Error('RUN_ID es obligatorio y solo admite [a-z0-9-] (máx. 40)');
}

export const options = {
  insecureSkipTLSVerify: true,
  summaryTrendStats: ['min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    audit: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RATE * 4),
      maxVUs: Math.max(50, RATE * 20),
    },
  },
};

// `access.denied.401` es el 401 sin token que responde el gateway (forward_auth): se cuenta aparte.
const ACTIONS = [
  'room.create',
  'room.update',
  'invitation.create',
  'ban.create',
  'ban.delete',
  'guest.revoke',
  'invitation.revoke',
  'access.denied',
  'access.denied.401',
  'room.delete',
];
const metricName = (action) => action.replace(/\./g, '_');
const done = Object.fromEntries(ACTIONS.map((a) => [a, new Counter(`n_${metricName(a)}`)]));
const latency = Object.fromEntries(ACTIONS.map((a) => [a, new Trend(`t_${metricName(a)}`, true)]));
const sensitive = new Trend('t_sensible', true);
const failed = new Counter('pasos_fallidos');

/** Registra la acción si respondió `status`; si no, corta el recorrido (lanza). */
function step(action, res, status) {
  latency[action].add(res.timings.duration);
  sensitive.add(res.timings.duration);
  const ok = check(res, { [`${action} → ${status}`]: (r) => r.status === status });
  if (!ok) {
    failed.add(1);
    throw new Error(`${action}: se esperaba ${status} y llegó ${res.status}`);
  }
  done[action].add(1);
  return status === 200 ? res.json() : null;
}

function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const API = `${BASE_URL}/api/v1`;
const tag = (name) => ({ tags: { name } });

export default function () {
  const host = authHeaders(hostToken(`bench-audit-${RUN_ID}-${__VU}`));
  const intruder = authHeaders(hostToken(`bench-audit-${RUN_ID}-${__VU}-x`));
  const opts = (name, base = host) => ({ ...base, ...tag(name) });

  const created = step('room.create', http.post(`${API}/rooms`, JSON.stringify({ title: 'Bitácora' }), opts('POST /rooms')), 200);
  const roomId = created.room.id;
  const room = `${API}/rooms/${roomId}`;

  step('room.update', http.patch(room, JSON.stringify({ title: 'Bitácora 2' }), opts('PATCH /rooms/:id')), 200);
  const invitation = step('invitation.create', http.post(`${room}/invitations`, '{}', opts('POST /rooms/:id/invitations')), 200);

  // Un invitado entra (no es acción sensible) para poder bloquearlo y retirarlo.
  const claim = http.post(
    `${API}/invitations/claim`,
    JSON.stringify({ code: invitation.code, nick: 'invitado', deviceId: uuid() }),
    { ...jsonHeaders(), ...tag('POST /invitations/claim') },
  );
  if (claim.status !== 200) {
    failed.add(1);
    throw new Error(`claim: llegó ${claim.status}`);
  }
  const guests = http.get(`${room}/guests`, opts('GET /rooms/:id/guests')).json('guests');
  const jti = guests[0].jti;

  const ban = step('ban.create', http.post(`${room}/bans`, JSON.stringify({ jti }), opts('POST /rooms/:id/bans')), 200);
  step('ban.delete', http.del(`${room}/bans/${ban.ban.id}`, null, opts('DELETE /rooms/:id/bans/:banId')), 200);
  step('guest.revoke', http.post(`${room}/guests/${jti}/revoke`, null, opts('POST /rooms/:id/guests/:jti/revoke')), 200);
  step('invitation.revoke', http.post(`${API}/invitations/${invitation.code}/revoke`, null, opts('POST /invitations/:code/revoke')), 200);

  step('access.denied', http.get(room, opts('GET /rooms/:id (otro host)', intruder)), 403);
  step('access.denied.401', http.get(room, tag('GET /rooms/:id (sin token)')), 401);

  step('room.delete', http.del(room, null, opts('DELETE /rooms/:id')), 200);
}
