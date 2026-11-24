import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useVoiceChannel, type UseVoiceChannelOptions } from './useVoiceChannel';
import { MockWebSocket } from '../test/mockWebSocket';
import { FakePeerConnection, FakeStream, flushAsync } from '../test/fakeWebRtc';
import { apiFetch, ApiError } from '../lib/api';
import { echoTicket } from '../test/serviceTicketMock';

vi.mock('../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/api')>()),
  apiFetch: vi.fn(),
}));

const ana = { peer_id: 'p-ana', user_id: 'guest:ana', nick: 'Ana', role: 'participant', muted: false, host_muted: false };
const host = { peer_id: 'p-host', user_id: 'u-host', nick: 'Host', role: 'host', muted: false, host_muted: false };
const me = { peer_id: 'p-me', user_id: 'guest:me', nick: 'Yo', role: 'participant', muted: false, host_muted: false };

let micStream: FakeStream;
let getUserMedia: ReturnType<typeof vi.fn>;

function options(extra: Partial<UseVoiceChannelOptions> = {}): UseVoiceChannelOptions {
  return {
    role: 'guest',
    baseUrl: 'ws://voz/ws/voice',
    createSocket: (url) => new MockWebSocket(url) as unknown as WebSocket,
    getUserMedia: getUserMedia as unknown as UseVoiceChannelOptions['getUserMedia'],
    createPeerConnection: (c) => new FakePeerConnection(c) as unknown as RTCPeerConnection,
    reconnectBaseMs: 100,
    ...extra,
  };
}

function sentOfType(ws: MockWebSocket, type: string) {
  return ws.sent.map((s) => JSON.parse(String(s))).filter((m) => m.type === type);
}

async function joinWith(hook: { current: ReturnType<typeof useVoiceChannel> }, state: object = {}) {
  await act(async () => {
    await hook.current.join();
  });
  const ws = MockWebSocket.last();
  await act(async () => {
    ws.open();
    ws.receive({ type: 'voice_state', room_id: 'r1', self: me, peers: [host], ice_servers: [{ urls: ['stun:s'] }], max_members: 8, ...state });
    await flushAsync();
  });
  return ws;
}

