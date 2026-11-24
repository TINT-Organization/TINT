import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import jwt from 'jsonwebtoken';

// ID token de Firebase del host: requireAuthHost real lo verificaría con JWKS; aquí basta con
// que el token no sea HS256 (así lo despacha la ruta) y que traiga el sub esperado.
vi.mock('../src/middlewares/auth.js', () => ({
  requireAuthHost: vi.fn((req: any, res: any, next: any) => {
    const header: string = req.headers.authorization ?? '';
    if (!header.startsWith('Bearer ')) return res.status(401).json({ error: 'Unauthorized' });
    req.user = { id: 'host-1', providerSub: 'fb-sub-1', email: 'h@t.c', displayName: 'Ana Host' };
    next();
  }),
}));

vi.mock('../src/modules/rooms/rooms.repo.js', () => ({
  create: vi.fn(),
  listByHost: vi.fn(),
  findById: vi.fn(),
  isOwner: vi.fn(),
  softRemoveById: vi.fn(),
  updateTitle: vi.fn(),
  setMedia: vi.fn(),
}));

vi.mock('../src/modules/guests/guestSessions.repo.js', () => ({
  create: vi.fn(),
  findByJti: vi.fn(),
  listByRoom: vi.fn(),
  listByRoomAndSub: vi.fn(),
  setRevoked: vi.fn(),
  listActiveByRoom: vi.fn(),
  setRevokedMany: vi.fn(),
}));

vi.mock('../src/modules/bans/roomBans.repo.js', () => ({
  create: vi.fn(),
  findByRoomAndDevice: vi.fn(),
  findByRoomAndSub: vi.fn(),
  listByRoom: vi.fn(),
  removeById: vi.fn(),
}));

import app from '../src/app.js';
import { env } from '../src/config/env.js';
import { findById, isOwner } from '../src/modules/rooms/rooms.repo.js';
import { findByJti } from '../src/modules/guests/guestSessions.repo.js';
import { findByRoomAndDevice, findByRoomAndSub } from '../src/modules/bans/roomBans.repo.js';
import { setRedisClient } from '../src/modules/tokens/revocation.js';
import { issueGuestToken } from '../src/modules/auth/auth.service.js';

const ROOM_ID = '11111111-2222-4333-8444-555555555555';
const OTHER_ROOM = '99999999-2222-4333-8444-555555555555';
const room = (status = 'active') => ({ id: ROOM_ID, hostId: 'host-1', title: 'Sala', status, kind: 'watch' });

const firebaseToken = `${Buffer.from('{"alg":"RS256","typ":"JWT"}').toString('base64url')}.${Buffer.from('{"sub":"fb-sub-1"}').toString('base64url')}.firma`;

const xadd = vi.fn(async (..._args: string[]) => '1-0');
const exists = vi.fn(async (_key: string) => 0);

function auditFields() {
  return xadd.mock.calls
    .filter((call) => call[0] === 'audit:events')
    .map((call) => {
      const args = call.slice(5);
      return Object.fromEntries(args.flatMap((_, i) => (i % 2 === 0 ? [[args[i], args[i + 1]]] : [])));
    });
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

function post(service: string, token?: string, roomId = ROOM_ID) {
  const req = request(app).post(`/api/v1/rooms/${roomId}/tickets/${service}`);
  return token ? req.set('Authorization', `Bearer ${token}`).send({}) : req.send({});
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(findById).mockResolvedValue(room() as any);
  vi.mocked(isOwner).mockResolvedValue(true);
  vi.mocked(findByJti).mockResolvedValue(null as any);
  vi.mocked(findByRoomAndDevice).mockResolvedValue(null as any);
  vi.mocked(findByRoomAndSub).mockResolvedValue(null as any);
  setRedisClient({ exists, xadd } as any);
});

afterEach(() => {
  env.JWT_LEGACY_AUDIENCE = undefined;
  setRedisClient(null);
});

