import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { logger } from '../src/logging.js';

vi.mock('../src/modules/auth/firebaseToken.js', () => ({ verifyFirebaseIdToken: vi.fn() }));
vi.mock('../src/modules/users/users.repo.js', () => ({ upsertByProvider: vi.fn() }));
vi.mock('../src/modules/rooms/rooms.repo.js', () => ({ isOwner: vi.fn() }));

import { requireAuthHost } from '../src/middlewares/auth.js';
import { requireRoomOwner } from '../src/middlewares/requireRoomOwner.js';
import { verifyFirebaseIdToken } from '../src/modules/auth/firebaseToken.js';
import { upsertByProvider } from '../src/modules/users/users.repo.js';
import { isOwner } from '../src/modules/rooms/rooms.repo.js';
import { setRedisClient } from '../src/modules/tokens/revocation.js';

const ROOM_ID = '3d97d007-29c0-4cec-bb61-cb37683c3256';
const xadd = vi.fn(async (..._args: unknown[]) => '1-0');

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

function res() {
  const r: any = {};
  r.status = vi.fn().mockReturnValue(r);
  r.json = vi.fn().mockReturnValue(r);
  return r;
}

const bearer = (params: Record<string, string> = {}) =>
  ({ headers: { authorization: 'Bearer token-secreto' }, params }) as any;

beforeEach(() => {
  vi.clearAllMocks();
  setRedisClient({ xadd } as any);
  vi.spyOn(logger, 'error').mockImplementation(() => {});
});

afterEach(() => {
  setRedisClient(null);
  vi.restoreAllMocks();
});

describe('requireAuthHost publica access.denied en cada 401', () => {
  it('sin cabecera Authorization', async () => {
    const r = res();
    await requireAuthHost({ headers: {}, params: { id: ROOM_ID } } as any, r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(401);
    const events = auditEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ action: 'access.denied', outcome: 'denied', source: 'identity-api' });
    // Sin token no hay actor.
    expect(events[0]).not.toHaveProperty('actor_id');
    expect(events[0]).not.toHaveProperty('actor_role');
  });

  it('token inválido: no guarda el token', async () => {
    vi.mocked(verifyFirebaseIdToken).mockRejectedValueOnce(new Error('invalid signature'));
    const r = res();
    await requireAuthHost(bearer(), r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(401);
    expect(auditEvents()).toEqual([expect.objectContaining({ action: 'access.denied', outcome: 'denied' })]);
    expect(JSON.stringify(xadd.mock.calls)).not.toContain('token-secreto');
  });

  it('error inesperado (401 del catch externo)', async () => {
    vi.mocked(verifyFirebaseIdToken).mockResolvedValueOnce({ sub: 'uid-1' } as any);
    vi.mocked(upsertByProvider).mockResolvedValueOnce(null as any);
    const r = res();
    await requireAuthHost(bearer(), r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(401);
    expect(auditEvents()).toEqual([expect.objectContaining({ action: 'access.denied', outcome: 'denied' })]);
  });

  it('no publica nada si el token es válido', async () => {
    vi.mocked(verifyFirebaseIdToken).mockResolvedValueOnce({ sub: 'uid-1', email: 'ana@correo.com' } as any);
    vi.mocked(upsertByProvider).mockResolvedValueOnce({ id: 'host-1', providerSub: 'uid-1' } as any);
    const next = vi.fn();
    await requireAuthHost(bearer(), res(), next);
    expect(next).toHaveBeenCalled();
    expect(auditEvents()).toHaveLength(0);
  });

  it('no publica nada si falla la BD (500: no es un acceso negado)', async () => {
    vi.mocked(verifyFirebaseIdToken).mockResolvedValueOnce({ sub: 'uid-1' } as any);
    vi.mocked(upsertByProvider).mockRejectedValueOnce(new Error('db down'));
    const r = res();
    await requireAuthHost(bearer(), r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(500);
    expect(auditEvents()).toHaveLength(0);
  });
});

describe('requireRoomOwner publica access.denied en cada 403', () => {
  const user = { id: 'host-2', providerSub: 'firebase-uid-2', email: 'otro@correo.com', displayName: 'Otro' };

  it('no es dueño: actor y sala, sin email', async () => {
    vi.mocked(isOwner).mockResolvedValueOnce(false);
    const r = res();
    await requireRoomOwner({ params: { id: ROOM_ID }, user } as any, r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(403);
    expect(auditEvents()).toEqual([
      expect.objectContaining({
        action: 'access.denied',
        outcome: 'denied',
        actor_id: 'firebase-uid-2',
        actor_role: 'host',
        room_id: ROOM_ID,
      }),
    ]);
    expect(JSON.stringify(xadd.mock.calls)).not.toContain('otro@correo.com');
  });

  it('sin usuario: sin actor', async () => {
    const r = res();
    await requireRoomOwner({ params: { id: ROOM_ID } } as any, r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(403);
    const [event] = auditEvents();
    expect(event).toMatchObject({ action: 'access.denied', room_id: ROOM_ID });
    expect(event).not.toHaveProperty('actor_id');
  });

  it('id que no es UUID (isOwner lanza): 403, sin room_id', async () => {
    vi.mocked(isOwner).mockRejectedValueOnce(Object.assign(new Error('invalid input syntax for type uuid'), { code: '22P02' }));
    const r = res();
    await requireRoomOwner({ params: { id: 'no-es-uuid' }, user } as any, r, vi.fn());
    expect(r.status).toHaveBeenCalledWith(403);
    const [event] = auditEvents();
    expect(event).toMatchObject({ action: 'access.denied', actor_id: 'firebase-uid-2' });
    expect(event).not.toHaveProperty('room_id');
  });

  it('no publica nada si es dueño', async () => {
    vi.mocked(isOwner).mockResolvedValueOnce(true);
    const next = vi.fn();
    await requireRoomOwner({ params: { id: ROOM_ID }, user } as any, res(), next);
    expect(next).toHaveBeenCalled();
    expect(auditEvents()).toHaveLength(0);
  });
});
