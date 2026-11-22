import { describe, it, expect, vi, afterEach } from 'vitest';
import { logger } from '../src/logging.js';
import { setRedisClient } from '../src/modules/tokens/revocation.js';
import { AUDIT_STREAM, auditDenied, auditHostAction, publishAuditEvent } from '../src/modules/audit/audit.events.js';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const ISO_MS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const ROOM_ID = '3d97d007-29c0-4cec-bb61-cb37683c3256';
const host = { id: 'host-1', providerSub: 'firebase-uid-1', email: 'ana@correo.com', displayName: 'Ana Pérez' };

/** Convierte los pares planos de XADD (después de `*`) en un objeto. */
function fieldsOf(call: unknown[]) {
  const star = call.indexOf('*');
  const pairs = call.slice(star + 1) as string[];
  const out: Record<string, string> = {};
  for (let i = 0; i < pairs.length; i += 2) out[pairs[i]] = pairs[i + 1];
  return out;
}

function fakeRedis() {
  const xadd = vi.fn().mockResolvedValue('1-0');
  setRedisClient({ xadd } as any);
  return xadd;
}

afterEach(() => {
  setRedisClient(null);
  vi.restoreAllMocks();
});

describe('audit.events', () => {
  it('XADD audit:events MAXLEN ~ 100000 * con los campos del contrato y nada más', async () => {
    const xadd = fakeRedis();
    await publishAuditEvent({ action: 'room.delete', outcome: 'ok', actorId: 'firebase-uid-1', actorRole: 'host', roomId: ROOM_ID });
    expect(AUDIT_STREAM).toBe('audit:events');
    expect(xadd).toHaveBeenCalledTimes(1);
    const call = xadd.mock.calls[0];
    expect(call.slice(0, 5)).toEqual(['audit:events', 'MAXLEN', '~', '100000', '*']);
    const fields = fieldsOf(call);
    expect(Object.keys(fields).sort()).toEqual(
      ['action', 'actor_id', 'actor_role', 'event_id', 'occurred_at', 'outcome', 'room_id', 'source'].sort(),
    );
    expect(fields).toMatchObject({
      action: 'room.delete',
      outcome: 'ok',
      actor_id: 'firebase-uid-1',
      actor_role: 'host',
      room_id: ROOM_ID,
      source: 'identity-api',
    });
    expect(fields.event_id).toMatch(UUID_RE);
    expect(fields.occurred_at).toMatch(ISO_MS_RE);
    for (const value of call.slice(5)) expect(typeof value).toBe('string');
  });

  it('cada evento lleva un event_id distinto', async () => {
    const xadd = fakeRedis();
    await publishAuditEvent({ action: 'room.update', outcome: 'ok' });
    await publishAuditEvent({ action: 'room.update', outcome: 'ok' });
    expect(fieldsOf(xadd.mock.calls[0]).event_id).not.toBe(fieldsOf(xadd.mock.calls[1]).event_id);
  });

  it('omite los campos opcionales vacíos', async () => {
    const xadd = fakeRedis();
    await publishAuditEvent({ action: 'access.denied', outcome: 'denied', actorId: null, roomId: undefined });
    const fields = fieldsOf(xadd.mock.calls[0]);
    expect(fields).not.toHaveProperty('actor_id');
    expect(fields).not.toHaveProperty('actor_role');
    expect(fields).not.toHaveProperty('room_id');
  });

  it('room_id solo si es un UUID: un id inventado en la URL no llega a la bitácora', async () => {
    const xadd = fakeRedis();
    await publishAuditEvent({ action: 'access.denied', outcome: 'denied', roomId: 'x'.repeat(500) });
    await publishAuditEvent({ action: 'access.denied', outcome: 'denied', roomId: 'ana@correo.com' });
    expect(fieldsOf(xadd.mock.calls[0])).not.toHaveProperty('room_id');
    expect(fieldsOf(xadd.mock.calls[1])).not.toHaveProperty('room_id');
  });

  it('auditHostAction: outcome ok, actor = provider_sub del host, sin email ni nombre', async () => {
    const xadd = fakeRedis();
    await auditHostAction(host, 'ban.create', ROOM_ID);
    const fields = fieldsOf(xadd.mock.calls[0]);
    expect(fields).toMatchObject({ action: 'ban.create', outcome: 'ok', actor_id: 'firebase-uid-1', actor_role: 'host', room_id: ROOM_ID });
    const raw = JSON.stringify(xadd.mock.calls);
    expect(raw).not.toContain('ana@correo.com');
    expect(raw).not.toContain('Ana');
    expect(raw).not.toContain('host-1');
  });

  it('auditDenied: access.denied con outcome denied; con o sin actor', async () => {
    const xadd = fakeRedis();
    await auditDenied({ user: host, roomId: ROOM_ID });
    await auditDenied({});
    expect(fieldsOf(xadd.mock.calls[0])).toMatchObject({
      action: 'access.denied',
      outcome: 'denied',
      actor_id: 'firebase-uid-1',
      actor_role: 'host',
      room_id: ROOM_ID,
    });
    const anonymous = fieldsOf(xadd.mock.calls[1]);
    expect(anonymous).toMatchObject({ action: 'access.denied', outcome: 'denied' });
    expect(anonymous).not.toHaveProperty('actor_id');
    expect(anonymous).not.toHaveProperty('actor_role');
  });

  it('best-effort: si XADD rechaza, no propaga y registra solo la acción', async () => {
    const log = vi.spyOn(logger, 'error').mockImplementation(() => {});
    setRedisClient({ xadd: vi.fn().mockRejectedValue(new Error('redis down')) } as any);
    await expect(auditHostAction(host, 'room.delete', ROOM_ID)).resolves.toBeUndefined();
    expect(log).toHaveBeenCalledTimes(1);
    const raw = JSON.stringify(log.mock.calls);
    expect(raw).toContain('room.delete');
    expect(raw).not.toContain('firebase-uid-1');
    expect(raw).not.toContain(ROOM_ID);
  });

  it('best-effort: si XADD lanza de forma síncrona, tampoco propaga', async () => {
    const log = vi.spyOn(logger, 'error').mockImplementation(() => {});
    setRedisClient({
      xadd: vi.fn(() => {
        throw new Error('boom');
      }),
    } as any);
    await expect(auditDenied({})).resolves.toBeUndefined();
    expect(log).toHaveBeenCalledTimes(1);
  });
});
