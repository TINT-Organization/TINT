package auth

import (
	"errors"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	secret = "test-secret"
	iss    = "tint-identity"
	aud    = "tint:room"
	room   = "room-1"
)

var now = time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)

func sign(t *testing.T, method jwt.SigningMethod, key any, mutate func(jwt.MapClaims)) string {
	t.Helper()
	c := jwt.MapClaims{
		"sub": "user-1", "room_id": room, "role": "host", "nick": "Ana",
		"iss": iss, "aud": aud, "iat": now.Unix(), "exp": now.Add(10 * time.Minute).Unix(),
	}
	if mutate != nil {
		mutate(c)
	}
	tok, err := jwt.NewWithClaims(method, c).SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func newVerifier() *Verifier {
	v := NewVerifier(secret, iss, aud, "")
	v.now = func() time.Time { return now }
	return v
}

func TestVerifyValid(t *testing.T) {
	tests := []struct {
		name string
		role string
		want Claims
	}{
		{"host", "host", Claims{Sub: "user-1", RoomID: room, Role: RoleHost, Nick: "Ana"}},
		{"cohost", "cohost", Claims{Sub: "user-1", RoomID: room, Role: RoleCohost, Nick: "Ana"}},
		{"participant", "participant", Claims{Sub: "user-1", RoomID: room, Role: RoleParticipant, Nick: "Ana"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["role"] = tt.role })
			got, err := newVerifier().Verify(tok, room)
			if err != nil {
				t.Fatal(err)
			}
			if got != tt.want {
				t.Errorf("Verify = %+v, want %+v", got, tt.want)
			}
		})
	}
}

func TestVerifyRejects(t *testing.T) {
	tests := []struct {
		name  string
		token func(t *testing.T) string
		room  string
		want  error
	}{
		{"firma con otro secreto", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte("otro"), nil)
		}, room, ErrInvalidToken},
		{"algoritmo HS512 no permitido", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS512, []byte(secret), nil)
		}, room, ErrInvalidToken},
		{"alg none", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodNone, jwt.UnsafeAllowNoneSignatureType, nil)
		}, room, ErrInvalidToken},
		{"expirado", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["exp"] = now.Add(-time.Minute).Unix() })
		}, room, ErrTokenExpired},
		{"sin exp", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { delete(c, "exp") })
		}, room, ErrInvalidToken},
		{"issuer incorrecto", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["iss"] = "evil" })
		}, room, ErrInvalidToken},
		{"audience incorrecta", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["aud"] = "otra" })
		}, room, ErrInvalidToken},
		{"basura", func(*testing.T) string { return "no.es.jwt" }, room, ErrInvalidToken},
		{"vacío", func(*testing.T) string { return "" }, room, ErrInvalidToken},
		{"token de otra sala", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), nil)
		}, "room-2", ErrRoomMismatch},
		{"rol desconocido", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["role"] = "admin" })
		}, room, ErrUnknownRole},
		{"sin sub", func(t *testing.T) string {
			return sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { delete(c, "sub") })
		}, room, ErrInvalidToken},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := newVerifier().Verify(tt.token(t), tt.room)
			if !errors.Is(err, tt.want) {
				t.Errorf("err = %v, want %v", err, tt.want)
			}
		})
	}
}

func TestCanControl(t *testing.T) {
	if !(Claims{Role: RoleHost}).CanControl() || !(Claims{Role: RoleCohost}).CanControl() {
		t.Error("host y cohost controlan la reproducción")
	}
	if (Claims{Role: RoleParticipant}).CanControl() {
		t.Error("participant no controla la reproducción")
	}
}

func TestNewVerifierUsesRealClock(t *testing.T) {
	v := NewVerifier(secret, iss, aud, "")
	tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) {
		c["exp"] = time.Now().Add(time.Minute).Unix()
	})
	if _, err := v.Verify(tok, room); err != nil {
		t.Fatalf("token vigente con reloj real: %v", err)
	}
}

func TestVerifyExposesJTI(t *testing.T) {
	tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["jti"] = "j-123" })
	got, err := newVerifier().Verify(tok, room)
	if err != nil || got.JTI != "j-123" {
		t.Errorf("Verify = %+v (%v), want JTI j-123", got, err)
	}
}

