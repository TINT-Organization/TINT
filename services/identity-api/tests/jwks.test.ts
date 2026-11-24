import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';

type Callback = (err: Error | null, key: unknown) => void;

const state = vi.hoisted(() => ({
  calls: [] as string[],
  behavior: (() => 'pem-key') as (kid: string, attempt: number) => Error | string,
  clientOptions: [] as Record<string, unknown>[],
  warn: vi.fn(),
}));

vi.mock('jwks-rsa', () => ({
  default: (options: Record<string, unknown>) => {
    state.clientOptions.push(options);
    return {
      getSigningKey: (kid: string, cb: Callback) => {
        state.calls.push(kid);
        const result = state.behavior(kid, state.calls.filter((c) => c === kid).length);
        if (result instanceof Error) cb(result, null);
        else cb(null, { getPublicKey: () => result });
      },
    };
  },
}));

vi.mock('../src/logging.js', () => ({ logger: { warn: (...args: unknown[]) => state.warn(...args), info: vi.fn(), error: vi.fn(), debug: vi.fn() } }));

import { getSigningKey, resetJwksStateForTests, STALE_KEY_MAX_AGE_MS } from '../src/modules/auth/jwks.js';

const notFound = () => Object.assign(new Error('Unable to find a signing key that matches'), { name: 'SigningKeyNotFoundError' });
const timeout = () => Object.assign(new Error('connect timeout'), { name: 'JwksError' });

async function settle<T>(promise: Promise<T>): Promise<T> {
  promise.catch(() => undefined); // el rechazo se comprueba abajo; evita "unhandled" mientras corren los temporizadores
  await vi.runAllTimersAsync();
  return promise;
}

describe('getSigningKey', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    state.calls.length = 0;
    state.warn.mockClear();
    state.behavior = () => 'pem-key';
    resetJwksStateForTests();
  });
  afterEach(() => vi.useRealTimers());

  it('resuelve la clave pública si el kid existe', async () => {
    await expect(settle(getSigningKey('good-kid'))).resolves.toBe('pem-key');
    expect(state.calls).toHaveLength(1);
  });

  it('rechaza sin reintentar si el kid no existe en el JWKS', async () => {
    state.behavior = () => notFound();
    const result = settle(getSigningKey('bad-kid'));
    await expect(result).rejects.toThrow(/signing key/);
    expect(state.calls).toHaveLength(1);
  });

  it('reintenta ante un fallo de red y resuelve si un intento posterior funciona', async () => {
    state.behavior = (_kid, attempt) => (attempt < 3 ? timeout() : 'pem-key');
    await expect(settle(getSigningKey('kid-a'))).resolves.toBe('pem-key');
    expect(state.calls).toHaveLength(3);
  });

  it('rechaza (falla cerrado) si agota los intentos y nunca vio esa clave', async () => {
    state.behavior = () => timeout();
    const result = settle(getSigningKey('kid-nueva'));
    await expect(result).rejects.toThrow('connect timeout');
    expect(state.calls).toHaveLength(3);
  });

  it('usa la última clave buena si Google no responde y la dejó registrada', async () => {
    await settle(getSigningKey('kid-a'));
    state.behavior = () => timeout();
    vi.setSystemTime(Date.now() + 60 * 60 * 1000);

    await expect(settle(getSigningKey('kid-a'))).resolves.toBe('pem-key');
    expect(state.warn).toHaveBeenCalledWith(expect.stringContaining('jwks'), expect.objectContaining({ kid: 'kid-a' }));
  });

  it('no usa una clave vieja pasado el límite de antigüedad', async () => {
    await settle(getSigningKey('kid-a'));
    state.behavior = () => timeout();
    vi.setSystemTime(Date.now() + STALE_KEY_MAX_AGE_MS + 1000);

    await expect(settle(getSigningKey('kid-a'))).rejects.toThrow('connect timeout');
  });

  it('no usa la copia vieja cuando Google responde que el kid ya no existe', async () => {
    await settle(getSigningKey('kid-a'));
    state.behavior = () => notFound();

    await expect(settle(getSigningKey('kid-a'))).rejects.toThrow(/signing key/);
  });

  it('configura el cliente con timeout corto y una caché larga', () => {
    const options = state.clientOptions[0];
    expect(options.timeout).toBe(5000);
    expect(options.cache).toBe(true);
    expect(options.cacheMaxAge).toBeGreaterThanOrEqual(60 * 60 * 1000);
    expect(options.jwksRequestsPerMinute).toBeGreaterThanOrEqual(10);
  });
});
