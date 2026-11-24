import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import { env } from '../config/env.js';
import { isTokenRevoked } from '../modules/tokens/revocation.js';
import { acceptedAudiences, audienceForRoute } from '../modules/auth/audiences.js';
import { requireAuthHost } from './auth.js';

function bearer(req: Request) {
  const header = req.headers.authorization;
  return header && header.startsWith('Bearer ') ? header.substring(7) : null;
}

function forwardedUri(req: Request) {
  const uri = req.headers['x-forwarded-uri'];
  return typeof uri === 'string' ? uri : undefined;
}

/**
 * Token de un handshake WebSocket que llega por el gateway. El navegador no puede mandar
 * `Authorization` al abrir un WebSocket, así que la web lo pone en `?token=` y Caddy
 * (`forward_auth`) reenvía la URI original en `X-Forwarded-Uri`. Solo vale para `/ws/*`:
 * la API REST siempre usa `Authorization`.
 */
function websocketToken(req: Request) {
  const uri = forwardedUri(req);
  if (!uri || !uri.startsWith('/ws/')) return null;
  const query = uri.indexOf('?');
  if (query < 0) return null;
  return new URLSearchParams(uri.slice(query + 1)).get('token') || null;
}

/** Firma, emisor y audiencia de un HS256 propio. La audiencia es la de la ruta (ADR 0030). */
function verifyOwnToken(token: string, audience: string, ignoreExpiration = false) {
  return jwt.verify(token, env.JWT_SECRET, {
    algorithms: ['HS256'],
    issuer: env.JWT_ISSUER,
    audience: acceptedAudiences(audience),
    ignoreExpiration,
  }) as jwt.JwtPayload;
}

/**
 * En `/ws/*` se exige la firma, el emisor y la audiencia, pero no se rechazan la caducidad ni la
 * revocación: cada servicio vuelve a verificar el token y, tras el upgrade, cierra con `4003` o
 * `4001`. Si el gateway respondiera 401 antes del upgrade el navegador solo vería `1006`,
 * reintentaría sin fin y no podría mostrar el aviso (lección de ADR 0014).
 */
function checkWebsocketToken(token: string, audience: string | null, res: Response) {
  const decoded = jwt.decode(token, { complete: true }) as jwt.Jwt | null;
  if (decoded?.header?.alg !== 'HS256' || !audience) {
    return unauthorized(res);
  }
  try {
    const payload = verifyOwnToken(token, audience, true);
    if (!payload.sub) {
      return unauthorized(res);
    }
    return forwardTokenIdentity(res, payload);
  } catch {
    return unauthorized(res);
  }
}

function unauthorized(res: Response) {
  return res.status(401).json({ error: 'Invalid token' });
}

function forwardIdentity(res: Response, identity: Record<string, unknown>) {
  const headers: Record<string, string> = {};
  for (const [name, value] of Object.entries(identity)) {
    if (typeof value === 'string' && value.length > 0) {
      headers[name] = value;
    }
  }
  return res.set(headers).status(204).end();
}

function forwardTokenIdentity(res: Response, payload: jwt.JwtPayload) {
  return forwardIdentity(res, {
    'X-User-Id': payload.sub,
    'X-User-Sub': payload.sub,
    'X-User-Role': payload.role,
    'X-User-Room-Id': payload.room_id,
    'X-User-Nick': payload.nick,
  });
}

/**
 * `forward_auth` de Caddy. La audiencia esperada sale de la ruta original (`X-Forwarded-Uri`,
 * ADR 0030): un ticket de un servicio no abre otro, y la legada (`JWT_LEGACY_AUDIENCE`) solo se
 * acepta mientras tenga valor.
 */
export async function centralTokenCheck(req: Request, res: Response, next: NextFunction) {
  const audience = audienceForRoute(forwardedUri(req));
  const token = bearer(req);
  if (!token) {
    const wsToken = websocketToken(req);
    return wsToken ? checkWebsocketToken(wsToken, audience, res) : unauthorized(res);
  }

  const decoded = jwt.decode(token, { complete: true }) as jwt.Jwt | null;
  if (!decoded?.header?.alg || !audience) {
    return unauthorized(res);
  }

  if (decoded.header.alg === 'HS256') {
    try {
      const payload = verifyOwnToken(token, audience);
      if (!payload.sub || (payload.jti && await isTokenRevoked(payload.jti))) {
        return unauthorized(res);
      }
      return forwardTokenIdentity(res, payload);
    } catch {
      return unauthorized(res);
    }
  }

  // ID token de Firebase del host: RS256 o, solo en modo emulador, `none` (requireAuthHost decide).
  // Solo hacia rutas de identity-api: los demás servicios piden su ticket (ADR 0030).
  if ((decoded.header.alg === 'RS256' || decoded.header.alg === 'none') && audience === env.JWT_AUDIENCE) {
    return requireAuthHost(req, res, () => {
      const user = req.user;
      if (!user) {
        return unauthorized(res);
      }
      return forwardIdentity(res, {
        'X-User-Id': user.id,
        'X-User-Sub': user.providerSub,
        'X-User-Role': 'host',
      });
    });
  }

  return unauthorized(res);
}
