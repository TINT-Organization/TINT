import { apiFetch, type ApiAuth } from './api';
import { errorStatus } from './errorStatus';
import { isRoomDisabledError } from './roomClosed';

/**
 * Un ticket por servicio (ADR 0030, mínimo privilegio): cada canal pide a identity-api un JWT
 * HS256 de vida corta con la audiencia de **su** servicio (`aud = tint:<service>`), con
 * `POST /rooms/:id/tickets/:service`. El host lo pide con su ID token de Firebase y el invitado
 * con su guest token (que ya no viaja a ningún otro servicio); el invitado no obtiene `analytics`.
 */
export type TicketService = 'sync' | 'social' | 'voice' | 'stream' | 'analytics';

export interface ServiceTicket {
  token: string;
  expiresAt: string;
  audience: string;
}

/** Quién pide el ticket: decide la credencial que manda `apiFetch`. */
export type TicketRole = 'host' | 'guest';

export async function getServiceTicket(
  roomId: string,
  service: TicketService,
  auth: ApiAuth = 'auto',
): Promise<ServiceTicket> {
  const body = (await apiFetch(
    `/rooms/${encodeURIComponent(roomId)}/tickets/${service}`,
    { method: 'POST' },
    auth,
  )) as { token?: unknown; expires_at?: unknown; audience?: unknown } | null;
  if (!body || typeof body.token !== 'string' || body.token === '') {
    throw new Error(`Ticket ${service} sin token`);
  }
  return {
    token: body.token,
    expiresAt: typeof body.expires_at === 'string' ? body.expires_at : '',
    audience: typeof body.audience === 'string' ? body.audience : `tint:${service}`,
  };
}

/** Token del ticket con la credencial del rol (`host` → Firebase, `guest` → guest token). */
export async function serviceTicketToken(roomId: string, service: TicketService, role?: TicketRole): Promise<string> {
  return (await getServiceTicket(roomId, service, role ?? 'auto')).token;
}

/**
 * Qué significa que identity-api rechace el ticket, o null si es transitorio (red, 5xx, 429 y el
 * 401 del host, que `apiFetch` ya intentó renovar) y conviene reintentar.
 * - `disabled`: `410 {error:"Disabled"}`, el evento terminó (ADR 0019).
 * - `gone`: sala o servicio inexistente (404, `410 Deleted`).
 * - `forbidden`: el host no es el dueño de la sala (403).
 * - `revoked`: el invitado fue retirado o bloqueado (403, `401 Token revoked`).
 * - `expired`: la sesión del invitado caducó (otro 401).
 */
export type TicketFailure = 'disabled' | 'gone' | 'forbidden' | 'revoked' | 'expired';

export function classifyTicketError(err: unknown, role: TicketRole | undefined): TicketFailure | null {
  if (isRoomDisabledError(err)) return 'disabled';
  const status = errorStatus(err);
  if (status === 404 || status === 410) return 'gone';
  if (role === 'guest') {
    if (status === 403) return 'revoked';
    if (status === 401) {
      const message = (err as { message?: unknown }).message;
      return message === 'Token revoked' ? 'revoked' : 'expired';
    }
    return null;
  }
  if (status === 403) return 'forbidden';
  return null;
}
