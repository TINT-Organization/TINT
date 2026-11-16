// Utilidades compartidas de los escenarios k6 del banco de medición (bench/).
//
// El token de host es un ID token del Firebase Auth Emulator (alg: none, ADR 0024): solo lo
// acepta un identity-api en modo emulador con un proyecto demo-*. Es el stack de pruebas del
// banco; nunca se usa contra un entorno con Firebase real.

import encoding from 'k6/encoding';
import http from 'k6/http';

export const BASE_URL = __ENV.BASE_URL || 'https://localhost:8443';
export const PROJECT_ID = __ENV.FIREBASE_PROJECT_ID || 'demo-tint';

function b64(obj) {
  return encoding.b64encode(JSON.stringify(obj), 'rawurl');
}

export function hostToken(sub) {
  const now = Math.floor(Date.now() / 1000);
  const claims = {
    iss: `https://securetoken.google.com/${PROJECT_ID}`,
    aud: PROJECT_ID,
    sub,
    iat: now,
    exp: now + 3600,
    email: `${sub}@bench.invalid`,
    name: `Host ${sub}`,
  };
  return `${b64({ alg: 'none', typ: 'JWT' })}.${b64(claims)}.`;
}

export function authHeaders(token) {
  return { headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } };
}

export function jsonHeaders() {
  return { headers: { 'Content-Type': 'application/json' } };
}

function must(res, status, what) {
  if (res.status !== status) {
    throw new Error(`${what}: se esperaba ${status} y llegó ${res.status} ${String(res.body).slice(0, 120)}`);
  }
  return res.json();
}

// Siembra datos sintéticos: `hosts` hosts con `rooms` salas cada uno y un invitado por host.
export function seed(hosts, rooms) {
  const out = [];
  for (let i = 0; i < hosts; i++) {
    const token = hostToken(`bench-host-${i}`);
    const roomIds = [];
    let code = '';
    for (let r = 0; r < rooms; r++) {
      const created = must(
        http.post(`${BASE_URL}/api/v1/rooms`, JSON.stringify({ title: `Sala banco ${i}-${r}` }), authHeaders(token)),
        200,
        'crear sala',
      );
      roomIds.push(created.room.id);
      code = created.invitation.code;
    }
    const claim = must(
      http.post(`${BASE_URL}/api/v1/invitations/claim`, JSON.stringify({ code, nick: `invitado-${i}` }), jsonHeaders()),
      200,
      'reclamar invitación',
    );
    out.push({ token, roomIds, code, guestToken: claim.token, guestRoomId: claim.roomId });
  }
  return out;
}
