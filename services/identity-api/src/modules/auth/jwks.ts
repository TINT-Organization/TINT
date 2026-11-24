import jwksRsa from 'jwks-rsa';
import { logger } from '../../logging.js';

/**
 * Claves públicas de Firebase (JWKS de Google) para verificar los ID tokens del host.
 *
 * Un fallo al pedirlas es un login perdido (401), y en el servidor de pruebas la salida a Google desde
 * los contenedores se estanca a ratos (conexiones de 3 s a 15 s o con timeout). Por eso:
 * - caché larga: Google rota las claves cada varios días, no cada 10 minutos;
 * - timeout corto y reintentos ante fallos de red (no ante `SigningKeyNotFoundError`);
 * - si Google no responde, se acepta la última clave buena de ese `kid` por un tiempo acotado.
 *   Sigue verificándose firma, `iss`, `aud` y `exp`; solo se tolera no poder refrescar la clave.
 *   Falla cerrado: un `kid` nunca visto, o uno que Google dice que ya no existe, se rechaza.
 */
const CACHE_MAX_AGE_MS = 6 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 5000;
const ATTEMPTS = 3;
const RETRY_DELAYS_MS = [200, 600];
export const STALE_KEY_MAX_AGE_MS = 24 * 60 * 60 * 1000;

const client = jwksRsa({
  cache: true,
  cacheMaxAge: CACHE_MAX_AGE_MS,
  timeout: REQUEST_TIMEOUT_MS,
  rateLimit: true,
  // Los reintentos también cuentan contra este límite.
  jwksRequestsPerMinute: 15,
  jwksUri: 'https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com',
});

const lastGoodKeys = new Map<string, { pem: string; at: number }>();

export function resetJwksStateForTests(): void {
  lastGoodKeys.clear();
}

function fetchKey(kid: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    client.getSigningKey(kid, (err, key) => {
      if (err) return reject(err);
      const signingKey = key?.getPublicKey();
      if (!signingKey) return reject(new Error('No signing key'));
      resolve(signingKey);
    });
  });
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export async function getSigningKey(kid: string): Promise<string> {
  let lastError: unknown;
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    try {
      const pem = await fetchKey(kid);
      lastGoodKeys.set(kid, { pem, at: Date.now() });
      return pem;
    } catch (err) {
      lastError = err;
      // Google respondió y esa clave no está: no es un fallo de red, no se reintenta ni se usa copia.
      if (err instanceof Error && err.name === 'SigningKeyNotFoundError') {
        lastGoodKeys.delete(kid);
        throw err;
      }
      if (attempt < ATTEMPTS - 1) await sleep(RETRY_DELAYS_MS[attempt]);
    }
  }
  const stale = lastGoodKeys.get(kid);
  if (stale && Date.now() - stale.at <= STALE_KEY_MAX_AGE_MS) {
    logger.warn('jwks: Google no respondió, se usa la última clave buena', {
      kid,
      age_s: Math.round((Date.now() - stale.at) / 1000),
      error: lastError instanceof Error ? lastError.message : String(lastError),
    });
    return stale.pem;
  }
  throw lastError;
}