beforeEach(() => {
  MockWebSocket.reset();
  FakePeerConnection.instances = [];
  localStorage.clear();
  localStorage.setItem('tint_guest_token', 'g-token');
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(echoTicket);
  micStream = new FakeStream();
  getUserMedia = vi.fn(async () => micStream);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useVoiceChannel — unirse (HU-VOZ-01)', () => {
  it('no conecta nada hasta pulsar unirse', () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    expect(result.current.status).toBe('idle');
    expect(MockWebSocket.instances).toHaveLength(0);
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it('pide el micrófono, abre /ws/voice con su ticket voice y ofrece a quien ya estaba', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({ token: 'voice-g', expires_at: 'x', audience: 'tint:voice' });
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    // ADR 0030: el guest token solo sirve para pedir el ticket; no viaja a voice-core.
    expect(apiFetch).toHaveBeenCalledWith('/rooms/r1/tickets/voice', { method: 'POST' }, 'guest');
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    expect(ws.url).toBe('ws://voz/ws/voice?room_id=r1&token=voice-g');
    expect(result.current.status).toBe('connected');
    expect(result.current.self?.peer_id).toBe('p-me');
    expect(result.current.peers).toEqual([host]);
    expect(FakePeerConnection.instances).toHaveLength(1);
    expect(FakePeerConnection.instances[0].config.iceServers).toEqual([{ urls: ['stun:s'] }]);
    const offer = sentOfType(ws, 'signal')[0];
    expect(offer).toMatchObject({ to: 'p-host', data: { description: { type: 'offer' } } });
  });

  it('el host usa un ticket voice nuevo en cada conexión', async () => {
    const fetchTicket = vi.fn(async () => 'ticket-1');
    const { result } = renderHook(() => useVoiceChannel('r1', options({ role: 'host', fetchTicket })));
    await act(async () => {
      await result.current.join();
    });
    expect(fetchTicket).toHaveBeenCalledWith('r1', 'host');
    expect(MockWebSocket.last().url).toContain('token=ticket-1');
  });

  it('si el ticket del host falla, reintenta', async () => {
    vi.useFakeTimers();
    const fetchTicket = vi.fn().mockRejectedValueOnce(new Error('red')).mockResolvedValue('t2');
    const { result } = renderHook(() => useVoiceChannel('r1', options({ role: 'host', fetchTicket })));
    await act(async () => {
      await result.current.join();
    });
    expect(result.current.status).toBe('reconnecting');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(MockWebSocket.last().url).toContain('token=t2');
  });

  it('sin permiso de micrófono no entra y lo explica', async () => {
    getUserMedia.mockRejectedValueOnce(Object.assign(new Error('no'), { name: 'NotAllowedError' }));
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await result.current.join();
    });
    expect(result.current.status).toBe('idle');
    expect(result.current.notice).toMatch(/micrófono/i);
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('sin getUserMedia (navegador viejo o http) lo explica', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options({ getUserMedia: undefined })));
    const original = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true });
    await act(async () => {
      await result.current.join();
    });
    Object.defineProperty(navigator, 'mediaDevices', { value: original, configurable: true });
    expect(result.current.notice).toMatch(/no permite/i);
  });

  it('peer_joined, signal y audio remoto', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    await act(async () => {
      ws.receive({ type: 'peer_joined', peer: ana });
      ws.receive({ type: 'signal', from: 'p-ana', data: { description: { type: 'offer', sdp: 'o' } } });
      await flushAsync();
    });
    expect(result.current.peers.map((p) => p.nick)).toEqual(['Host', 'Ana']);
    expect(sentOfType(ws, 'signal').at(-1)).toMatchObject({ to: 'p-ana', data: { description: { type: 'answer' } } });
    const stream = new FakeStream();
    act(() => FakePeerConnection.instances[1].emitTrack(stream));
    expect(result.current.remoteStreams['p-ana']).toBe(stream);
    act(() => FakePeerConnection.instances[1].setConnectionState('connected'));
    expect(result.current.connection['p-ana']).toBe('connected');
  });

  // AC3: al salir alguien, se cierra su conexión y deja de oírse.
  it('peer_left cierra esa conexión', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => FakePeerConnection.instances[0].emitTrack(new FakeStream()));
    act(() => ws.receive({ type: 'peer_left', peer_id: 'p-host' }));
    expect(result.current.peers).toEqual([]);
    expect(result.current.remoteStreams['p-host']).toBeUndefined();
    expect(FakePeerConnection.instances[0].closed).toBe(true);
  });

  it('salir envía leave, apaga el micrófono y no reconecta', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => result.current.leave());
    expect(sentOfType(ws, 'leave')).toHaveLength(1);
    expect(result.current.status).toBe('idle');
    expect(micStream.tracks[0].stopped).toBe(true);
    expect(FakePeerConnection.instances[0].closed).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('al desmontar libera todo', async () => {
    const { result, unmount } = renderHook(() => useVoiceChannel('r1', options()));
    await joinWith(result);
    unmount();
    expect(micStream.tracks[0].stopped).toBe(true);
    expect(FakePeerConnection.instances[0].closed).toBe(true);
  });
});

