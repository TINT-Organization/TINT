import { describe, it, expect, afterEach } from 'vitest';
import jwt from 'jsonwebtoken';
import {
  issueGuestToken,
  generateSyncTicket,
  generateHostTicket,
  issueHostServiceTicket,
  issueGuestServiceTicket,
} from '../src/modules/auth/auth.service.js';
import { env } from '../src/config/env.js';

afterEach(() => {
  env.JWT_LEGACY_AUDIENCE = undefined;
});

describe('issueGuestToken', () => {
  it('emite un guest JWT acotado a la sala con TTL de 4h', () => {
    const { token, payload } = issueGuestToken('room-123', '  Invitado1  ');
    expect(typeof token).toBe('string');
    expect(payload.room_id).toBe('room-123');
    expect(payload.role).toBe('participant');
    expect(payload.nick).toBe('Invitado1');
    expect(payload.iss).toBe(env.JWT_ISSUER);
    expect(payload.sub.startsWith('guest:')).toBe(true);
    expect(payload.exp - payload.iat).toBe(4 * 60 * 60);
  });

  it('el token es verificable con JWT_SECRET', () => {
    const { token } = issueGuestToken('room-123', 'nick');
    const decoded = jwt.verify(token, env.JWT_SECRET) as any;
    expect(decoded.room_id).toBe('room-123');
    expect(decoded.role).toBe('participant');
  });

  it('firma explícitamente con HS256', () => {
    const { token } = issueGuestToken('room-1', 'nick');
    expect(jwt.decode(token, { complete: true })!.header.alg).toBe('HS256');
    expect(() => jwt.verify(token, env.JWT_SECRET, { algorithms: ['HS256'] })).not.toThrow();
  });
});

describe('issueGuestToken · audiencia (ADR 0030)', () => {
  it('sin JWT_LEGACY_AUDIENCE lleva aud tint:api', () => {
    expect(issueGuestToken('room-1', 'Ana').payload.aud).toBe('tint:api');
  });

  it('con JWT_LEGACY_AUDIENCE lleva la audiencia legada (la web vieja lo usa en los WebSocket)', () => {
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    expect(issueGuestToken('room-1', 'Ana').payload.aud).toBe('tint:room');
  });
});

describe('generateSyncTicket', () => {
  const verifyOpts = {
    algorithms: ['HS256'] as jwt.Algorithm[],
    issuer: env.JWT_ISSUER,
    audience: 'tint:sync',
  };
  const input = { userId: 'u', roomId: 'r', nick: 'n', audience: 'tint:sync' };

  it('emite un ticket HS256 de host verificable con secreto, issuer y audience', () => {
    const before = Math.floor(Date.now() / 1000);
    const { token, expiresAt } = generateSyncTicket({ userId: 'user-1', roomId: 'room-9', nick: 'Ana', audience: 'tint:sync' });
    const decoded = jwt.verify(token, env.JWT_SECRET, verifyOpts) as any;
    expect(decoded.sub).toBe('user-1');
    expect(decoded.room_id).toBe('room-9');
    expect(decoded.role).toBe('host');
    expect(decoded.nick).toBe('Ana');
    expect(typeof decoded.jti).toBe('string');
    expect(env.SYNC_TICKET_TTL_SECONDS).toBe(600);
    expect(decoded.exp - decoded.iat).toBe(600);
    expect(Math.abs(decoded.exp - (before + 600))).toBeLessThanOrEqual(2);
    expect(expiresAt).toBeInstanceOf(Date);
    expect(expiresAt.getTime()).toBe(decoded.exp * 1000);
  });

  it('declara alg HS256 en el header', () => {
    const { token } = generateSyncTicket(input);
    expect(jwt.decode(token, { complete: true })!.header.alg).toBe('HS256');
  });

  it('usa "Host" como nick si viene vacío o nulo', () => {
    const { token } = generateSyncTicket({ ...input, nick: '  ' });
    expect((jwt.decode(token) as any).nick).toBe('Host');
    const { token: t2 } = generateSyncTicket({ ...input, nick: null });
    expect((jwt.decode(t2) as any).nick).toBe('Host');
  });

  it('genera un jti único por ticket', () => {
    const a = jwt.decode(generateSyncTicket(input).token) as any;
    const b = jwt.decode(generateSyncTicket(input).token) as any;
    expect(a.jti).not.toBe(b.jti);
  });
});

