import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSocialSocket } from './useSocialSocket';
import { echoTicket, flushTicket } from '../test/serviceTicketMock';

vi.mock('../lib/api', async (orig) => ({
  ...(await orig<typeof import('../lib/api')>()),
  apiFetch: vi.fn((...args: Parameters<typeof echoTicket>) => echoTicket(...args)),
}));

class FakeSocket {
  static instances: FakeSocket[] = [];
  url: string;
  readyState = 0; // CONNECTING
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  open() {
    this.readyState = 1; // OPEN
    this.onopen?.({});
  }
  receive(data: string) {
    this.onmessage?.({ data });
  }
  send(data: string) {
    this.sent.push(data);
  }
  close(code = 1006) {
    this.readyState = 3;
    this.closed = true;
    this.onclose?.({ code });
  }
  fail() {
    this.onerror?.();
  }
}

beforeEach(() => {
  FakeSocket.instances = [];
  localStorage.clear();
  vi.useFakeTimers();
});

function lastSocket() {
  return FakeSocket.instances[FakeSocket.instances.length - 1];
}

describe('useSocialSocket — URL del canal', () => {
  const create = (url: string) => new FakeSocket(url) as unknown as WebSocket;

  it('usa VITE_SOCIAL_WS_URL si está definida', async () => {
    vi.stubEnv('VITE_SOCIAL_WS_URL', 'wss://social.test/ws/social');
    renderHook(() => useSocialSocket('r1', { createSocket: create }));
    await flushTicket();
    expect(lastSocket().url.startsWith('wss://social.test/ws/social')).toBe(true);
    vi.unstubAllEnvs();
  });

  it('por defecto usa la ruta pública del gateway', async () => {
    vi.stubEnv('VITE_SOCIAL_WS_URL', '');
    renderHook(() => useSocialSocket('r1', { createSocket: create }));
    await flushTicket();
    expect(lastSocket().url.startsWith('ws://localhost:3000/ws/social')).toBe(true);
    vi.unstubAllEnvs();
  });
});