describe('useVoiceChannel — cierres', () => {
  // AC2: un bloqueado es rechazado (4001).
  it('4001 termina sin reintentar', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await result.current.join();
    });
    act(() => MockWebSocket.last().close(4001, 'token revoked'));
    expect(result.current.status).toBe('ended');
    expect(result.current.notice).toMatch(/acceso/i);
    expect(micStream.tracks[0].stopped).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('VOICE_FULL + 4008 avisa que está llena', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await result.current.join();
    });
    act(() => {
      MockWebSocket.last().receive({ type: 'error', code: 'VOICE_FULL' });
      MockWebSocket.last().close(4008);
    });
    expect(result.current.status).toBe('ended');
    expect(result.current.notice).toMatch(/llena/i);
  });

  it('4003: host e invitado reconectan con otro ticket una vez; un segundo 4003 seguido termina', async () => {
    vi.useFakeTimers();
    for (const role of ['guest', 'host'] as const) {
      MockWebSocket.reset();
      const fetchTicket = vi.fn(async () => 'tk');
      const hook = renderHook(() => useVoiceChannel('r1', options({ role, fetchTicket })));
      await act(async () => {
        await hook.result.current.join();
      });
      act(() => MockWebSocket.last().close(4003));
      expect(hook.result.current.status).toBe('reconnecting');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(fetchTicket).toHaveBeenCalledTimes(2);
      expect(fetchTicket).toHaveBeenLastCalledWith('r1', role);
      act(() => MockWebSocket.last().close(4003));
      expect(hook.result.current.status).toBe('ended');
      expect(hook.result.current.notice).toMatch(/caducó/i);
    }
  });

  it('el reintento por 4003 se rearma con voice_state', async () => {
    vi.useFakeTimers();
    const fetchTicket = vi.fn(async () => 'tk');
    const { result } = renderHook(() => useVoiceChannel('r1', options({ fetchTicket })));
    await act(async () => {
      await result.current.join();
    });
    act(() => MockWebSocket.last().close(4003));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const ws = MockWebSocket.last();
    await act(async () => {
      ws.open();
      ws.receive({ type: 'voice_state', room_id: 'r1', self: me, peers: [], ice_servers: [], max_members: 8 });
      await flushAsync();
    });
    act(() => MockWebSocket.last().close(4003));
    expect(result.current.status).toBe('reconnecting');
  });

  it('ticket del invitado rechazado: 401 caducó, 403 sin acceso; no reintenta', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new ApiError(401, 'Invalid token'));
    const a = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await a.result.current.join();
    });
    expect(a.result.current.status).toBe('ended');
    expect(a.result.current.notice).toMatch(/caducó/i);
    vi.mocked(apiFetch).mockRejectedValueOnce(new ApiError(403, 'Forbidden'));
    const b = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await b.result.current.join();
    });
    expect(b.result.current.status).toBe('ended');
    expect(b.result.current.notice).toMatch(/ya no tienes acceso/i);
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('una caída reconecta con espera exponencial y rehace la malla', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => ws.close(1006));
    expect(result.current.status).toBe('reconnecting');
    expect(result.current.peers).toEqual([]);
    expect(FakePeerConnection.instances[0].closed).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(99);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    act(() => MockWebSocket.last().close(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(199);
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  // HU-VOZ-05 AC3: al volver la red se reinicia ICE (o se reconecta ya si el socket cayó).
  it('el evento online reinicia ICE', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await joinWith(result);
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    expect(FakePeerConnection.instances[0].restarts).toBe(1);
  });

  it('online con el socket caído reconecta sin esperar', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options({ reconnectBaseMs: 60_000 })));
    const ws = await joinWith(result);
    act(() => ws.close(1006));
    await act(async () => {
      window.dispatchEvent(new Event('online'));
      await flushAsync();
    });
    expect(MockWebSocket.instances).toHaveLength(2);
  });
});

describe('useVoiceChannel — silencios', () => {
  // HU-VOZ-02: silenciado no envía audio; al activar vuelve.
  it('toggleMute quita y devuelve la pista y lo avisa al servidor', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    const sender = FakePeerConnection.instances[0].transceivers[0].sender;
    await act(async () => {
      result.current.toggleMute();
      await flushAsync();
    });
    expect(result.current.muted).toBe(true);
    expect(sender.track).toBeNull();
    expect(sentOfType(ws, 'self_mute')).toMatchObject([{ muted: true }]);
    await act(async () => {
      result.current.toggleMute();
      await flushAsync();
    });
    expect(result.current.muted).toBe(false);
    expect(sender.track).toBe(micStream.tracks[0]);
  });

  it('el silencio propio sobrevive a una reconexión', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => result.current.toggleMute());
    act(() => ws.close(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const ws2 = MockWebSocket.last();
    await act(async () => {
      ws2.open();
      ws2.receive({ type: 'voice_state', room_id: 'r1', self: me, peers: [], ice_servers: [], max_members: 8 });
      await flushAsync();
    });
    expect(sentOfType(ws2, 'self_mute')).toMatchObject([{ muted: true }]);
    expect(result.current.muted).toBe(true);
  });

  it('peer_muted de otro actualiza su icono', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => ws.receive({ type: 'peer_muted', peer_id: 'p-host', user_id: 'u-host', muted: true, host_muted: false }));
    expect(result.current.peers[0].muted).toBe(true);
  });

  // HU-VOZ-04: silenciado por el host no puede activarse.
  it('el silencio del host corta el micrófono y bloquea activarlo', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    const sender = FakePeerConnection.instances[0].transceivers[0].sender;
    await act(async () => {
      ws.receive({ type: 'peer_muted', peer_id: 'p-me', user_id: 'guest:me', muted: true, host_muted: true });
      await flushAsync();
    });
    expect(result.current.hostMuted).toBe(true);
    expect(result.current.muted).toBe(true);
    expect(sender.track).toBeNull();
    act(() => result.current.toggleMute());
    expect(sentOfType(ws, 'self_mute')).toHaveLength(0);
    expect(result.current.notice).toMatch(/host te silenció/i);

    // El host lo levanta: sigue silenciado hasta que se active.
    await act(async () => {
      ws.receive({ type: 'peer_muted', peer_id: 'p-me', user_id: 'guest:me', muted: true, host_muted: false });
      await flushAsync();
    });
    expect(result.current.hostMuted).toBe(false);
    expect(sender.track).toBeNull();
    act(() => result.current.toggleMute());
    expect(sentOfType(ws, 'self_mute')).toMatchObject([{ muted: false }]);
  });

  it('entrar ya silenciado por el host', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await joinWith(result, { self: { ...me, muted: true, host_muted: true } });
    expect(result.current.hostMuted).toBe(true);
    expect(FakePeerConnection.instances[0].transceivers[0].sender.track).toBeNull();
  });

  it('setHostMute envía host_mute y los errores se muestran', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => result.current.setHostMute('guest:ana', true));
    expect(sentOfType(ws, 'host_mute')).toMatchObject([{ user_id: 'guest:ana', muted: true }]);
    act(() => ws.receive({ type: 'error', code: 'FORBIDDEN' }));
    expect(result.current.notice).toMatch(/solo el host/i);
    act(() => result.current.dismissNotice());
    expect(result.current.notice).toBeNull();
  });

  it('sin conexión, silenciar no envía nada', () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    act(() => result.current.toggleMute());
    act(() => result.current.setHostMute('x', true));
    expect(result.current.muted).toBe(true);
  });
});

