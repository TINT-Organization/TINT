import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import jwt from 'jsonwebtoken';
import { centralTokenCheck } from '../src/middlewares/centralAuth.js';
import { env } from '../src/config/env.js';

vi.mock('../src/modules/tokens/revocation.js', () => ({
  isTokenRevoked: vi.fn(async () => false),
}));

// El ID token de Firebase lo verifica requireAuthHost; aquí solo importa a qué rutas llega.
vi.mock('../src/middlewares/auth.js', () => ({
  requireAuthHost: vi.fn((req: any, _res: any, next: any) => {
    req.user = { id: 'host-1', providerSub: 'fb-sub-1' };
    next();
  }),
}));

function mockResponse() {
  const response: any = {};
  response.status = vi.fn().mockReturnValue(response);
  response.json = vi.fn().mockReturnValue(response);
  response.set = vi.fn().mockReturnValue(response);
  response.end = vi.fn().mockReturnValue(response);
  return response;
}

function requestWithToken(token: string) {
  return { headers: { authorization: `Bearer ${token}` } } as any;
}

describe('centralTokenCheck', () => {
  it('returns the verified identity for a valid gateway token', async () => {
    const token = jwt.sign(
      { sub: 'guest:1', role: 'participant', room_id: 'room-1', nick: 'Ada', jti: 'jti-1' },
      env.JWT_SECRET,
      { issuer: env.JWT_ISSUER, audience: env.JWT_AUDIENCE, expiresIn: '5m' },
    );
    const response = mockResponse();

    await centralTokenCheck(requestWithToken(token), response, vi.fn());

    expect(response.status).toHaveBeenCalledWith(204);
    expect(response.set).toHaveBeenCalledWith({
      'X-User-Id': 'guest:1',
      'X-User-Sub': 'guest:1',
      'X-User-Role': 'participant',
      'X-User-Room-Id': 'room-1',
      'X-User-Nick': 'Ada',
    });
  });

  it('rejects an expired token with 401', async () => {
    const token = jwt.sign({ sub: 'guest:1' }, env.JWT_SECRET, {
      issuer: env.JWT_ISSUER,
      audience: env.JWT_AUDIENCE,
      expiresIn: -1,
    });
    const response = mockResponse();

    await centralTokenCheck(requestWithToken(token), response, vi.fn());

    expect(response.status).toHaveBeenCalledWith(401);
    expect(response.json).toHaveBeenCalledWith({ error: 'Invalid token' });
  });
});
// WebSocket por el gateway: el navegador no puede mandar Authorization en el handshake, así que la
// web pone el token en `?token=` y Caddy (forward_auth) lo reenvía en X-Forwarded-Uri.
function wsRequest(uri: string, extra: Record<string, string> = {}) {
  return { headers: { 'x-forwarded-uri': uri, ...extra } } as any;
}

function roomToken(
  claims: Record<string, unknown> = {},
  expiresIn: string | number = '5m',
  secret = env.JWT_SECRET,
  audience: string = env.JWT_AUDIENCE,
) {
  return jwt.sign(
    { sub: 'guest:1', role: 'participant', room_id: 'room-1', nick: 'Ada', jti: 'jti-1', ...claims },
    secret,
    { issuer: env.JWT_ISSUER, audience, expiresIn: expiresIn as any },
  );
}

const ticket = (audience: string, expiresIn: string | number = '5m') => roomToken({}, expiresIn, env.JWT_SECRET, audience);

