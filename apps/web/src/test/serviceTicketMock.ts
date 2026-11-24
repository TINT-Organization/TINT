import { act } from '@testing-library/react';

/**
 * Implementación de `apiFetch` para pruebas de canales (ADR 0030): responde a
 * `POST /rooms/:id/tickets/:service` con un ticket cuyo token es el `tint_guest_token` guardado
 * (o `host-ticket` para el host), así las URLs esperadas siguen siendo legibles.
 */
export function echoTicket(_path: string, _init?: RequestInit, auth?: string): Promise<unknown> {
  const token = auth === 'host' ? 'host-ticket' : (localStorage.getItem('tint_guest_token') || 'test-ticket');
  return Promise.resolve({ token, expires_at: '2099-01-01T00:00:00Z', audience: 'tint:test' });
}

/** Deja correr las microtareas del pedido del ticket dentro de `act`. */
export async function flushTicket() {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });
}
