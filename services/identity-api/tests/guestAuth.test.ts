import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import jwt from 'jsonwebtoken';

vi.mock('../src/modules/guests/guestSessions.repo.js', () => ({
  create: vi.fn(),
  findByJti: vi.fn(),
  listByRoom: vi.fn(),
  setRevoked: vi.fn(),
}));

import { findByJti } from '../src/modules/guests/guestSessions.repo.js';
import { env } from '../src/config/env.js';
import { requireRoomGuest, requireRoomHostOrGuest } from '../src/middlewares/guestAuth.js';
import { setRedisClient } from '../src/modules/tokens/revocation.js';
import { issueGuestToken } from '../src/modules/auth/auth.service.js';

function mockRes() {
  const res: any = {};
  res.status = vi.fn().mockReturnValue(res);
  res.json = vi.fn().mockReturnValue(res);
  return res;
}

async function run(token: string | undefined, roomId = 'room-1') {
  const req: any = { headers: token ? { authorization: `Bearer ${token}` } : {}, params: { id: roomId } };
  const res = mockRes();
  const next = vi.fn();
  await requireRoomGuest(req, res, next);
  return { req, res, next };
}

function setRevokedKeys(...jtis: string[]) {
  setRedisClient({
    exists: vi.fn(async (k: string) => (jtis.some((j) => k === `token:revoked:${j}`) ? 1 : 0)),
  } as any);
}

function sign(claims: Record<string, unknown>, opts: { secret?: string; issuer?: string } = {}) {
  return jwt.sign(claims, opts.secret ?? env.JWT_SECRET, {
    algorithm: 'HS256',
    issuer: opts.issuer ?? env.JWT_ISSUER,
    audience: env.JWT_AUDIENCE,
    expiresIn: 60,
  });
}

beforeEach(() => {
  vi.mocked(findByJti).mockReset();
  vi.mocked(findByJti).mockResolvedValue(null as any);
});
afterEach(() => setRedisClient(null));

