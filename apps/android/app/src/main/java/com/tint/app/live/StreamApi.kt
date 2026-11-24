package com.tint.app.live

import com.tint.app.net.ApiError
import com.tint.app.net.TicketRejected
import com.tint.app.sync.SyncProtocol
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import kotlin.coroutines.cancellation.CancellationException

/** Respuesta de WHEP: la respuesta SDP y la URL de la sesión (para cerrarla con `DELETE`). */
data class WhepSession(val answerSdp: String, val sessionUrl: String)

/** Lo que el espectador necesita de stream-core (se sustituye en las pruebas). */
interface StreamSignaling {
    suspend fun iceServers(roomId: String): List<IceServer>

    suspend fun whep(roomId: String, offerSdp: String): WhepSession

    /** Libera el lugar en el cupo. Nunca falla: si la sesión ya no existe o no hay red, no hay nada que hacer. */
    suspend fun delete(sessionUrl: String)
}

/**
 * Señalización WHEP por HTTP contra stream-core (rutas bajo `/api/v1/stream`, directo o por el gateway) con el ticket
 * `stream` de identity-api (ADR 0030), igual que `useLiveStream.ts` de la web. `baseUrl` es el origen, sin la ruta.
 *
 * `token` pide un ticket nuevo: se llama al empezar cada intento ([iceServers]) y WHEP y `DELETE` reutilizan ese
 * ticket. Un [TicketRejected] se traduce al error terminal equivalente; un fallo transitorio, a uno reintentable.
 */
class StreamApi(
    baseUrl: String,
    private val http: OkHttpClient,
    private val token: suspend () -> String?,
) : StreamSignaling {
    private val base = baseUrl.trimEnd('/')

    @Volatile
    private var ticket: String? = null

    private class Reply(val code: Int, val body: String, val location: String?)

    override suspend fun iceServers(roomId: String): List<IceServer> {
        ticket = null // nuevo intento: ticket nuevo
        val reply = call(Request.Builder().url(roomUrl(roomId, "ice-servers")).get())
        if (reply.code !in 200..299) throw StreamProtocol.errorFor(reply.code, reply.body)
        return StreamProtocol.parseIceServers(reply.body)
    }

    override suspend fun whep(roomId: String, offerSdp: String): WhepSession {
        val body = offerSdp.toRequestBody(SDP)
        val reply = call(Request.Builder().url(roomUrl(roomId, "whep")).post(body))
        if (reply.code !in 200..299) throw StreamProtocol.errorFor(reply.code, reply.body)
        return WhepSession(reply.body, reply.location?.let { StreamProtocol.sessionUrl(base, it) }.orEmpty())
    }

    override suspend fun delete(sessionUrl: String) {
        if (sessionUrl.isEmpty()) return
        try {
            call(Request.Builder().url(sessionUrl).delete())
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Sin red o sesión ya cerrada: el servidor libera el lugar por su cuenta cuando se cae la conexión.
        }
    }

    private fun roomUrl(roomId: String, action: String): HttpUrl =
        base.toHttpUrl().newBuilder()
            .addPathSegments("api/v1/stream/rooms")
            .addPathSegment(roomId)
            .addPathSegment(action)
            .build()

    private suspend fun currentTicket(): String? {
        ticket?.let { return it }
        val fresh = try {
            token()
        } catch (e: CancellationException) {
            throw e
        } catch (e: TicketRejected) {
            val code = when (e.closeCode) {
                SyncProtocol.CLOSE_REVOKED -> "TOKEN_REVOKED"
                SyncProtocol.CLOSE_EXPIRED -> "TOKEN_EXPIRED"
                else -> "ROOM_DISABLED"
            }
            val status = if (code == "ROOM_DISABLED") 410 else 401
            throw StreamError(status, code, StreamProtocol.errorMessage(code))
        } catch (e: ApiError) {
            throw if (e.status >= 500 || e.status == 429) {
                StreamError(e.status, "UNAVAILABLE", StreamProtocol.errorMessage("UNAVAILABLE"))
            } else {
                StreamError(0, "NETWORK", StreamProtocol.errorMessage("NETWORK"))
            }
        }
        ticket = fresh
        return fresh
    }

    private suspend fun call(builder: Request.Builder): Reply {
        val bearer = currentTicket() ?: throw StreamProtocol.errorFor(401, null)
        val request = builder.header("Authorization", "Bearer $bearer").build()
        return withContext(Dispatchers.IO) {
            try {
                http.newCall(request).execute().use { Reply(it.code, it.body?.string().orEmpty(), it.header("Location")) }
            } catch (e: IOException) {
                throw StreamError(0, "NETWORK", StreamProtocol.errorMessage("NETWORK"))
            }
        }
    }

    private companion object {
        val SDP = "application/sdp".toMediaType()
    }
}
