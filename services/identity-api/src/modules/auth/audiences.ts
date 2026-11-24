import { env } from '../../config/env.js';

/**
 * Una audiencia por servicio (ADR 0030, mínimo privilegio): un ticket emitido para un servicio
 * no abre ningún otro. identity-api usa la suya (`JWT_AUDIENCE`, `tint:api`).
 */
export const TICKET_SERVICES = ['sync', 'social', 'voice', 'stream', 'analytics'] as const;
export type TicketService = (typeof TICKET_SERVICES)[number];

/** Servicios para los que un invitado puede pedir ticket (analytics es solo del host). */
export const GUEST_TICKET_SERVICES: ReadonlySet<TicketService> = new Set(['sync', 'social', 'voice', 'stream']);

export function isTicketService(value: unknown): value is TicketService {
  return typeof value === 'string' && (TICKET_SERVICES as readonly string[]).includes(value);
}

export function serviceAudience(service: TicketService) {
  return `tint:${service}`;
}

/**
 * Audiencias que acepta un verificador: la propia y, solo mientras `JWT_LEGACY_AUDIENCE` tenga
 * valor, la legada (transición). Vacía = desactivada: falla cerrado.
 */
export function acceptedAudiences(primary: string): [string, ...string[]] {
  const legacy = env.JWT_LEGACY_AUDIENCE;
  return legacy && legacy !== primary ? [primary, legacy] : [primary];
}

/** Audiencia de los tokens viejos (guest token, sync-ticket, analytics-ticket) mientras dure la transición. */
export function legacyAudienceOr(fallback: string) {
  return env.JWT_LEGACY_AUDIENCE || fallback;
}

const ANALYTICS_ROUTE = /^\/api\/v1\/rooms\/[^/]+\/(kpis|reactions\/heatmap|timeline)$/;

function isUnder(path: string, base: string) {
  return path === base || path.startsWith(`${base}/`);
}

/**
 * Audiencia que espera el gateway (`forward_auth`) según la ruta original (`X-Forwarded-Uri`).
 * Sin cabecera (llamada directa) o con cualquier otra ruta `/api/`, la de identity-api.
 * Un `/ws/*` que no es de ningún servicio no tiene audiencia: `null` (falla cerrado).
 */
export function audienceForRoute(uri: string | undefined): string | null {
  if (!uri) return env.JWT_AUDIENCE;
  const query = uri.indexOf('?');
  const path = query < 0 ? uri : uri.slice(0, query);
  if (isUnder(path, '/ws/sync')) return serviceAudience('sync');
  if (isUnder(path, '/ws/social') || path.startsWith('/api/v1/social/')) return serviceAudience('social');
  if (isUnder(path, '/ws/voice') || path.startsWith('/api/v1/voice/')) return serviceAudience('voice');
  if (path.startsWith('/api/v1/stream/')) return serviceAudience('stream');
  if (ANALYTICS_ROUTE.test(path)) return serviceAudience('analytics');
  if (path.startsWith('/ws/')) return null;
  return env.JWT_AUDIENCE;
}
