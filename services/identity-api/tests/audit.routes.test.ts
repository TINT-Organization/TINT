/**
 * Bitácora de auditoría (Prototipo 3, escenario 5): cada acción sensible que termina bien publica
 * exactamente un evento en `audit:events`; si la acción no ocurre (404, 409, 410, 503) no publica
 * nada; un acceso negado publica `access.denied`. Un fallo de Redis no cambia ninguna respuesta.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import { logger } from '../src/logging.js';

vi.mock('../src/middlewares/auth.js', () => ({
  requireAuthHost: (req: any, _res: any, next: any) => {
    req.user = { id: 'host-1', providerSub: 'firebase-uid-1', email: 'ana@correo.com', displayName: 'Ana Pérez' };
    next();
  },
}));

vi.mock('../src/middlewares/internalToken.js', () => ({
  requireInternalToken: (_req: any, _res: any, next: any) => next(),
}));

vi.mock('../src/modules/rooms/rooms.repo.js', () => ({
  create: vi.fn(),
  listByHost: vi.fn(async () => []),
  findById: vi.fn(),
  isOwner: vi.fn(),
  softRemoveById: vi.fn(),
  updateTitle: vi.fn(),
  setMedia: vi.fn(),
}));

vi.mock('../src/modules/rooms/rooms.create.js', () => ({ createRoomWithInvitation: vi.fn() }));
vi.mock('../src/modules/rooms/rooms.close.js', () => ({ closeLiveRoom: vi.fn() }));

vi.mock('../src/modules/eventStart/eventStart.service.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/modules/eventStart/eventStart.service.js')>()),
  startEvent: vi.fn(),
}));

vi.mock('../src/modules/invitations/invitations.repo.js', () => ({
  create: vi.fn(),
  findByCode: vi.fn(),
  setRevoked: vi.fn(),
  listByRoom: vi.fn(),
  listLatestActiveByRooms: vi.fn(async () => new Map()),
}));

vi.mock('../src/modules/guests/guestSessions.repo.js', () => ({
  create: vi.fn(async (s: unknown) => s),
  findByJti: vi.fn(),
  listByRoom: vi.fn(),
  setRevoked: vi.fn(async () => null),
  listActiveByRoom: vi.fn(async () => []),
  setRevokedMany: vi.fn(async () => []),
  listByRoomAndSub: vi.fn(async () => []),
}));

vi.mock('../src/modules/bans/roomBans.repo.js', () => ({
  create: vi.fn(),
  findByRoomAndDevice: vi.fn(async () => null),
  findByRoomAndSub: vi.fn(async () => null),
  listByRoom: vi.fn(async () => []),
  removeById: vi.fn(),
}));

import app from '../src/app.js';
import { setRedisClient } from '../src/modules/tokens/revocation.js';
import { findById, isOwner, softRemoveById, updateTitle } from '../src/modules/rooms/rooms.repo.js';
import { createRoomWithInvitation } from '../src/modules/rooms/rooms.create.js';
import { closeLiveRoom } from '../src/modules/rooms/rooms.close.js';
import { startEvent } from '../src/modules/eventStart/eventStart.service.js';
import { create as createInvitation, findByCode } from '../src/modules/invitations/invitations.repo.js';
import { findByJti, listByRoomAndSub } from '../src/modules/guests/guestSessions.repo.js';
import { create as createBan, removeById as removeBan } from '../src/modules/bans/roomBans.repo.js';

const ROOM_ID = '3d97d007-29c0-4cec-bb61-cb37683c3256';
const JTI = '33333333-3333-4333-8333-333333333333';
const BAN_ID = '88888888-8888-4888-8888-888888888888';
const DEVICE = '77777777-7777-4777-8777-777777777777';
const future = new Date(Date.now() + 60 * 60 * 1000);

const room = { id: ROOM_ID, hostId: 'host-1', title: 'Noche de cine', status: 'active', kind: 'watch', mediaId: null, mediaKey: null };
const invitation = { id: 'inv-1', code: 'abc123def456', roomId: ROOM_ID, createdBy: 'host-1', expiresAt: future, revoked: false };
const session = {
  jti: JTI,
  roomId: ROOM_ID,
  sub: 'guest:secreto',
  nick: 'Ana',
  joinedAt: new Date(),
  expiresAt: future,
  revoked: false,
  deviceId: DEVICE,
};
const ban = { id: BAN_ID, roomId: ROOM_ID, deviceId: DEVICE, sub: 'guest:secreto', nick: 'Ana', createdAt: new Date(), createdBy: 'host-1' };
const closed = { room_id: ROOM_ID, status: 'disabled' as const, closed_at: new Date().toISOString() };

const xadd = vi.fn(async (..._args: unknown[]) => '1-0');
const redis = {
  xadd,
  set: vi.fn(async () => 'OK'),
  publish: vi.fn(async () => 1),
  exists: vi.fn(async () => 0),
};

/** Eventos publicados en audit:events, como objetos campo → valor. */
function auditEvents() {
  return xadd.mock.calls
    .filter((call) => call[0] === 'audit:events')
    .map((call) => {
      const pairs = call.slice(call.indexOf('*') + 1) as string[];
      const out: Record<string, string> = {};
      for (let i = 0; i < pairs.length; i += 2) out[pairs[i]] = pairs[i + 1];
      return out;
    });
}

