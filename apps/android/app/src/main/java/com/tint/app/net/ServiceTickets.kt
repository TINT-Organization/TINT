package com.tint.app.net

import com.tint.app.sync.SyncProtocol
import kotlinx.serialization.Serializable
import java.net.URLEncoder

/**
 * Servicios que emiten ticket propio (ADR 0030, mínimo privilegio). La app es invitada, así que no pide
 * `analytics` (identity-api se lo niega con 403 a un invitado).
 */
enum class TicketService(val wire: String) {
    SYNC("sync"),
    SOCIAL("social"),
    VOICE("voice"),
    STREAM("stream"),
}

/**
 * identity-api rechazó el ticket sin remedio. [closeCode] es el cierre de canal equivalente, para que los
 * clientes lo traten igual que si el servicio hubiera cerrado: `4001` (retirado: 403 o `401 Token revoked`),
 * `4003` (la sesión del invitado caducó: otro 401) o `4010` (la sala terminó o ya no existe: 410/404).
 */
class TicketRejected(val closeCode: Int, message: String) : Exception(message)

@Serializable
private class TicketDto(val token: String? = null)

/**
 * Pide `POST /rooms/:id/tickets/:service` con el guest token (espejo de `apps/web/src/lib/serviceTicket.ts`).
 * El guest token solo viaja a identity-api: cada canal abre su servicio con un ticket de vida corta y
 * audiencia `tint:<service>`, que los demás servicios rechazan. Se pide uno nuevo en cada (re)conexión.
 */
class ServiceTickets(private val api: ApiClient, private val tokens: TokenStore) {
    /**
     * El token del ticket, o null si no hay sesión de invitado (no se pide nada).
     * @throws TicketRejected si identity-api lo rechaza sin remedio (borra el guest token si fue retirado o caducó).
     * @throws ApiError si es transitorio (red, 5xx, respuesta sin token): conviene reintentar.
     */
    suspend fun token(roomId: String, service: TicketService): String? {
        if (tokens.guestToken == null) return null
        val path = "/rooms/${URLEncoder.encode(roomId, "UTF-8")}/tickets/${service.wire}"
        val body = try {
            api.request("POST", path, null, ApiAuth.GUEST)
        } catch (e: ApiError) {
            throw rejectionFor(e) ?: e
        }
        val dto = runCatching { api.json.decodeFromString<TicketDto>(body) }.getOrNull()
        return dto?.token?.takeIf { it.isNotEmpty() } ?: throw ApiError(0, "Ticket ${service.wire} sin token")
    }

    private fun rejectionFor(e: ApiError): TicketRejected? {
        val code = when {
            e.status == 403 || (e.status == 401 && e.message == "Token revoked") -> SyncProtocol.CLOSE_REVOKED
            e.status == 401 -> SyncProtocol.CLOSE_EXPIRED
            e.status == 404 || e.status == 410 -> SyncProtocol.CLOSE_ROOM_CLOSED
            else -> return null
        }
        // Falla cerrado, como PlaybackRepository: un invitado retirado o caducado ya no tiene sesión.
        if (code != SyncProtocol.CLOSE_ROOM_CLOSED) tokens.guestToken = null
        return TicketRejected(code, e.message ?: "")
    }
}