describe('POST /rooms/:id/tickets/:service · host (ADR 0030)', () => {
  it.each(['sync', 'social', 'voice', 'stream', 'analytics'])('emite un ticket de host con aud tint:%s', async (service) => {
    const res = await post(service, firebaseToken);
    expect(res.status).toBe(200);
    expect(res.body.audience).toBe(`tint:${service}`);
    const decoded = jwt.verify(res.body.token, env.JWT_SECRET, {
      algorithms: ['HS256'],
      issuer: env.JWT_ISSUER,
      audience: `tint:${service}`,
    }) as any;
    expect(decoded).toMatchObject({ sub: 'host-1', room_id: ROOM_ID, role: 'host', nick: 'Ana Host' });
    expect(typeof decoded.jti).toBe('string');
    expect(decoded.exp - decoded.iat).toBe(env.SERVICE_TICKET_TTL_SECONDS);
    expect(new Date(res.body.expires_at).getTime()).toBe(decoded.exp * 1000);
    expect(isOwner).toHaveBeenCalledWith(ROOM_ID, 'host-1');
  });

  it('cada ticket de host lleva un jti nuevo', async () => {
    const a = jwt.decode((await post('sync', firebaseToken)).body.token) as any;
    const b = jwt.decode((await post('sync', firebaseToken)).body.token) as any;
    expect(a.jti).not.toBe(b.jti);
  });

  it('404 con un servicio desconocido', async () => {
    for (const service of ['api', 'room', 'toString', 'SYNC']) {
      const res = await post(service, firebaseToken);
      expect(res.status).toBe(404);
      expect(res.body.token).toBeUndefined();
    }
  });

  it('401 sin token', async () => {
    expect((await post('sync')).status).toBe(401);
  });

  it('403 si no es dueño de la sala (y access.denied en la bitácora)', async () => {
    vi.mocked(isOwner).mockResolvedValue(false);
    const res = await post('voice', firebaseToken);
    expect(res.status).toBe(403);
    expect(res.body.token).toBeUndefined();
    await flush();
    expect(auditFields()).toEqual([expect.objectContaining({ action: 'access.denied', actor_id: 'fb-sub-1', room_id: ROOM_ID })]);
  });

  it('404 si la sala no existe', async () => {
    vi.mocked(findById).mockResolvedValue(null as any);
    expect((await post('sync', firebaseToken)).status).toBe(404);
  });

  it.each(['sync', 'voice', 'stream'])('410 Disabled al pedir %s con la sala deshabilitada', async (service) => {
    vi.mocked(findById).mockResolvedValue(room('disabled') as any);
    const res = await post(service, firebaseToken);
    expect(res.status).toBe(410);
    expect(res.body).toEqual({ error: 'Disabled' });
  });

  it('con la sala deshabilitada aún emite social (historial del chat, HU-LIVE-09) y analytics (KPIs)', async () => {
    vi.mocked(findById).mockResolvedValue(room('disabled') as any);
    expect((await post('social', firebaseToken)).status).toBe(200);
    expect((await post('analytics', firebaseToken)).status).toBe(200);
  });

  it('con la sala borrada solo emite analytics (como analytics-ticket); el resto 410 Deleted', async () => {
    vi.mocked(findById).mockResolvedValue(room('deleted') as any);
    expect((await post('analytics', firebaseToken)).status).toBe(200);
    const res = await post('social', firebaseToken);
    expect(res.status).toBe(410);
    expect(res.body).toEqual({ error: 'Deleted' });
  });
});