describe('useSocialSocket', () => {
  it('conecta con room y token, carga historial y recibe mensajes', async () => {
    localStorage.setItem('tint_guest_token', 'g123');
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
        reconnectBaseMs: 500,
        reconnectMaxMs: 8000,
      }),
    );
    await flushTicket();
    expect(result.current.status).toBe('connecting');
    expect(lastSocket().url).toContain('room_id=r1');
    expect(lastSocket().url).toContain('token=g123');

    act(() => {
      lastSocket().open();
      lastSocket().receive(
        '{"type":"history","room_id":"r1","messages":[{"event_id":"h1","type":"chat_message","room_id":"r1","user_id":"u","nick":"A","text":"previo","occurred_at":"t","received_at":"t"}]}',
      );
    });
    expect(result.current.status).toBe('open');
    expect(result.current.messages).toHaveLength(1);

    act(() => {
      lastSocket().receive(
        '{"type":"chat_message","event_id":"m2","room_id":"r1","user_id":"u2","nick":"B","text":"hola","occurred_at":"t","received_at":"t"}',
      );
    });
    expect(result.current.messages.map((m) => m.text)).toEqual(['previo', 'hola']);
  });

  it('agrega los avisos de presencia a la lista', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    act(() => {
      lastSocket().open();
      lastSocket().receive(
        '{"type":"notice","event_id":"n1","room_id":"r1","user_id":"u9","nick":"Pedro","kind":"joined","text":"Pedro entró al chat","occurred_at":"2026-10-10T10:00:00Z"}',
      );
    });
    expect(result.current.messages).toHaveLength(1);
    const [msg] = result.current.messages;
    expect(msg.type).toBe('notice');
    if (msg.type === 'notice') {
      expect(msg.kind).toBe('joined');
      expect(msg.nick).toBe('Pedro');
    }
  });

  it('send usa el socket abierto y valida vacío/cerrado', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    expect(result.current.send('   ')).toBe(false);
    expect(result.current.send('hola')).toBe(false); // aún CONNECTING
    act(() => lastSocket().open());
    expect(result.current.send('  hola  ')).toBe(true);
    const payload = JSON.parse(lastSocket().sent[0]);
    expect(payload).toMatchObject({ type: 'chat_send', room_id: 'r1', text: 'hola' });
  });

  it('sendReaction envía reaction_send y acumula el broadcast aparte del chat', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    expect(result.current.sendReaction('👏', 12000)).toBe(false); // aún CONNECTING
    act(() => lastSocket().open());
    expect(result.current.sendReaction('👏', 12000)).toBe(true);
    expect(JSON.parse(lastSocket().sent[0])).toMatchObject({
      type: 'reaction_send',
      room_id: 'r1',
      emoji: '👏',
      position_ms: 12000,
    });
    expect(result.current.reactions).toHaveLength(0);
    act(() => {
      lastSocket().receive(
        '{"type":"reaction","event_id":"e1","room_id":"r1","user_id":"u2","nick":"B","emoji":"👏","position_ms":12000,"occurred_at":"t","received_at":"t"}',
      );
    });
    expect(result.current.reactions).toHaveLength(1);
    expect(result.current.messages).toHaveLength(0);
    expect(result.current.reactions[0]).toMatchObject({ type: 'reaction', emoji: '👏' });
  });

  it('reconecta con backoff exponencial tras un corte', async () => {
    renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
        reconnectBaseMs: 500,
        reconnectMaxMs: 8000,
      }),
    );
    await flushTicket();
    act(() => lastSocket().open());
    expect(FakeSocket.instances).toHaveLength(1);
    act(() => lastSocket().close());
    expect(FakeSocket.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(FakeSocket.instances).toHaveLength(2);
    act(() => lastSocket().close());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(FakeSocket.instances).toHaveLength(3);
  });

  describe('cierres terminales (el host retiró o bloqueó al participante)', () => {
    const create = (url: string) => new FakeSocket(url) as unknown as WebSocket;

    it('4001: marca revoked, borra el guest token, queda closed y no vuelve a reconectar', async () => {
      localStorage.setItem('tint_guest_token', 'guest-jwt');
      const { result } = renderHook(() =>
        useSocialSocket('r1', { createSocket: create, role: 'guest', reconnectBaseMs: 500, reconnectMaxMs: 8000 }),
      );
      await flushTicket();
      act(() => lastSocket().open());
      expect(result.current.revoked).toBe(false);
      act(() => lastSocket().close(4001));
      expect(result.current.revoked).toBe(true);
      expect(result.current.status).toBe('closed');
      expect(localStorage.getItem('tint_guest_token')).toBeNull();
      await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
      expect(FakeSocket.instances).toHaveLength(1);
    });

    it('dos 4003 seguidos de un invitado: marca expired y no reconecta más', async () => {
      localStorage.setItem('tint_guest_token', 'guest-jwt');
      const { result } = renderHook(() => useSocialSocket('r1', { createSocket: create, role: 'guest' }));
      await flushTicket();
      act(() => lastSocket().open());
      act(() => lastSocket().close(4003));
      expect(result.current.expired).toBe(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      expect(FakeSocket.instances).toHaveLength(2);
      act(() => lastSocket().open());
      act(() => lastSocket().close(4003));
      expect(result.current.expired).toBe(true);
      expect(result.current.revoked).toBe(false);
      expect(result.current.status).toBe('closed');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(FakeSocket.instances).toHaveLength(2);
    });

    it('un corte normal sigue reconectando y no marca nada', async () => {
      const { result } = renderHook(() => useSocialSocket('r1', { createSocket: create, role: 'guest' }));
      await flushTicket();
      act(() => lastSocket().open());
      act(() => lastSocket().close(1006));
      await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
      expect(FakeSocket.instances).toHaveLength(2);
      expect(result.current.revoked).toBe(false);
      expect(result.current.expired).toBe(false);
    });
  });

  it('ignora mensajes desconocidos y queda closed sin room', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    act(() => {
      lastSocket().open();
      lastSocket().receive('no-json');
      lastSocket().receive('{"type":"trivia"}');
    });
    expect(result.current.messages).toHaveLength(0);

    const noRoom = renderHook(() =>
      useSocialSocket(undefined, {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    expect(noRoom.result.current.status).toBe('closed');
  });

  it('traga ecos: mismo event_id no se muestra dos veces', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    const echo =
      '{"type":"chat_message","event_id":"m1","room_id":"r1","user_id":"u","nick":"A","text":"hola","occurred_at":"t","received_at":"t"}';
    act(() => {
      lastSocket().open();
      lastSocket().receive(echo);
      lastSocket().receive(echo);
      lastSocket().receive(echo);
    });
    expect(result.current.messages).toHaveLength(1);
  });

  it('lo ya visto en el historial no se duplica al llegar en vivo', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    act(() => {
      lastSocket().open();
      lastSocket().receive(
        '{"type":"history","room_id":"r1","messages":[{"event_id":"h1","type":"chat_message","room_id":"r1","user_id":"u","nick":"A","text":"previo","occurred_at":"t","received_at":"t"}]}',
      );
      lastSocket().receive(
        '{"type":"chat_message","event_id":"h1","room_id":"r1","user_id":"u","nick":"A","text":"previo","occurred_at":"t","received_at":"t"}',
      );
    });
    expect(result.current.messages).toHaveLength(1);
  });

  it('al desmontar cierra el socket (un solo socket vivo)', async () => {
    const { unmount } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      }),
    );
    await flushTicket();
    act(() => lastSocket().open());
    const sock = lastSocket();
    unmount();
    expect(sock.closed).toBe(true);
  });

  it('un cierre tardío del socket viejo no mata al vigente', async () => {
    const { result } = renderHook(() =>
      useSocialSocket('r1', {
        createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
        reconnectBaseMs: 500,
        reconnectMaxMs: 8000,
      }),
    );
    await flushTicket();
    act(() => lastSocket().open());
    const old = lastSocket();
    act(() => old.close());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(FakeSocket.instances).toHaveLength(2);
    act(() => lastSocket().open());
    // Cierre tardío del socket viejo: debe ignorarse.
    act(() => {
      old.close();
      vi.advanceTimersByTime(8000);
    });
    expect(FakeSocket.instances).toHaveLength(2);
    expect(result.current.send('sigo vivo')).toBe(true);
    expect(lastSocket().sent).toHaveLength(1);
  });
});
