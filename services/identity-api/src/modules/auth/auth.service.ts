import jwt from 'jsonwebtoken';
import { env } from '../../config/env.js';
import { v4 as uuidv4 } from 'uuid';
import { legacyAudienceOr, serviceAudience, type TicketService } from './audiences.js';

export interface GuestTokenPayload {
  sub: string;
  room_id: string;
  role: 'participant';
  nick: string;
  jti: string;
  iss: string;
  aud: string;
  exp: number;
  iat: number;
}

/**
 * Guest token (sesión del invitado, 4 h). Su audiencia es la de identity-api (`tint:api`) o, mientras
 * dure la transición de ADR 0030 (`JWT_LEGACY_AUDIENCE` con valor), la legada: la web vieja lo usa
 * directamente en los WebSocket.
 */
export function issueGuestToken(roomId: string, nick: string): { token: string; payload: GuestTokenPayload } {
  const now = Math.floor(Date.now() / 1000);
  const ttlHours = parseInt(env.GUEST_TOKEN_TTL_HOURS, 10);
  const exp = now + ttlHours * 60 * 60;
  const payload: GuestTokenPayload = {
    sub: `guest:${uuidv4()}`,
    room_id: roomId,
    role: 'participant',
    nick: nick.trim(),
    jti: uuidv4(),
    iss: env.JWT_ISSUER,
    aud: legacyAudienceOr(env.JWT_AUDIENCE),
    exp,
    iat: now,
  };
  const token = jwt.sign(payload, env.JWT_SECRET, { algorithm: 'HS256' });
  return { token, payload };
}

export interface SyncTicketPayload {
  sub: string;
  room_id: string;
  role: 'host';
  nick: string;
  jti: string;
  iss: string;
  aud: string;
  exp: number;
  iat: number;
}

export interface HostTicketIdentity {
  userId: string;
  roomId: string;
  nick?: string | null;
}

export interface SyncTicketInput extends HostTicketIdentity {
  /** Audiencia del ticket: la del servicio destino (o la legada durante la transición). */
  audience: string;
  /** Vida en segundos; por defecto `SYNC_TICKET_TTL_SECONDS`. */
  ttlSeconds?: number;
}

export type HostTicketPayload = SyncTicketPayload;
export type HostTicketInput = SyncTicketInput;

/**
 * Ticket de host de corta duración (HS256, mismo secreto que los guest tokens). Lo usan los
 * servicios que solo validan JWT simétricos y no conocen los ID tokens de Firebase.
 */
export function generateHostTicket({ userId, roomId, nick, audience, ttlSeconds }: HostTicketInput): { token: string; expiresAt: Date } {
  const now = Math.floor(Date.now() / 1000);
  const exp = now + (ttlSeconds ?? env.SYNC_TICKET_TTL_SECONDS);
  const payload: SyncTicketPayload = {
    sub: userId,
    room_id: roomId,
    role: 'host',
    nick: nick?.trim() || 'Host',
    jti: uuidv4(),
    iss: env.JWT_ISSUER,
    aud: audience,
    exp,
    iat: now,
  };
  const token = jwt.sign(payload, env.JWT_SECRET, { algorithm: 'HS256' });
  return { token, expiresAt: new Date(exp * 1000) };
}

/** Sync-ticket para /ws/sync: mismo ticket de host. */
export const generateSyncTicket = generateHostTicket;

export interface ServiceTicket {
  token: string;
  expiresAt: Date;
  audience: string;
}

/** Ticket de host para un solo servicio (ADR 0030): `aud = tint:<service>`, jti nuevo. */
export function issueHostServiceTicket(identity: HostTicketIdentity, service: TicketService): ServiceTicket {
  const audience = serviceAudience(service);
  const { token, expiresAt } = generateHostTicket({
    ...identity,
    audience,
    ttlSeconds: env.SERVICE_TICKET_TTL_SECONDS,
  });
  return { token, expiresAt, audience };
}

/**
 * Ticket de invitado para un solo servicio (ADR 0030). Hereda `sub`, `nick`, `room_id` y **`jti`**
 * de la sesión: retirar al invitado (`token:revoked:{jti}`) revoca también sus tickets. Su `exp`
 * nunca supera el de la sesión.
 */
export function issueGuestServiceTicket(session: GuestTokenPayload, service: TicketService): ServiceTicket {
  const now = Math.floor(Date.now() / 1000);
  const exp = Math.min(now + env.SERVICE_TICKET_TTL_SECONDS, session.exp);
  const audience = serviceAudience(service);
  const payload: GuestTokenPayload = {
    sub: session.sub,
    room_id: session.room_id,
    role: 'participant',
    nick: session.nick,
    jti: session.jti,
    iss: env.JWT_ISSUER,
    aud: audience,
    exp,
    iat: now,
  };
  const token = jwt.sign(payload, env.JWT_SECRET, { algorithm: 'HS256' });
  return { token, expiresAt: new Date(exp * 1000), audience };
}