describe('POST /rooms/:id/tickets/:service · invitado (ADR 0030)', () => {
  it.each(['sync', 'social', 'voice', 'stream'])('emite %s con el sub, nick y jti de la sesión', async (service) => {
    const { token, payload } = issueGuestToken(ROOM_ID, 'Bea');
    const res = await post(service, token);
    expect(res.status).toBe(200);
    expect(res.body.audience).toBe(`tint:${service}`);
    const decoded = jwt.verify(res.body.token, env.JWT_SECRET, {
      algorithms: ['HS256'],
      issuer: env.JWT_ISSUER,
      audience: `tint:${service}`,
    }) as any;
    expect(decoded).toMatchObject({ sub: payload.sub, nick: 'Bea', jti: payload.jti, room_id: ROOM_ID, role: 'participant' });
    expect(decoded.exp).toBeLessThanOrEqual(payload.exp);
    expect(new Date(res.body.expires_at).getTime()).toBe(decoded.exp * 1000);
  });

  it('el exp del ticket no supera el de la sesión', async () => {
    const now = Math.floor(Date.now() / 1000);
    const session = { sub: 'guest:x', room_id: ROOM_ID, role: 'participant', nick: 'Bea', jti: 'jti-corto' };
    const token = jwt.sign({ ...session, exp: now + 20 }, env.JWT_SECRET, { algorithm: 'HS256', issuer: env.JWT_ISSUER, audience: env.JWT_AUDIENCE });
    const res = await post('sync', token);
    expect(res.status).toBe(200);
    expect((jwt.decode(res.body.token) as any).exp).toBe(now + 20);
  });

  it('403 al pedir analytics (AC3) y access.denied sin actor', async () => {
    const { token } = issueGuestToken(ROOM_ID, 'Bea');
    const res = await post('analytics', token);
    expect(res.status).toBe(403);
    expect(res.body.token).toBeUndefined();
    await flush();
    const fields = auditFields();
    expect(fields).toEqual([expect.objectContaining({ action: 'access.denied', outcome: 'denied', room_id: ROOM_ID })]);
    expect(fields[0].actor_id).toBeUndefined();
  });

  it('403 con el token de otra sala (AC3) y access.denied', async () => {
    const { token } = issueGuestToken(OTHER_ROOM, 'Bea');
    const res = await post('sync', token);
    expect(res.status).toBe(403);
    await flush();
    expect(auditFields()).toEqual([expect.objectContaining({ action: 'access.denied', room_id: ROOM_ID })]);
  });

  it('401 si la sesión está revocada (Redis o BD)', async () => {
    const { token, payload } = issueGuestToken(ROOM_ID, 'Bea');
    exists.mockResolvedValueOnce(1);
    expect((await post('sync', token)).status).toBe(401);
    vi.mocked(findByJti).mockResolvedValueOnce({ jti: payload.jti, revoked: true } as any);
    expect((await post('sync', token)).status).toBe(401);
  });

  it('403 BANNED si el dispositivo de la sesión está bloqueado en la sala', async () => {
    const { token, payload } = issueGuestToken(ROOM_ID, 'Bea');
    vi.mocked(findByJti).mockResolvedValue({ jti: payload.jti, revoked: false, deviceId: 'dev-1' } as any);
    vi.mocked(findByRoomAndDevice).mockResolvedValue({ id: 'ban-1' } as any);
    const res = await post('voice', token);
    expect(res.status).toBe(403);
    expect(res.body).toEqual({ error: 'BANNED' });
    expect(findByRoomAndDevice).toHaveBeenCalledWith(ROOM_ID, 'dev-1');
    await flush();
    expect(auditFields()).toEqual([expect.objectContaining({ action: 'access.denied', room_id: ROOM_ID })]);
  });

  it('403 BANNED si el sub está bloqueado (cliente sin deviceId)', async () => {
    const { token, payload } = issueGuestToken(ROOM_ID, 'Bea');
    vi.mocked(findByRoomAndSub).mockResolvedValue({ id: 'ban-1' } as any);
    const res = await post('social', token);
    expect(res.status).toBe(403);
    expect(findByRoomAndSub).toHaveBeenCalledWith(ROOM_ID, payload.sub);
    expect(findByRoomAndDevice).not.toHaveBeenCalled();
  });

  it('503 si no se puede comprobar el bloqueo (falla cerrado)', async () => {
    const { token } = issueGuestToken(ROOM_ID, 'Bea');
    vi.mocked(findByRoomAndSub).mockRejectedValue(new Error('db down'));
    const res = await post('sync', token);
    expect(res.status).toBe(503);
    expect(res.body.token).toBeUndefined();
  });

  it('410 Disabled con la sala deshabilitada', async () => {
    vi.mocked(findById).mockResolvedValue(room('disabled') as any);
    const { token } = issueGuestToken(ROOM_ID, 'Bea');
    const res = await post('sync', token);
    expect(res.status).toBe(410);
    expect(res.body).toEqual({ error: 'Disabled' });
  });

  it('404 con un servicio desconocido', async () => {
    const { token } = issueGuestToken(ROOM_ID, 'Bea');
    expect((await post('api', token)).status).toBe(404);
  });

  it('un ticket de servicio no sirve para pedir otros tickets (401)', async () => {
    const { token } = issueGuestToken(ROOM_ID, 'Bea');
    const sync = (await post('sync', token)).body.token;
    expect((await post('voice', sync)).status).toBe(401);
  });

  it('acepta el guest token legado solo mientras JWT_LEGACY_AUDIENCE tenga valor', async () => {
    const claims = { sub: 'guest:x', room_id: ROOM_ID, role: 'participant', nick: 'Bea', jti: 'jti-legado' };
    const legacy = jwt.sign(claims, env.JWT_SECRET, { algorithm: 'HS256', issuer: env.JWT_ISSUER, audience: 'tint:room', expiresIn: 3600 });
    expect((await post('sync', legacy)).status).toBe(401);
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    const res = await post('sync', legacy);
    expect(res.status).toBe(200);
    expect((jwt.decode(res.body.token) as any).aud).toBe('tint:sync');
  });
});