const byHost = { outcome: 'ok', actor_id: 'firebase-uid-1', actor_role: 'host', source: 'identity-api' };

type Case = { name: string; action: string; arrange: () => void; send: () => request.Test; status: number };

const cases: Case[] = [
  {
    name: 'POST /rooms',
    action: 'room.create',
    arrange: () => vi.mocked(createRoomWithInvitation).mockResolvedValueOnce({ room, invitation } as any),
    send: () => request(app).post('/api/v1/rooms').send({ title: 'Noche de cine' }),
    status: 200,
  },
  {
    name: 'POST /events/start',
    action: 'room.create',
    arrange: () => vi.mocked(startEvent).mockResolvedValueOnce({ room, invitation, sessions: {} } as any),
    send: () => request(app).post('/api/v1/events/start').send({ title: 'Noche de cine' }),
    status: 200,
  },
  {
    name: 'PATCH /rooms/:id',
    action: 'room.update',
    arrange: () => vi.mocked(updateTitle).mockResolvedValueOnce({ ...room, title: 'Otro' } as any),
    send: () => request(app).patch(`/api/v1/rooms/${ROOM_ID}`).send({ title: 'Otro' }),
    status: 200,
  },
  {
    name: 'DELETE /rooms/:id',
    action: 'room.delete',
    arrange: () => vi.mocked(softRemoveById).mockResolvedValueOnce({ ...room, status: 'deleted' } as any),
    send: () => request(app).delete(`/api/v1/rooms/${ROOM_ID}`),
    status: 200,
  },
  {
    name: 'POST /rooms/:id/end',
    action: 'room.end',
    arrange: () => vi.mocked(closeLiveRoom).mockResolvedValueOnce({ ok: true, body: { ...closed, reason: 'host_ended' } }),
    send: () => request(app).post(`/api/v1/rooms/${ROOM_ID}/end`),
    status: 200,
  },
  {
    name: 'POST /rooms/:id/invitations',
    action: 'invitation.create',
    arrange: () => vi.mocked(createInvitation).mockResolvedValueOnce({ ...invitation } as any),
    send: () => request(app).post(`/api/v1/rooms/${ROOM_ID}/invitations`).send({}),
    status: 200,
  },
  {
    name: 'POST /invitations/:code/revoke',
    action: 'invitation.revoke',
    arrange: () => vi.mocked(findByCode).mockResolvedValueOnce({ ...invitation } as any),
    send: () => request(app).post('/api/v1/invitations/abc123def456/revoke'),
    status: 200,
  },
  {
    name: 'POST /rooms/:id/guests/:jti/revoke',
    action: 'guest.revoke',
    arrange: () => vi.mocked(findByJti).mockResolvedValueOnce({ ...session } as any),
    send: () => request(app).post(`/api/v1/rooms/${ROOM_ID}/guests/${JTI}/revoke`),
    status: 200,
  },
  {
    name: 'POST /rooms/:id/bans',
    action: 'ban.create',
    arrange: () => {
      vi.mocked(listByRoomAndSub).mockResolvedValueOnce([{ ...session }] as any);
      vi.mocked(createBan).mockResolvedValueOnce({ ...ban } as any);
    },
    send: () => request(app).post(`/api/v1/rooms/${ROOM_ID}/bans`).send({ sub: 'guest:secreto' }),
    status: 200,
  },
  {
    name: 'DELETE /rooms/:id/bans/:banId',
    action: 'ban.delete',
    arrange: () => vi.mocked(removeBan).mockResolvedValueOnce({ ...ban } as any),
    send: () => request(app).delete(`/api/v1/rooms/${ROOM_ID}/bans/${BAN_ID}`),
    status: 200,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  xadd.mockImplementation(async () => '1-0');
  vi.mocked(findById).mockResolvedValue(room as any);
  vi.mocked(isOwner).mockResolvedValue(true);
  setRedisClient(redis as any);
});

afterEach(() => {
  setRedisClient(null);
  vi.restoreAllMocks();
});

describe('cada acción sensible publica un evento de auditoría', () => {
  for (const c of cases) {
    it(`${c.name} → ${c.action}`, async () => {
      c.arrange();
      const res = await c.send();
      expect(res.status).toBe(c.status);
      expect(auditEvents()).toEqual([expect.objectContaining({ ...byHost, action: c.action, room_id: ROOM_ID })]);
      const raw = JSON.stringify(xadd.mock.calls.filter((call) => call[0] === 'audit:events'));
      // Ley 1581: ni email, ni nombre, ni nick, ni el sub del invitado.
      for (const personal of ['ana@correo.com', 'Ana', 'guest:secreto', 'host-1']) expect(raw).not.toContain(personal);
    });
  }

  it('POST /internal/rooms/:id/disable → room.disable con actor_role service y sin actor_id', async () => {
    vi.mocked(closeLiveRoom).mockResolvedValueOnce({ ok: true, body: { ...closed, reason: 'grace_expired' } });
    const res = await request(app).post(`/api/v1/internal/rooms/${ROOM_ID}/disable`).send({});
    expect(res.status).toBe(200);
    const events = auditEvents();
    expect(events).toEqual([
      expect.objectContaining({ action: 'room.disable', outcome: 'ok', actor_role: 'service', room_id: ROOM_ID }),
    ]);
    expect(events[0]).not.toHaveProperty('actor_id');
  });

  it('las rutas de lectura no publican nada', async () => {
    await request(app).get('/api/v1/rooms');
    await request(app).get(`/api/v1/rooms/${ROOM_ID}`);
    expect(auditEvents()).toHaveLength(0);
  });
});