describe('generateHostTicket', () => {
  it('emite la audiencia pedida con el TTL del sync-ticket', () => {
    const { token, expiresAt } = generateHostTicket({ userId: 'user-1', roomId: 'room-9', nick: 'Ana', audience: 'tint:analytics' });
    const decoded = jwt.verify(token, env.JWT_SECRET, {
      algorithms: ['HS256'],
      issuer: env.JWT_ISSUER,
      audience: 'tint:analytics',
    }) as any;
    expect(decoded).toMatchObject({ sub: 'user-1', room_id: 'room-9', role: 'host', nick: 'Ana' });
    expect(decoded.exp - decoded.iat).toBe(env.SYNC_TICKET_TTL_SECONDS);
    expect(expiresAt.getTime()).toBe(decoded.exp * 1000);
  });

  it('generateSyncTicket es un alias de generateHostTicket', () => {
    expect(generateSyncTicket).toBe(generateHostTicket);
  });
});

describe('issueHostServiceTicket (ADR 0030)', () => {
  it('emite un ticket de host con aud tint:<servicio> y TTL SERVICE_TICKET_TTL_SECONDS', () => {
    const { token, expiresAt, audience } = issueHostServiceTicket({ userId: 'user-1', roomId: 'room-9', nick: 'Ana' }, 'voice');
    expect(audience).toBe('tint:voice');
    const decoded = jwt.verify(token, env.JWT_SECRET, { algorithms: ['HS256'], issuer: env.JWT_ISSUER, audience: 'tint:voice' }) as any;
    expect(decoded).toMatchObject({ sub: 'user-1', room_id: 'room-9', role: 'host', nick: 'Ana', aud: 'tint:voice' });
    expect(typeof decoded.jti).toBe('string');
    expect(decoded.exp - decoded.iat).toBe(env.SERVICE_TICKET_TTL_SECONDS);
    expect(expiresAt.getTime()).toBe(decoded.exp * 1000);
  });

  it('nunca emite la audiencia legada, aunque JWT_LEGACY_AUDIENCE tenga valor', () => {
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    const { token } = issueHostServiceTicket({ userId: 'u', roomId: 'r', nick: 'n' }, 'sync');
    expect((jwt.decode(token) as any).aud).toBe('tint:sync');
  });
});

describe('issueGuestServiceTicket (ADR 0030)', () => {
  it('conserva sub, nick, room_id y jti de la sesión y lleva aud tint:<servicio>', () => {
    const { payload } = issueGuestToken('room-1', 'Ana');
    const { token, audience, expiresAt } = issueGuestServiceTicket(payload, 'social');
    expect(audience).toBe('tint:social');
    const decoded = jwt.verify(token, env.JWT_SECRET, { algorithms: ['HS256'], issuer: env.JWT_ISSUER, audience: 'tint:social' }) as any;
    expect(decoded).toMatchObject({ sub: payload.sub, nick: 'Ana', room_id: 'room-1', jti: payload.jti, role: 'participant' });
    expect(decoded.exp - decoded.iat).toBe(env.SERVICE_TICKET_TTL_SECONDS);
    expect(expiresAt.getTime()).toBe(decoded.exp * 1000);
  });

  it('el exp nunca supera el de la sesión del invitado', () => {
    const { payload } = issueGuestToken('room-1', 'Ana');
    const now = Math.floor(Date.now() / 1000);
    const session = { ...payload, exp: now + 30 };
    const decoded = jwt.decode(issueGuestServiceTicket(session, 'sync').token) as any;
    expect(decoded.exp).toBe(session.exp);
  });
});
