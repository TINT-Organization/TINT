import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiFetch } from '../lib/api';
import { useSocialSocket } from './useSocialSocket';
import { flushTicket } from '../test/serviceTicketMock';

vi.mock('../lib/api', async (orig) => ({
  ...(await orig<typeof import('../lib/api')>()),
  apiFetch: vi.fn(),
}));
const mockedApiFetch = vi.mocked(apiFetch);

class FakeSocket {
  static instances: FakeSocket[] = [];
  url: string;
  readyState = 0;
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1006 });
  }
}

const create = (url: string) => new FakeSocket(url) as unknown as WebSocket;
const last = () => FakeSocket.instances[FakeSocket.instances.length - 1];

const flush = flushTicket;

beforeEach(() => {
  FakeSocket.instances = [];
  localStorage.clear();
  localStorage.setItem('tint_token', 'firebase-id-token');
  mockedApiFetch.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useSocialSocket — ticket social (ADR 0030)', () => {
  it('el host abre el canal con un ticket social (no con el ID token de Firebase)', async () => {
    mockedApiFetch.mockResolvedValue({ token: 'ticket-1', expiresAt: 'x' });
    renderHook(() => useSocialSocket('r 1', { createSocket: create, role: 'host' }));
    expect(FakeSocket.instances).toHaveLength(0);
    await flush();
    expect(mockedApiFetch).toHaveBeenCalledWith('/rooms/r%201/tickets/social', { method: 'POST' }, 'host');
    expect(last().url).toContain('token=ticket-1');
    expect(last().url).not.toContain('firebase');
  });

  it('pide un ticket nuevo en cada reconexión', async () => {
    mockedApiFetch.mockResolvedValueOnce({ token: 't1' }).mockResolvedValueOnce({ token: 't2' });
    const { result } = renderHook(() =>
      useSocialSocket('r1', { createSocket: create, role: 'host', reconnectBaseMs: 100 }),
    );
    await flush();
    act(() => last().open());
    act(() => last().close());
    expect(result.current.status).toBe('reconnecting');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(mockedApiFetch).toHaveBeenCalledTimes(2);
    expect(last().url).toContain('token=t2');
  });

  it('si el ticket falla reintenta con backoff', async () => {
    mockedApiFetch.mockRejectedValueOnce(new Error('red')).mockResolvedValueOnce({ token: 't2' });
    const { result } = renderHook(() =>
      useSocialSocket('r1', { createSocket: create, role: 'host', reconnectBaseMs: 100 }),
    );
    await flush();
    expect(result.current.status).toBe('reconnecting');
    expect(FakeSocket.instances).toHaveLength(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(last().url).toContain('token=t2');
  });

  it.each([403, 404, 410])('ticket con %i: deja de reintentar', async (status) => {
    mockedApiFetch.mockRejectedValue(new ApiError(status, 'no'));
    const { result } = renderHook(() =>
      useSocialSocket('r1', { createSocket: create, role: 'host', reconnectBaseMs: 100 }),
    );
    await flush();
    expect(result.current.status).toBe('closed');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
  });

  it('un ticket que llega tras desmontar no abre socket', async () => {
    let resolve: (v: unknown) => void = () => {};
    mockedApiFetch.mockReturnValue(new Promise((r) => (resolve = r)));
    const { unmount } = renderHook(() => useSocialSocket('r1', { createSocket: create, role: 'host' }));
    unmount();
    resolve({ token: 't' });
    await flush();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it('el invitado pide su ticket social con el guest token aunque haya restos de sesión de host', async () => {
    localStorage.setItem('tint_guest_token', 'g1');
    mockedApiFetch.mockResolvedValue({ token: 'social-g1', expires_at: 'x', audience: 'tint:social' });
    renderHook(() => useSocialSocket('r1', { createSocket: create, role: 'guest' }));
    await flush();
    expect(mockedApiFetch).toHaveBeenCalledWith('/rooms/r1/tickets/social', { method: 'POST' }, 'guest');
    expect(last().url).toContain('token=social-g1');
    expect(last().url).not.toContain('g1&');
    expect(last().url).not.toContain('firebase');
  });

  it('cierre 4003: el invitado pide un ticket nuevo una vez; un segundo 4003 seguido es terminal', async () => {
    localStorage.setItem('tint_guest_token', 'g1');
    mockedApiFetch.mockResolvedValueOnce({ token: 's1' }).mockResolvedValueOnce({ token: 's2' });
    const { result } = renderHook(() =>
      useSocialSocket('r1', { createSocket: create, role: 'guest', reconnectBaseMs: 100 }),
    );
    await flush();
    act(() => last().open());
    act(() => last().onclose?.({ code: 4003 }));
    expect(result.current.expired).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    await flush();
    expect(mockedApiFetch).toHaveBeenCalledTimes(2);
    expect(last().url).toContain('token=s2');
    act(() => last().open());
    act(() => last().onclose?.({ code: 4003 }));
    expect(result.current.expired).toBe(true);
    expect(result.current.status).toBe('closed');
    expect(localStorage.getItem('tint_guest_token')).toBeNull();
  });

  it('el reintento por 4003 se rearma al recibir el historial', async () => {
    mockedApiFetch.mockResolvedValue({ token: 't' });
    const { result } = renderHook(() =>
      useSocialSocket('r1', { createSocket: create, role: 'host', reconnectBaseMs: 100 }),
    );
    await flush();
    act(() => last().open());
    act(() => last().onclose?.({ code: 4003 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    await flush();
    act(() => last().open());
    act(() => last().onmessage?.({ data: JSON.stringify({ type: 'history', messages: [] }) }));
    act(() => last().onclose?.({ code: 4003 }));
    expect(result.current.expired).toBe(false);
    expect(result.current.status).toBe('reconnecting');
  });

  it('ticket del invitado con 401: expired y borra el guest token; con 403: revoked', async () => {
    localStorage.setItem('tint_guest_token', 'g1');
    mockedApiFetch.mockRejectedValueOnce(new ApiError(401, 'Invalid token'));
    const a = renderHook(() => useSocialSocket('r1', { createSocket: create, role: 'guest' }));
    await flush();
    expect(a.result.current.expired).toBe(true);
    expect(a.result.current.status).toBe('closed');
    expect(localStorage.getItem('tint_guest_token')).toBeNull();
    localStorage.setItem('tint_guest_token', 'g1');
    mockedApiFetch.mockRejectedValueOnce(new ApiError(403, 'Forbidden'));
    const b = renderHook(() => useSocialSocket('r2', { createSocket: create, role: 'guest' }));
    await flush();
    expect(b.result.current.revoked).toBe(true);
    expect(b.result.current.expired).toBe(false);
    expect(FakeSocket.instances).toHaveLength(0);
  });
});