describe('si la acción no ocurre, no se publica', () => {
  it('DELETE /rooms/:id sobre una sala ya borrada (410)', async () => {
    vi.mocked(findById).mockResolvedValueOnce({ ...room, status: 'deleted' } as any);
    const res = await request(app).delete(`/api/v1/rooms/${ROOM_ID}`);
    expect(res.status).toBe(410);
    expect(auditEvents()).toHaveLength(0);
  });

  it('PATCH /rooms/:id sobre una sala que no existe (404)', async () => {
    vi.mocked(findById).mockResolvedValueOnce(null as any);
    const res = await request(app).patch(`/api/v1/rooms/${ROOM_ID}`).send({ title: 'Otro' });
    expect(res.status).toBe(404);
    expect(auditEvents()).toHaveLength(0);
  });

  it('POST /rooms/:id/end de una sala watch (409) e interno con 503', async () => {
    vi.mocked(closeLiveRoom)
      .mockResolvedValueOnce({ ok: false, status: 409, body: { error: 'NotLive' } })
      .mockResolvedValueOnce({ ok: false, status: 503, body: { error: 'Service unavailable' } });
    expect((await request(app).post(`/api/v1/rooms/${ROOM_ID}/end`)).status).toBe(409);
    expect((await request(app).post(`/api/v1/internal/rooms/${ROOM_ID}/disable`).send({})).status).toBe(503);
    expect(auditEvents()).toHaveLength(0);
  });

  it('POST /rooms/:id/guests/:jti/revoke de un invitado de otra sala (404)', async () => {
    vi.mocked(findByJti).mockResolvedValueOnce({ ...session, roomId: 'otra' } as any);
    const res = await request(app).post(`/api/v1/rooms/${ROOM_ID}/guests/${JTI}/revoke`);
    expect(res.status).toBe(404);
    expect(auditEvents()).toHaveLength(0);
  });

  it('POST /rooms/:id/bans sin sesiones del invitado (404)', async () => {
    const res = await request(app).post(`/api/v1/rooms/${ROOM_ID}/bans`).send({ sub: 'guest:nadie' });
    expect(res.status).toBe(404);
    expect(auditEvents()).toHaveLength(0);
  });

  it('DELETE /rooms/:id/bans/:banId inexistente (404)', async () => {
    vi.mocked(removeBan).mockResolvedValueOnce(null as any);
    const res = await request(app).delete(`/api/v1/rooms/${ROOM_ID}/bans/${BAN_ID}`);
    expect(res.status).toBe(404);
    expect(auditEvents()).toHaveLength(0);
  });
});

describe('accesos negados', () => {
  it('no dueño de la sala: un solo access.denied y ninguna acción', async () => {
    vi.mocked(isOwner).mockResolvedValueOnce(false);
    const res = await request(app).delete(`/api/v1/rooms/${ROOM_ID}`);
    expect(res.status).toBe(403);
    expect(softRemoveById).not.toHaveBeenCalled();
    expect(auditEvents()).toEqual([
      expect.objectContaining({ action: 'access.denied', outcome: 'denied', actor_id: 'firebase-uid-1', room_id: ROOM_ID }),
    ]);
  });

  it('POST /invitations/:code/revoke de una sala ajena: access.denied con la sala', async () => {
    vi.mocked(findByCode).mockResolvedValueOnce({ ...invitation } as any);
    vi.mocked(findById).mockResolvedValueOnce({ ...room, hostId: 'otro-host' } as any);
    const res = await request(app).post('/api/v1/invitations/abc123def456/revoke');
    expect(res.status).toBe(403);
    expect(auditEvents()).toEqual([
      expect.objectContaining({ action: 'access.denied', outcome: 'denied', actor_id: 'firebase-uid-1', room_id: ROOM_ID }),
    ]);
  });
});

describe('un fallo de Redis no cambia la respuesta', () => {
  const failures = {
    'XADD rechaza': () => xadd.mockRejectedValue(new Error('redis down')),
    'XADD lanza': () =>
      xadd.mockImplementation(() => {
        throw new Error('boom');
      }),
  };

  for (const [name, arrange] of Object.entries(failures)) {
    it(`${name}: todas las acciones responden igual`, async () => {
      vi.spyOn(logger, 'error').mockImplementation(() => {});
      arrange();
      for (const c of cases) {
        c.arrange();
        const res = await c.send();
        expect(res.status, c.name).toBe(c.status);
      }
    });
  }
});
