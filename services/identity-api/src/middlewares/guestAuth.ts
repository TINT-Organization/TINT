import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import { env } from '../config/env.js';
import { isTokenRevoked } from '../modules/tokens/revocation.js';
import { findByJti as findGuestSession, type GuestSession } from '../modules/guests/guestSessions.repo.js';
import { requireAuthHost } from './auth.js';
import { requireRoomOwner } from './requireRoomOwner.js';
import type { GuestTokenPayload } from '../modules/auth/auth.service.js';
import { acceptedAudiences } from '../modules/auth/audiences.js';
import { auditDenied } from '../modules/audit/audit.events.js';
import { logger } from '../logging.js';

declare module 'express-serve-static-core' {
  interface Request {
    guest?: GuestTokenPayload;
    /** Fila de `guest_sessions` del token (null si es anterior a esa tabla). */
    guestSession?: GuestSession | null;
  }
}

function bearer(req: Request) {
  const header = req.headers.authorization;
  return header && header.startsWith('Bearer ') ? header.substring(7) : null;
}

function roomIdParam(req: Request) {
  const rawId = req.params.id;
  return Array.isArray(rawId) ? rawId[0] : rawId;
}

/**
 * Exige un guest token HS256 emitido por identity-api para la sala `:id`
 * (audiencia `tint:api`, o la legada mientras dure la transición de ADR 0030) y cuyo `jti` no esté revocado
 * (ni `guest_sessions.revoked` en BD ni la clave Redis `token:revoked:{jti}`).
 * Tokens emitidos antes de existir guest_sessions (sin fila) se aceptan si Redis no los marca.
 */
export async function requireRoomGuest(req: Request, res: Response, next: NextFunction) {
  const token = bearer(req);
  if (!token) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  let payload: GuestTokenPayload;
  try {
    // Solo la sesión del invitado (`tint:api`, o la legada mientras dure la transición de
    // ADR 0030): un ticket de servicio no sirve contra identity-api.
    payload = jwt.verify(token, env.JWT_SECRET, {
      algorithms: ['HS256'],
      issuer: env.JWT_ISSUER,
      audience: acceptedAudiences(env.JWT_AUDIENCE),
    }) as GuestTokenPayload;
  } catch {
    return res.status(401).json({ error: 'Invalid token' });
  }
  if (payload.role !== 'participant' || !payload.jti || !payload.room_id) {
    return res.status(401).json({ error: 'Invalid token' });
  }
  const roomId = roomIdParam(req);
  if (payload.room_id !== roomId) {
    // Sin actor: el `sub` del invitado nunca llega a la bitácora.
    void auditDenied({ roomId });
    return res.status(403).json({ error: 'Forbidden' });
  }
  let session: GuestSession | null;
  try {
    session = (await findGuestSession(payload.jti)) ?? null;
    if (session?.revoked || (await isTokenRevoked(payload.jti))) {
      return res.status(401).json({ error: 'Token revoked' });
    }
  } catch (err) {
    logger.error('guestAuth: no se pudo comprobar la revocación', { error: (err as Error).message });
    return res.status(503).json({ error: 'Service unavailable' });
  }
  req.guest = payload;
  req.guestSession = session;
  next();
}

/**
 * Host dueño (ID token de Firebase, RS256) o invitado de la sala (guest token HS256).
 * Se distingue por el `alg` de la cabecera del JWT.
 */
export function requireRoomHostOrGuest(req: Request, res: Response, next: NextFunction) {
  const token = bearer(req);
  const decoded = token ? (jwt.decode(token, { complete: true }) as jwt.Jwt | null) : null;
  if (decoded?.header?.alg === 'HS256') {
    return requireRoomGuest(req, res, next);
  }
  return requireAuthHost(req, res, () => requireRoomOwner(req, res, next));
}
