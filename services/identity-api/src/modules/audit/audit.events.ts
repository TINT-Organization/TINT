import { v4 as uuidv4 } from 'uuid';
import { getRedisClient } from '../tokens/revocation.js';
import { logger } from '../../logging.js';

/**
 * Stream que consume la bitácora de auditoría (`services/audit-log`, grupo `audit-workers`).
 * Contrato en services/audit-log/AGENTS.md: campos planos de texto, lista blanca, sin datos
 * personales (ni email, ni nombre, ni nick, ni IP, ni tokens).
 */
export const AUDIT_STREAM = 'audit:events';
const MAXLEN = '100000';
const SOURCE = 'identity-api';
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type AuditAction =
  | 'room.create'
  | 'room.update'
  | 'room.delete'
  | 'room.end'
  | 'room.disable'
  | 'invitation.create'
  | 'invitation.revoke'
  | 'guest.revoke'
  | 'ban.create'
  | 'ban.delete'
  | 'access.denied';
export type AuditOutcome = 'ok' | 'denied';
export type AuditActorRole = 'host' | 'service';

export interface AuditEvent {
  action: AuditAction;
  outcome: AuditOutcome;
  actorId?: string | null;
  actorRole?: AuditActorRole;
  roomId?: string | null;
}

/** Lo único que la bitácora lee del usuario autenticado: el `sub` de Firebase. */
type Actor = { providerSub: string } | null | undefined;

function logFailure(action: string, err: unknown) {
  // Solo la acción y el mensaje: nunca el actor ni la sala.
  logger.error(`audit: no se pudo publicar ${action}`, {
    error: (err as Error)?.message ?? String(err),
  });
}

/**
 * `XADD audit:events MAXLEN ~ 100000 * …` best-effort: la promesa nunca rechaza (ni si el
 * cliente lanza de forma síncrona), así que la ruta la llama con `void` y la acción nunca se
 * bloquea por la bitácora. Un hueco se ve en la medición "hechas vs registradas".
 * `room_id` solo se envía si es un UUID: un id inventado en la URL no llega a la bitácora.
 */
export function publishAuditEvent(event: AuditEvent): Promise<void> {
  try {
    const args: string[] = [
      'event_id',
      uuidv4(),
      'action',
      event.action,
      'outcome',
      event.outcome,
      'occurred_at',
      new Date().toISOString(),
      'source',
      SOURCE,
    ];
    if (event.actorId) args.push('actor_id', event.actorId);
    if (event.actorRole) args.push('actor_role', event.actorRole);
    if (event.roomId && UUID_RE.test(event.roomId)) args.push('room_id', event.roomId);
    return Promise.resolve(getRedisClient().xadd(AUDIT_STREAM, 'MAXLEN', '~', MAXLEN, '*', ...args)).then(
      () => undefined,
      (err) => logFailure(event.action, err),
    );
  } catch (err) {
    logFailure(event.action, err);
    return Promise.resolve();
  }
}

/** Acción sensible del host que terminó bien. Actor = `users.provider_sub`, nunca `users.id`. */
export function auditHostAction(user: Actor, action: AuditAction, roomId?: string | null) {
  return publishAuditEvent({ action, outcome: 'ok', actorId: user?.providerSub, actorRole: 'host', roomId });
}

/** Acceso negado (401/403). Sin token no hay actor. */
export function auditDenied({ user, roomId }: { user?: Actor; roomId?: string | null }) {
  return publishAuditEvent({
    action: 'access.denied',
    outcome: 'denied',
    actorId: user?.providerSub,
    actorRole: user ? 'host' : undefined,
    roomId,
  });
}
