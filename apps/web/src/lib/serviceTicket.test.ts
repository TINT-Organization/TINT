import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { classifyTicketError, getServiceTicket } from './serviceTicket';

const mockedFetch = vi.fn();
vi.stubGlobal('fetch', mockedFetch);

const ok = (body: unknown) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });

beforeEach(() => {
  mockedFetch.mockReset();
  localStorage.clear();
});

describe('getServiceTicket (ADR 0030)', () => {
  it('pide el ticket del servicio con POST /rooms/:id/tickets/:service', async () => {
    localStorage.setItem('tint_token', 'firebase');
    mockedFetch.mockResolvedValueOnce(ok({ token: 't', expires_at: '2026-11-23T10:00:00Z', audience: 'tint:sync' }));
    const ticket = await getServiceTicket('r 1', 'sync', 'host');
    expect(ticket).toEqual({ token: 't', expiresAt: '2026-11-23T10:00:00Z', audience: 'tint:sync' });
    const [url, init] = mockedFetch.mock.calls[0] as [string, { method: string; headers: Headers }];
    expect(url).toBe('/api/v1/rooms/r%201/tickets/sync');
    expect(init.method).toBe('POST');
    expect(init.headers.get('Authorization')).toBe('Bearer firebase');
  });

  it('el invitado lo pide con su guest token aunque el navegador conserve una sesión de host', async () => {
    localStorage.setItem('tint_token', 'firebase');
    localStorage.setItem('tint_guest_token', 'guest-jwt');
    mockedFetch.mockResolvedValueOnce(ok({ token: 'g', expires_at: 'x', audience: 'tint:voice' }));
    await getServiceTicket('r1', 'voice', 'guest');
    const [url, init] = mockedFetch.mock.calls[0] as [string, { headers: Headers }];
    expect(url).toBe('/api/v1/rooms/r1/tickets/voice');
    expect(init.headers.get('Authorization')).toBe('Bearer guest-jwt');
  });

  it('por defecto (auto) usa la sesión que haya: el guest token si no hay host', async () => {
    localStorage.setItem('tint_guest_token', 'guest-jwt');
    mockedFetch.mockResolvedValueOnce(ok({ token: 'g', expires_at: 'x', audience: 'tint:social' }));
    await getServiceTicket('r1', 'social');
    const [, init] = mockedFetch.mock.calls[0] as [string, { headers: Headers }];
    expect(init.headers.get('Authorization')).toBe('Bearer guest-jwt');
  });

  it('propaga el ApiError del endpoint (p. ej. 403 del invitado que pide analytics)', async () => {
    localStorage.setItem('tint_guest_token', 'guest-jwt');
    mockedFetch.mockResolvedValueOnce({ ok: false, status: 403, json: () => Promise.resolve({ error: 'Forbidden' }) });
    await expect(getServiceTicket('r1', 'analytics', 'guest')).rejects.toMatchObject({ status: 403 });
  });

  it('una respuesta sin token es un error (no se abre un canal sin credencial)', async () => {
    mockedFetch.mockResolvedValueOnce(ok({}));
    await expect(getServiceTicket('r1', 'sync')).rejects.toThrow();
  });
});

describe('classifyTicketError', () => {
  const api = (status: number, error: string) => new ApiError(status, error, { error });

  it('410 Disabled es la sala deshabilitada para cualquiera (ADR 0019)', () => {
    expect(classifyTicketError(api(410, 'Disabled'), 'host')).toBe('disabled');
    expect(classifyTicketError(api(410, 'Disabled'), 'guest')).toBe('disabled');
  });

  it('404 y 410 Deleted: la sala o el servicio no existen', () => {
    expect(classifyTicketError(api(404, 'Not found'), 'host')).toBe('gone');
    expect(classifyTicketError(api(410, 'Deleted'), 'guest')).toBe('gone');
  });

  it('host: 403 = no es el dueño; 401 se reintenta (apiFetch ya renovó o cerró la sesión)', () => {
    expect(classifyTicketError(api(403, 'Forbidden'), 'host')).toBe('forbidden');
    expect(classifyTicketError(api(401, 'Invalid token'), 'host')).toBeNull();
  });

  it('invitado: 403 o token revocado = retirado; otro 401 = sesión caducada', () => {
    expect(classifyTicketError(api(403, 'Forbidden'), 'guest')).toBe('revoked');
    expect(classifyTicketError(api(401, 'Token revoked'), 'guest')).toBe('revoked');
    expect(classifyTicketError(api(401, 'Invalid token'), 'guest')).toBe('expired');
  });

  it('red y 5xx son transitorios', () => {
    expect(classifyTicketError(new TypeError('Failed to fetch'), 'guest')).toBeNull();
    expect(classifyTicketError(api(503, 'Unavailable'), 'host')).toBeNull();
  });
});
