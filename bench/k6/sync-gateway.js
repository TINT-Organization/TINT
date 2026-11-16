// sync.js (sync-core) a través del gateway del banco, sin modificarlo.
// Solo añade la resolución de `localhost` a la IP del servidor: el sitio de Caddy es
// https://localhost (GATEWAY_HOST por defecto) y así se mide el Caddyfile del repo tal cual.
import { options as base, setup, default as sync } from '../../services/sync-core/loadtest/sync.js';

export const options = {
  ...base,
  insecureSkipTLSVerify: true,
  hosts: __ENV.SERVER_IP ? { localhost: __ENV.SERVER_IP } : {},
};
export { setup };
export default sync;