// Un token expirado pero bien firmado se distingue para cerrar con 4003;
// cualquier otro defecto (firma, iss, aud, sala) prevalece sobre la expiración.
func TestVerifyExpired(t *testing.T) {
	expired := func(c jwt.MapClaims) { c["exp"] = now.Add(-time.Minute).Unix() }
	tok := sign(t, jwt.SigningMethodHS256, []byte(secret), expired)
	if _, err := newVerifier().Verify(tok, room); !errors.Is(err, ErrTokenExpired) || !errors.Is(err, ErrInvalidToken) {
		t.Errorf("expirado: err = %v, want ErrTokenExpired (y ErrInvalidToken)", err)
	}
	tests := []struct {
		name   string
		key    []byte
		mutate func(jwt.MapClaims)
		room   string
		want   error
	}{
		{"otro secreto", []byte("otro"), nil, room, ErrInvalidToken},
		{"issuer incorrecto", []byte(secret), func(c jwt.MapClaims) { c["iss"] = "evil" }, room, ErrInvalidToken},
		{"audience incorrecta", []byte(secret), func(c jwt.MapClaims) { c["aud"] = "otra" }, room, ErrInvalidToken},
		{"sin sub", []byte(secret), func(c jwt.MapClaims) { delete(c, "sub") }, room, ErrInvalidToken},
		{"otra sala", []byte(secret), nil, "room-2", ErrRoomMismatch},
		{"rol desconocido", []byte(secret), func(c jwt.MapClaims) { c["role"] = "admin" }, room, ErrUnknownRole},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tok := sign(t, jwt.SigningMethodHS256, tt.key, func(c jwt.MapClaims) {
				expired(c)
				if tt.mutate != nil {
					tt.mutate(c)
				}
			})
			_, err := newVerifier().Verify(tok, tt.room)
			if !errors.Is(err, tt.want) || errors.Is(err, ErrTokenExpired) {
				t.Errorf("err = %v, want %v sin ErrTokenExpired", err, tt.want)
			}
		})
	}
}

// AC1 de ADR 0030: sync-core solo acepta tickets con su propia audiencia
// (JWT_AUDIENCE) y, durante la transición, la de JWT_LEGACY_AUDIENCE si tiene
// valor. Cualquier otra se rechaza como un token inválido.
func TestVerifyAudiencePerService(t *testing.T) {
	const own, legacy = "tint:sync", "tint:room"
	tests := []struct {
		name   string
		legacy string
		aud    any
		ok     bool
	}{
		{"propia", "", own, true},
		{"propia en lista", "", []string{"tint:otra", own}, true},
		{"analytics", legacy, "tint:analytics", false},
		{"voice", legacy, "tint:voice", false},
		{"social", legacy, "tint:social", false},
		{"stream", legacy, "tint:stream", false},
		{"api", legacy, "tint:api", false},
		{"lista sin la propia", legacy, []string{"tint:voice", "tint:social"}, false},
		{"lista vacía", legacy, []string{}, false},
		{"vacía con la variable vacía", "", "", false},
		{"legada con la variable vacía", "", legacy, false},
		{"legada en lista con la variable vacía", "", []string{legacy}, false},
		{"legada con la variable definida", legacy, legacy, true},
		{"legada en lista con la variable definida", legacy, []string{"tint:voice", legacy}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v := NewVerifier(secret, iss, own, tt.legacy)
			v.now = func() time.Time { return now }
			tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { c["aud"] = tt.aud })
			_, err := v.Verify(tok, room)
			if tt.ok && err != nil {
				t.Fatalf("aud %v debe aceptarse: %v", tt.aud, err)
			}
			if !tt.ok && (!errors.Is(err, ErrInvalidToken) || errors.Is(err, ErrTokenExpired)) {
				t.Fatalf("aud %v: err = %v, want ErrInvalidToken", tt.aud, err)
			}
		})
	}
	// Sin aud no hay audiencia que comprobar: también se rechaza.
	v := NewVerifier(secret, iss, own, legacy)
	v.now = func() time.Time { return now }
	tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) { delete(c, "aud") })
	if _, err := v.Verify(tok, room); !errors.Is(err, ErrInvalidToken) {
		t.Errorf("sin aud: err = %v, want ErrInvalidToken", err)
	}
}

// Un ticket de otro servicio ya vencido no se distingue de uno inválido: no
// llega al cierre 4003 de "token expirado".
func TestVerifyExpiredForeignAudience(t *testing.T) {
	v := NewVerifier(secret, iss, "tint:sync", "tint:room")
	v.now = func() time.Time { return now }
	tok := sign(t, jwt.SigningMethodHS256, []byte(secret), func(c jwt.MapClaims) {
		c["aud"] = "tint:voice"
		c["exp"] = now.Add(-time.Minute).Unix()
	})
	if _, err := v.Verify(tok, room); !errors.Is(err, ErrInvalidToken) || errors.Is(err, ErrTokenExpired) {
		t.Errorf("err = %v, want ErrInvalidToken sin ErrTokenExpired", err)
	}
}
