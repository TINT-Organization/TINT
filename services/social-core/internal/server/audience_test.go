package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

// audTicket firma un HS256 como identity-api con la audiencia dada.
func audTicket(t *testing.T, secret, role string, aud any, exp time.Time) string {
	t.Helper()
	return signGuestToken(t, secret, map[string]any{
		"sub": "u-" + role, "room_id": "r1", "role": role, "nick": role,
		"jti": "jti-" + role, "iss": "tint-identity", "aud": aud, "exp": exp.Unix(),
	})
}

func TestDefaultDepsLegacyAudience(t *testing.T) {
	if d := DefaultDeps(func(string) string { return "" }); d.JWTLegacyAudience != "" {
		t.Fatalf("sin JWT_LEGACY_AUDIENCE = %q, want vacía (desactivada)", d.JWTLegacyAudience)
	}
	env := map[string]string{"JWT_AUDIENCE": "tint:social", "JWT_LEGACY_AUDIENCE": " tint:room "}
	d := DefaultDeps(func(k string) string { return env[k] })
	if d.JWTAudience != "tint:social" || d.JWTLegacyAudience != "tint:room" {
		t.Fatalf("audiencias = %q / %q", d.JWTAudience, d.JWTLegacyAudience)
	}
}

// AC1 y AC5 de ADR 0030 en el handshake de /ws/social: un ticket de otro
// servicio se rechaza como un token inválido (401 sin upgrade).
func TestSocialHandshakeAudiencePerService(t *testing.T) {
	const secret = "s3cr3t"
	serve := func(legacy string) string {
		env := map[string]string{"JWT_SECRET": secret, "JWT_AUDIENCE": "tint:social", "JWT_LEGACY_AUDIENCE": legacy}
		srv := httptest.NewServer(NewRouterWithDeps(DefaultDeps(func(k string) string { return env[k] })))
		t.Cleanup(srv.Close)
		return "ws://" + strings.TrimPrefix(srv.URL, "http://") + "/ws/social?room_id=r1"
	}
	strict, transition := serve(""), serve("tint:room")
	future, past := time.Now().Add(time.Hour), time.Now().Add(-time.Minute)
	tests := []struct {
		name string
		url  string
		tok  string
		want int
	}{
		{"propia", strict, audTicket(t, secret, "participant", "tint:social", future), 101},
		{"propia en lista", strict, audTicket(t, secret, "host", []string{"tint:sync", "tint:social"}, future), 101},
		{"ticket de sync-core", transition, audTicket(t, secret, "host", "tint:sync", future), http.StatusUnauthorized},
		{"ticket de voice-core", transition, audTicket(t, secret, "participant", "tint:voice", future), http.StatusUnauthorized},
		{"ticket de analytics", transition, audTicket(t, secret, "host", "tint:analytics", future), http.StatusUnauthorized},
		{"ticket de otro servicio vencido", transition, audTicket(t, secret, "host", "tint:voice", past), http.StatusUnauthorized},
		{"legada sin JWT_LEGACY_AUDIENCE", strict, audTicket(t, secret, "participant", "tint:room", future), http.StatusUnauthorized},
		{"legada con JWT_LEGACY_AUDIENCE", transition, audTicket(t, secret, "participant", "tint:room", future), 101},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := dialStatus(t, tt.url, tt.tok); got != tt.want {
				t.Fatalf("status = %d, want %d", got, tt.want)
			}
		})
	}
}

// AC1 en REST: GET /api/v1/social/rooms/{id}/chat con el ticket de otro
// servicio responde 401 UNAUTHORIZED, igual que un token inválido.
func TestChatRESTAudiencePerService(t *testing.T) {
	const secret = "s3cr3t"
	mr := miniredis.RunT(t)
	env := map[string]string{
		"REDIS_URL": "redis://" + mr.Addr(), "JWT_SECRET": secret,
		"JWT_AUDIENCE": "tint:social", "JWT_LEGACY_AUDIENCE": "tint:room",
	}
	srv := httptest.NewServer(NewRouterWithDeps(DefaultDeps(func(k string) string { return env[k] })))
	t.Cleanup(srv.Close)
	future := time.Now().Add(time.Hour)
	for _, aud := range []string{"tint:sync", "tint:voice", "tint:stream", "tint:analytics", "tint:api"} {
		if status, body := getChat(t, srv.URL, "r1", audTicket(t, secret, "host", aud, future)); status != http.StatusUnauthorized || errorCode(t, body) != "UNAUTHORIZED" {
			t.Fatalf("aud %s = %d %s, want 401 UNAUTHORIZED", aud, status, body)
		}
	}
	for _, aud := range []string{"tint:social", "tint:room"} {
		if status, body := getChat(t, srv.URL, "r1", audTicket(t, secret, "host", aud, future)); status != http.StatusOK {
			t.Fatalf("aud %s = %d %s, want 200", aud, status, body)
		}
	}
}