describe('requireRoomGuest', () => {
  it('acepta un guest token válido de la sala y expone req.guest', async () => {
    setRevokedKeys();
    const { token, payload } = issueGuestToken('room-1', 'Ana');
    const { req, next } = await run(token);
    expect(next).toHaveBeenCalled();
    expect(req.guest.jti).toBe(payload.jti);
  });

  it('401 sin Authorization', async () => {
    const { res, next } = await run(undefined);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(next).not.toHaveBeenCalled();
  });

  it('401 con firma o emisor inválidos', async () => {
    setRevokedKeys();
    const claims = { room_id: 'room-1', role: 'participant', jti: 'x', sub: 's' };
    expect((await run(sign(claims, { secret: 'otro-secreto' }))).res.status).toHaveBeenCalledWith(401);
    expect((await run(sign(claims, { issuer: 'otro' }))).res.status).toHaveBeenCalledWith(401);
  });

  it('401 si el token no es de participante o le falta jti', async () => {
    setRevokedKeys();
    expect((await run(sign({ room_id: 'room-1', role: 'host', jti: 'x', sub: 's' }))).res.status).toHaveBeenCalledWith(401);
    expect((await run(sign({ room_id: 'room-1', role: 'participant', sub: 's' }))).res.status).toHaveBeenCalledWith(401);
  });

  it('403 si el token es de otra sala', async () => {
    setRevokedKeys();
    const { token } = issueGuestToken('room-2', 'Ana');
    const { res } = await run(token, 'room-1');
    expect(res.status).toHaveBeenCalledWith(403);
  });

  it('401 si el jti está revocado en Redis', async () => {
    const { token, payload } = issueGuestToken('room-1', 'Ana');
    setRevokedKeys(payload.jti);
    const { res, next } = await run(token);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(res.json).toHaveBeenCalledWith({ error: 'Token revoked' });
    expect(next).not.toHaveBeenCalled();
  });

  it('401 si la sesión del invitado está revocada en BD (aunque Redis no tenga la clave)', async () => {
    setRevokedKeys();
    const { token, payload } = issueGuestToken('room-1', 'Ana');
    vi.mocked(findByJti).mockResolvedValueOnce({ jti: payload.jti, roomId: 'room-1', revoked: true } as any);
    const { res, next } = await run(token);
    expect(findByJti).toHaveBeenCalledWith(payload.jti);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(res.json).toHaveBeenCalledWith({ error: 'Token revoked' });
    expect(next).not.toHaveBeenCalled();
  });

  it('acepta si la sesión existe y no está revocada', async () => {
    setRevokedKeys();
    const { token, payload } = issueGuestToken('room-1', 'Ana');
    vi.mocked(findByJti).mockResolvedValueOnce({ jti: payload.jti, roomId: 'room-1', revoked: false } as any);
    const { next } = await run(token);
    expect(next).toHaveBeenCalled();
  });

  it('requireRoomHostOrGuest sin token delega en requireAuthHost → 401', async () => {
    const req: any = { headers: {}, params: { id: 'room-1' } };
    const res = mockRes();
    const next = vi.fn();
    await requireRoomHostOrGuest(req, res, next);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(next).not.toHaveBeenCalled();
  });

  it('ADR 0030: rechaza un ticket de servicio (otra audiencia) como sesión del invitado', async () => {
    setRevokedKeys();
    const claims = { room_id: 'room-1', role: 'participant', jti: 'x', sub: 's' };
    const ticket = jwt.sign(claims, env.JWT_SECRET, { algorithm: 'HS256', issuer: env.JWT_ISSUER, audience: 'tint:sync', expiresIn: 60 });
    expect((await run(ticket)).res.status).toHaveBeenCalledWith(401);
  });

  it('ADR 0030: acepta la audiencia legada solo mientras JWT_LEGACY_AUDIENCE tenga valor', async () => {
    setRevokedKeys();
    const claims = { room_id: 'room-1', role: 'participant', jti: 'x', sub: 's' };
    const legacy = jwt.sign(claims, env.JWT_SECRET, { algorithm: 'HS256', issuer: env.JWT_ISSUER, audience: 'tint:room', expiresIn: 60 });
    expect((await run(legacy)).res.status).toHaveBeenCalledWith(401);
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    try {
      expect((await run(legacy)).next).toHaveBeenCalled();
    } finally {
      env.JWT_LEGACY_AUDIENCE = undefined;
    }
  });

  it('el 403 por otra sala publica access.denied sin actor (el sub del invitado no va a la bitácora)', async () => {
    const xadd = vi.fn(async () => '1-0');
    setRedisClient({ exists: vi.fn(async () => 0), xadd } as any);
    const roomId = '11111111-2222-4333-8444-555555555555';
    const { token } = issueGuestToken('room-2', 'Ana');
    const { res } = await run(token, roomId);
    expect(res.status).toHaveBeenCalledWith(403);
    expect(xadd).toHaveBeenCalledTimes(1);
    const args = (xadd.mock.calls[0] as unknown as string[]).slice(5);
    const fields = Object.fromEntries(args.flatMap((_, i) => (i % 2 === 0 ? [[args[i], args[i + 1]]] : [])));
    expect(fields).toMatchObject({ action: 'access.denied', outcome: 'denied', room_id: roomId });
    expect(fields.actor_id).toBeUndefined();
  });

  it('expone la sesión de BD en req.guestSession', async () => {
    setRevokedKeys();
    const { token, payload } = issueGuestToken('room-1', 'Ana');
    const session = { jti: payload.jti, roomId: 'room-1', revoked: false, deviceId: 'd-1' };
    vi.mocked(findByJti).mockResolvedValueOnce(session as any);
    const { req } = await run(token);
    expect(req.guestSession).toEqual(session);
  });

  it('503 si no se puede comprobar la revocación (falla cerrado)', async () => {
    setRedisClient({ exists: vi.fn().mockRejectedValue(new Error('down')) } as any);
    const { token } = issueGuestToken('room-1', 'Ana');
    const { res, next } = await run(token);
    expect(res.status).toHaveBeenCalledWith(503);
    expect(next).not.toHaveBeenCalled();
  });
});
