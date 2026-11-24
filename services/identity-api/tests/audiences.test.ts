import { describe, it, expect, afterEach } from 'vitest';
import { env } from '../src/config/env.js';
import {
  TICKET_SERVICES,
  isTicketService,
  serviceAudience,
  acceptedAudiences,
  legacyAudienceOr,
  audienceForRoute,
} from '../src/modules/auth/audiences.js';

afterEach(() => {
  env.JWT_LEGACY_AUDIENCE = undefined;
});

describe('audiencias por servicio (ADR 0030)', () => {
  it('reconoce solo los cinco servicios', () => {
    expect(TICKET_SERVICES).toEqual(['sync', 'social', 'voice', 'stream', 'analytics']);
    for (const s of TICKET_SERVICES) expect(isTicketService(s)).toBe(true);
    expect(isTicketService('api')).toBe(false);
    expect(isTicketService('room')).toBe(false);
    expect(isTicketService('toString')).toBe(false);
  });

  it('cada servicio tiene su audiencia tint:<servicio>', () => {
    expect(serviceAudience('voice')).toBe('tint:voice');
    expect(serviceAudience('analytics')).toBe('tint:analytics');
  });

  it('sin JWT_LEGACY_AUDIENCE solo se acepta la audiencia propia', () => {
    expect(acceptedAudiences('tint:sync')).toEqual(['tint:sync']);
    expect(legacyAudienceOr('tint:sync')).toBe('tint:sync');
  });

  it('con JWT_LEGACY_AUDIENCE se acepta además la legada', () => {
    env.JWT_LEGACY_AUDIENCE = 'tint:room';
    expect(acceptedAudiences('tint:sync')).toEqual(['tint:sync', 'tint:room']);
    expect(legacyAudienceOr('tint:sync')).toBe('tint:room');
  });

  it.each([
    ['/ws/sync?room_id=r&token=x', 'tint:sync'],
    ['/ws/sync/', 'tint:sync'],
    ['/ws/social?token=x', 'tint:social'],
    ['/api/v1/social/rooms/r/chat', 'tint:social'],
    ['/ws/voice?token=x', 'tint:voice'],
    ['/api/v1/voice/rooms/r/mutes/u', 'tint:voice'],
    ['/api/v1/stream/whip/r', 'tint:stream'],
    ['/api/v1/rooms/r-1/kpis', 'tint:analytics'],
    ['/api/v1/rooms/r-1/reactions/heatmap?bucket=5', 'tint:analytics'],
    ['/api/v1/rooms/r-1/timeline', 'tint:analytics'],
    ['/api/v1/rooms/r-1/playback', 'tint:api'],
    ['/api/v1/rooms', 'tint:api'],
    ['/api/v1/rooms/r-1/kpis/extra', 'tint:api'],
  ])('%s → %s', (uri, audience) => {
    expect(audienceForRoute(uri)).toBe(audience);
  });

  it('sin X-Forwarded-Uri (llamada directa) se espera la audiencia de identity-api', () => {
    expect(audienceForRoute(undefined)).toBe(env.JWT_AUDIENCE);
  });

  it('un /ws/* desconocido no tiene audiencia (falla cerrado)', () => {
    expect(audienceForRoute('/ws/otro?token=x')).toBeNull();
    expect(audienceForRoute('/ws/syncx')).toBeNull();
  });
});
