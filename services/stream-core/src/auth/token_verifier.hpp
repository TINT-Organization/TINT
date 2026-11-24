#pragma once

#include <cstdint>
#include <expected>
#include <string>
#include <string_view>

namespace stream::auth {

// Claims del contrato compartido con identity-api (guest token y sync-ticket, HS256).
struct Claims {
  std::string sub;
  std::string room_id;
  std::string role;  // host | cohost | participant
  std::string nick;
  std::string jti;
  std::int64_t exp = 0;

  bool is_host() const { return role == "host" || role == "cohost"; }
};

enum class TokenError {
  Malformed,
  UnsupportedAlgorithm,
  BadSignature,
  Expired,
  WrongIssuer,
  WrongAudience,
  MissingClaim,
  InvalidRole,
};

std::string_view to_string(TokenError error);

// Verifica firma HS256, iss, aud, exp y los claims obligatorios. Nunca confía en un
// token sin verificar la firma: Expired solo se devuelve si la firma es válida.
// aud (string o lista, RFC 7519) debe contener la audiencia propia del servicio (tint:stream,
// ADR 0030) o, solo si no está vacía, la legada de la transición (JWT_LEGACY_AUDIENCE).
class TokenVerifier {
 public:
  TokenVerifier(std::string secret, std::string issuer, std::string audience,
                std::string legacy_audience = {});

  std::expected<Claims, TokenError> verify(std::string_view token, std::int64_t now_s) const;

 private:
  std::string secret_;
  std::string issuer_;
  std::string audience_;
  std::string legacy_audience_;  // vacía = desactivada (falla cerrado)
};

}  // namespace stream::auth