describe('centralTokenCheck · WebSocket handshakes', () => {
  it('accepts the room token from the ?token= query of a /ws/* upgrade', async () => {
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/ws/sync?room_id=room-1&token=${ticket('tint:sync')}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(204);
    expect(response.set).toHaveBeenCalledWith(expect.objectContaining({ 'X-User-Room-Id': 'room-1', 'X-User-Role': 'participant' }));
  });

  it('lets an expired but genuine token through so the service can close with 4003', async () => {
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/ws/social?token=${ticket('tint:social', -1)}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(204);
  });

  it('lets a revoked token through so the service can close with 4001', async () => {
    const { isTokenRevoked } = await import('../src/modules/tokens/revocation.js');
    vi.mocked(isTokenRevoked).mockResolvedValueOnce(true);
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/ws/voice?token=${ticket('tint:voice')}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(204);
  });

  it('rejects a /ws/* token with a forged signature', async () => {
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/ws/sync?token=${roomToken({}, '5m', 'not-the-secret', 'tint:sync')}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(401);
  });

  it('rejects an unsigned (alg none) token on /ws/*', async () => {
    const forged = `${Buffer.from('{"alg":"none","typ":"JWT"}').toString('base64url')}.${Buffer.from('{"sub":"x"}').toString('base64url')}.`;
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/ws/sync?token=${forged}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(401);
  });

  it('rejects a /ws/* handshake without token', async () => {
    const response = mockResponse();
    await centralTokenCheck(wsRequest('/ws/sync?room_id=room-1'), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(401);
  });

  it('ignores ?token= outside /ws/* (REST must use Authorization)', async () => {
    const response = mockResponse();
    await centralTokenCheck(wsRequest(`/api/v1/rooms?token=${roomToken()}`), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(401);
  });

  it('still rejects an expired Bearer token on REST', async () => {
    const response = mockResponse();
    await centralTokenCheck(
      wsRequest('/api/v1/rooms/room-1/playback', { authorization: `Bearer ${roomToken({}, -1)}` }),
      response,
      vi.fn(),
    );
    expect(response.status).toHaveBeenCalledWith(401);
  });
});

// ADR 0030: la audiencia esperada depende de la ruta original (X-Forwarded-Uri).
describe('centralTokenCheck · audiencia por ruta (ADR 0030)', () => {
  const firebaseToken = () =>
    `${Buffer.from('{"alg":"RS256","typ":"JWT"}').toString('base64url')}.${Buffer.from('{"sub":"fb-sub-1"}').toString('base64url')}.firma`;

  async function check(uri: string, token: string, viaQuery = false) {
    const response = mockResponse();
    const headers: Record<string, string> = { 'x-forwarded-uri': viaQuery ? `${uri}?token=${token}` : uri };
    if (!viaQuery) headers.authorization = `Bearer ${token}`;
    await centralTokenCheck({ headers } as any, response, vi.fn());
    return response.status.mock.calls[0][0];
  }

  beforeEach(async () => {
    // En /ws/* la revocación no se consulta: un mockResolvedValueOnce de otra prueba quedaría pendiente.
    const { isTokenRevoked } = await import('../src/modules/tokens/revocation.js');
    vi.mocked(isTokenRevoked).mockReset();
    vi.mocked(isTokenRevoked).mockResolvedValue(false);
  });

  afterEach(() => {
    env.JWT_LEGACY_AUDIENCE = undefined;
  });

  const routes: Array<[string, string, boolean]> = [
    ['/ws/sync', 'tint:sync', true],
    ['/ws/social', 'tint:social', true],
    ['/ws/voice', 'tint:voice', true],
    ['/api/v1/social/rooms/room-1/chat', 'tint:social', false],
    ['/api/v1/voice/rooms/room-1/mutes/u', 'tint:voice', false],
    ['/api/v1/stream/whep/room-1', 'tint:stream', false],
    ['/api/v1/rooms/room-1/kpis', 'tint:analytics', false],
    ['/api/v1/rooms/room-1/reactions/heatmap', 'tint:analytics', false],
    ['/api/v1/rooms/room-1/timeline', 'tint:analytics', false],
    ['/api/v1/rooms/room-1/playback', 'tint:api', false],
  ];

  it.each(routes)('%s acepta su audiencia (%s)', async (uri, audience, viaQuery) => {
    expect(await check(uri, ticket(audience), viaQuery)).toBe(204);
  });

  it.each(routes)('%s rechaza con 401 un HS256 de cualquier otra audiencia (AC2)', async (uri, audience, viaQuery) => {
    const others = ['tint:api', 'tint:sync', 'tint:social', 'tint:voice', 'tint:stream', 'tint:analytics', 'tint:room'].filter(
      (a) => a !== audience,
    );
    for (const other of others) {
      expect(await check(uri, ticket(other), viaQuery)).toBe(401);
    }
  });

  it('acepta la audiencia legada en cualquier ruta solo mientras JWT_LEGACY_AUDIENCE tenga valor (AC5)', async () => {
    expect(await check('/ws/sync', ticket('tint:room'), true)).toBe(401);
    expect(await check('/api/v1/voice/rooms/room-1/mutes/u', ticket('tint:room'))).toBe(401);
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    expect(await check('/ws/sync', ticket('tint:room'), true)).toBe(204);
    expect(await check('/api/v1/voice/rooms/room-1/mutes/u', ticket('tint:room'))).toBe(204);
    expect(await check('/api/v1/rooms/room-1/playback', ticket('tint:room'))).toBe(204);
  });

  it('un /ws/* que no es de ningún servicio se rechaza (falla cerrado)', async () => {
    expect(await check('/ws/otro', ticket('tint:api'), true)).toBe(401);
  });

  it('sin X-Forwarded-Uri espera la audiencia de identity-api', async () => {
    const response = mockResponse();
    await centralTokenCheck(requestWithToken(ticket('tint:sync')), response, vi.fn());
    expect(response.status).toHaveBeenCalledWith(401);
  });

  it('el ID token de Firebase solo pasa hacia rutas de identity-api', async () => {
    const { requireAuthHost } = await import('../src/middlewares/auth.js');
    vi.mocked(requireAuthHost).mockClear();
    expect(await check('/api/v1/rooms/room-1', firebaseToken())).toBe(204);
    expect(requireAuthHost).toHaveBeenCalledTimes(1);
    for (const uri of ['/api/v1/voice/rooms/room-1/mutes/u', '/api/v1/stream/whip/room-1', '/api/v1/social/rooms/room-1/chat', '/api/v1/rooms/room-1/kpis']) {
      expect(await check(uri, firebaseToken())).toBe(401);
    }
    expect(requireAuthHost).toHaveBeenCalledTimes(1);
  });
});
