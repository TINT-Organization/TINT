package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

func signGuest(payload map[string]any, secret string) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	raw, _ := json.Marshal(payload)
	body := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(header + "." + body))
	return header + "." + body + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

var guestCfg = GuestConfig{Secret: "secret", Issuer: "tint-identity", Audience: "tint:room"}

func guestClaims(now time.Time, mutate func(map[string]any)) map[string]any {
	c := map[string]any{
		"sub": "guest:1", "room_id": "r1", "role": "participant", "nick": "Invitado",
		"jti": "jti-1", "iss": "tint-identity", "aud": "tint:room",
		"iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	}
	if mutate != nil {
		mutate(c)
	}
	return c
}

func TestVerifyGuestOKAndWrongRoom(t *testing.T) {
	now := time.Now()
	token := signGuest(guestClaims(now, nil), "secret")
	got, err := VerifyGuest(token, guestCfg, "r1", now)
	if err != nil {
		t.Fatalf("VerifyGuest = %v", err)
	}
	if got.Subject != "guest:1" || !got.IsGuest || got.Nick != "Invitado" || got.JTI != "jti-1" {
		t.Fatalf("claims = %+v", got)
	}
	if _, err := VerifyGuest(token, guestCfg, "otra", now); err != ErrWrongRoom {
		t.Fatalf("sala distinta debe dar ErrWrongRoom, got %v", err)
	}
	bad := guestCfg
	bad.Secret = "otra"
	if _, err := VerifyGuest(token, bad, "r1", now); err != ErrInvalidToken {
		t.Fatalf("firma mala debe dar ErrInvalidToken, got %v", err)
	}
	expired := signGuest(guestClaims(now, func(c map[string]any) { c["exp"] = now.Add(-time.Hour).Unix() }), "secret")
	if _, err := VerifyGuest(expired, guestCfg, "r1", now); err != ErrExpired {
		t.Fatalf("expirado debe dar ErrExpired, got %v", err)
	}
	// Caducado y de otra sala: ErrWrongRoom (403 sin upgrade, ADR 0014).
	if _, err := VerifyGuest(expired, guestCfg, "otra", now); err != ErrWrongRoom {
		t.Fatalf("expirado de otra sala debe dar ErrWrongRoom, got %v", err)
	}
	// aud como arreglo (RFC 7519) también vale si contiene la audiencia.
	arr := signGuest(guestClaims(now, func(c map[string]any) { c["aud"] = []string{"otra", "tint:room"} }), "secret")
	if _, err := VerifyGuest(arr, guestCfg, "r1", now); err != nil {
		t.Fatalf("aud arreglo = %v", err)
	}
}

// Sin jti el token no se puede revocar; sin iss/aud/exp no es de identity-api.
func TestVerifyGuestRequiresRegisteredClaims(t *testing.T) {
	now := time.Now()
	cases := map[string]func(map[string]any){
		"sin jti":        func(c map[string]any) { delete(c, "jti") },
		"sin exp":        func(c map[string]any) { delete(c, "exp") },
		"iss ajeno":      func(c map[string]any) { c["iss"] = "otro" },
		"sin iss":        func(c map[string]any) { delete(c, "iss") },
		"aud ajena":      func(c map[string]any) { c["aud"] = "otra" },
		"aud arreglo":    func(c map[string]any) { c["aud"] = []string{"otra"} },
		"aud no texto":   func(c map[string]any) { c["aud"] = 3 },
		"sin sub":        func(c map[string]any) { c["sub"] = "" },
		"payload basura": func(c map[string]any) { c["exp"] = "mañana" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := VerifyGuest(signGuest(guestClaims(now, mutate), "secret"), guestCfg, "r1", now); err == nil {
				t.Fatal("debe fallar")
			}
		})
	}
}

func TestGuestConfigDefaults(t *testing.T) {
	now := time.Now()
	token := signGuest(guestClaims(now, nil), "secret")
	if _, err := VerifyGuest(token, GuestConfig{Secret: "secret"}, "r1", now); err != nil {
		t.Fatalf("iss/aud por defecto (tint-identity, tint:room) = %v", err)
	}
}

// AC1 de ADR 0030: social-core solo acepta tickets HS256 con su propia
// audiencia (JWT_AUDIENCE, tint:social en Compose) y, durante la transición,
// la de JWT_LEGACY_AUDIENCE si tiene valor. Otra audiencia = token inválido.
func TestVerifyGuestAudiencePerService(t *testing.T) {
	const own, legacy = "tint:social", "tint:room"
	now := time.Now()
	tests := []struct {
		name   string
		legacy string
		aud    any
		ok     bool
	}{
		{"propia", "", own, true},
		{"propia en lista", "", []string{"tint:otra", own}, true},
		{"sync", legacy, "tint:sync", false},
		{"voice", legacy, "tint:voice", false},
		{"stream", legacy, "tint:stream", false},
		{"analytics", legacy, "tint:analytics", false},
		{"api", legacy, "tint:api", false},
		{"lista sin la propia", legacy, []string{"tint:sync", "tint:voice"}, false},
		{"lista vacía", legacy, []string{}, false},
		{"vacía con la variable vacía", "", "", false},
		{"legada con la variable vacía", "", legacy, false},
		{"legada en lista con la variable vacía", "", []string{legacy}, false},
		{"legada con la variable definida", legacy, legacy, true},
		{"legada en lista con la variable definida", legacy, []string{"tint:voice", legacy}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := GuestConfig{Secret: "secret", Issuer: "tint-identity", Audience: own, LegacyAudience: tt.legacy}
			tok := signGuest(guestClaims(now, func(c map[string]any) { c["aud"] = tt.aud }), "secret")
			_, err := VerifyGuest(tok, cfg, "r1", now)
			if tt.ok && err != nil {
				t.Fatalf("aud %v debe aceptarse: %v", tt.aud, err)
			}
			if !tt.ok && err != ErrInvalidToken {
				t.Fatalf("aud %v: err = %v, want ErrInvalidToken", tt.aud, err)
			}
		})
	}
}

// Un ticket de otro servicio ya vencido es inválido (401), no expirado (4003).
func TestVerifyGuestExpiredForeignAudience(t *testing.T) {
	now := time.Now()
	cfg := GuestConfig{Secret: "secret", Audience: "tint:social", LegacyAudience: "tint:room"}
	tok := signGuest(guestClaims(now, func(c map[string]any) {
		c["aud"] = "tint:voice"
		c["exp"] = now.Add(-time.Minute).Unix()
	}), "secret")
	if _, err := VerifyGuest(tok, cfg, "r1", now); err != ErrInvalidToken {
		t.Fatalf("err = %v, want ErrInvalidToken", err)
	}
}

func TestVerifyGuestRejectsEmptySecret(t *testing.T) {
	now := time.Now()
	token := signGuest(guestClaims(now, nil), "")
	if _, err := VerifyGuest(token, GuestConfig{}, "r1", now); err != ErrInvalidToken {
		t.Fatalf("secreto vacío debe fallar cerrado, got %v", err)
	}
}

func TestBearerToken(t *testing.T) {
	got, err := BearerToken("Bearer abc", "")
	if err != nil || got != "abc" {
		t.Fatalf("header = %q, %v", got, err)
	}
	got, err = BearerToken("", "q123")
	if err != nil || got != "q123" {
		t.Fatalf("query = %q, %v", got, err)
	}
	if _, err := BearerToken("", ""); err != ErrMissingToken {
		t.Fatalf("sin token debe dar ErrMissingToken, got %v", err)
	}
}
