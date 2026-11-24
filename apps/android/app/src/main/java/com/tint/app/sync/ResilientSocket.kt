package com.tint.app.sync

import com.tint.app.net.TicketRejected
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlin.coroutines.cancellation.CancellationException

/**
 * Conexión WebSocket que se reconecta sola (compartida por los canales Sync, Social y de Voz). Pide el
 * token (el ticket del servicio, ADR 0030) en cada intento, reconecta con backoff exponencial y no reconecta
 * ante `4001` (revocado) ni `4010` (sala cerrada). Un `4003` (ticket caducado) pide un ticket nuevo una vez;
 * el segundo seguido es terminal (se rearma con [resetBackoff]). Si pedir el token falla con [TicketRejected]
 * se trata como su cierre equivalente (terminal); cualquier otro fallo, como un corte (backoff). Ignora los
 * mensajes y cierres de sockets viejos, y es segura entre hilos: los callbacks de OkHttp llegan por su propio hilo.
 */
class ResilientSocket(
    private val token: suspend () -> String?,
    private val urlFor: (token: String) -> String,
    private val sockets: SyncSocketFactory,
    private val scope: CoroutineScope,
    private val sleep: suspend (Long) -> Unit,
    private val listener: Listener,
    /** Cierres en los que no se reconecta, además de 4001, 4010 y el 4003 repetido (p. ej. la voz llena de voice-core). */
    private val extraTerminalCodes: Set<Int> = emptySet(),
) {
    interface Listener {
        /** Antes de abrir cada socket (también en las reconexiones). */
        fun onConnecting() {}

        /** No hay token: no se abre nada ni se reintenta. */
        fun onNoToken() {}

        fun onOpen() {}

        fun onMessage(text: String) {}

        /** El socket se cerró; `reconnectAttempt` es el número de reintento programado, o null si es terminal. */
        fun onClosed(code: Int, reconnectAttempt: Int?) {}
    }

    private val lock = Any()
    private var running = false
    private var generation = 0
    private var reconnectEpoch = 0
    private var socket: SyncSocket? = null
    private var attempt = 0
    private var renewedAfterExpiry = false

    fun start() {
        synchronized(lock) {
            if (running) return
            running = true
            attempt = 0
            renewedAfterExpiry = false
        }
        scope.launch { connect() }
    }

    fun stop() {
        val toClose = synchronized(lock) {
            running = false
            generation++
            reconnectEpoch++
            socket.also { socket = null }
        }
        toClose?.close(1000, "stop")
    }

    /** Envía un frame si hay un socket abierto. */
    fun send(text: String): Boolean {
        val target = synchronized(lock) { socket } ?: return false
        return target.send(text)
    }

    /**
     * El servidor respondió con estado válido: el siguiente corte vuelve a empezar desde 500 ms y un `4003`
     * vuelve a pedir un ticket nuevo.
     */
    fun resetBackoff() {
        synchronized(lock) {
            attempt = 0
            renewedAfterExpiry = false
        }
    }

    private suspend fun connect() {
        val gen = synchronized(lock) {
            if (!running) return
            ++generation
        }
        listener.onConnecting()
        val value = try {
            token()
        } catch (e: CancellationException) {
            throw e
        } catch (e: TicketRejected) {
            // identity-api no da el ticket y no lo dará: como el cierre equivalente, sin reintento.
            handleClosed(gen, e.closeCode, renewable = false)
            return
        } catch (e: Exception) {
            // Red o 5xx al pedir el ticket: como un corte, con backoff.
            handleClosed(gen, 1006)
            return
        }
        if (!current(gen)) return
        if (value == null) {
            listener.onNoToken()
            return
        }
        val opened = sockets.open(urlFor(value), listenerFor(gen))
        synchronized(lock) {
            if (gen == generation && running) socket = opened else opened.close(1000, "stale")
        }
    }

    private fun listenerFor(gen: Int) = object : SyncSocketListener {
        override fun onOpen() {
            if (current(gen)) listener.onOpen()
        }

        override fun onMessage(text: String) {
            if (current(gen)) listener.onMessage(text)
        }

        override fun onClosed(code: Int) = handleClosed(gen, code)

        override fun onFailure() = handleClosed(gen, 1006)
    }

    private fun current(gen: Int): Boolean = synchronized(lock) { gen == generation }

    private fun handleClosed(gen: Int, code: Int, renewable: Boolean = true) {
        val reconnectIn: Long?
        val epoch: Int
        val attemptNow: Int
        synchronized(lock) {
            if (gen != generation) return
            socket = null
            epoch = reconnectEpoch
            // Ticket caducado: se pide otro enseguida, una sola vez hasta que el servidor responda con estado.
            val renew = renewable && code == SyncProtocol.CLOSE_EXPIRED && !renewedAfterExpiry
            if (renew) renewedAfterExpiry = true
            val terminal = code == SyncProtocol.CLOSE_REVOKED || (code == SyncProtocol.CLOSE_EXPIRED && !renew) ||
                code == SyncProtocol.CLOSE_ROOM_CLOSED || code in extraTerminalCodes
            reconnectIn = when {
                terminal || !running -> null
                renew -> 0L
                else -> SyncProtocol.backoffDelay(attempt).also { attempt++ }
            }
            attemptNow = attempt
        }
        listener.onClosed(code, if (reconnectIn != null) attemptNow else null)
        if (reconnectIn != null) {
            scope.launch {
                if (reconnectIn > 0) sleep(reconnectIn)
                val stillValid = synchronized(lock) { running && epoch == reconnectEpoch }
                if (stillValid) connect()
            }
        }
    }
}