describe('useVoiceChannel — casos de borde', () => {
  it('sin fetchTicket pide el ticket voice a identity-api', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({ token: 'desde-api' });
    const { result } = renderHook(() => useVoiceChannel('sala 1', options({ role: 'host' })));
    await act(async () => {
      await result.current.join();
    });
    expect(apiFetch).toHaveBeenCalledWith('/rooms/sala%201/tickets/voice', { method: 'POST' }, 'host');
    expect(MockWebSocket.last().url).toContain('token=desde-api');
  });

  it('un ticket rechazado (403) termina sin reintentar', async () => {
    const fetchTicket = vi.fn().mockRejectedValue(new ApiError(403, 'no'));
    const { result } = renderHook(() => useVoiceChannel('r1', options({ role: 'host', fetchTicket })));
    await act(async () => {
      await result.current.join();
    });
    expect(result.current.status).toBe('ended');
    expect(result.current.notice).toMatch(/no se pudo entrar/i);
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('sin micrófono lo dice distinto que sin permiso', async () => {
    getUserMedia.mockRejectedValueOnce(Object.assign(new Error('no'), { name: 'NotFoundError' }));
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await result.current.join();
    });
    expect(result.current.notice).toMatch(/no encontramos un micrófono/i);
  });

  it('unirse dos veces no abre dos conexiones', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await joinWith(result);
    await act(async () => {
      await result.current.join();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  it('un candidato inválido no tumba la voz', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    await act(async () => {
      ws.receive({ type: 'signal', from: 'p-nuevo', data: { candidate: { candidate: 'x' } } });
      ws.receive('basura');
      await flushAsync();
    });
    expect(result.current.status).toBe('connected');
  });

  it('un error del socket lo cierra y reconecta', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    await act(async () => {
      await result.current.join();
    });
    act(() => MockWebSocket.last().onerror?.(new Event('error')));
    expect(result.current.status).toBe('reconnecting');
  });

  it('un cierre de un socket viejo no afecta al nuevo', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    const ws = await joinWith(result);
    act(() => ws.close(1006));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    act(() => ws.onclose?.(new CloseEvent('close', { code: 1006 })));
    expect(MockWebSocket.instances).toHaveLength(2);
  });
});

describe('useVoiceChannel — energía para saber quién habla', () => {
  it('sampleEnergy lee la malla; sin conexión devuelve vacío', async () => {
    const { result } = renderHook(() => useVoiceChannel('r1', options()));
    expect(await result.current.sampleEnergy()).toEqual({});
    await joinWith(result);
    FakePeerConnection.instances[0].stats = [
      { type: 'inbound-rtp', kind: 'audio', totalAudioEnergy: 2, totalSamplesDuration: 4 },
    ];
    expect(await result.current.sampleEnergy()).toEqual({ 'p-host': { energy: 2, duration: 4 } });
  });
});
