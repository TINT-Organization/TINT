package com.tint.app.live

import com.tint.app.net.ApiError
import com.tint.app.net.TicketRejected
import com.tint.app.sync.SyncProtocol
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class StreamApiTest {
    private lateinit var server: MockWebServer
    /** Ticket `stream` que devolvería identity-api (ADR 0030); cuenta cuántos se piden. */
    private var token: String? = "guest-jwt"
    private var ticketError: Exception? = null
    private var ticketsRequested = 0
    private lateinit var api: StreamApi

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        api = StreamApi(server.url("/").toString().trimEnd('/'), OkHttpClient(), token = {
            ticketsRequested++
            ticketError?.let { throw it }
            token
        })
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun json(body: String, code: Int = 200) =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    private fun failure(block: suspend () -> Unit): StreamError = runBlocking {
        try {
            block()
            fail("debía fallar")
            throw IllegalStateException()
        } catch (e: StreamError) {
            e
        }
    }

    @Test
    fun `pide los servidores ICE de la sala con el token de invitado`() = runBlocking {
        server.enqueue(json("""{"ice_servers":[{"urls":"stun:s:3478"}]}"""))

        val servers = api.iceServers("room 1")

        assertEquals(listOf("stun:s:3478"), servers.single().urls)
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/v1/stream/rooms/room%201/ice-servers", request.path)
        assertEquals("Bearer guest-jwt", request.getHeader("Authorization"))
    }

    @Test
    fun `WHEP manda la oferta como application sdp y devuelve la respuesta y la sesion`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(201).setHeader("Content-Type", "application/sdp")
                .setHeader("Location", "/api/v1/stream/sessions/s-42").setBody("v=0 respuesta"),
        )

        val session = api.whep("r1", "v=0 oferta")

        assertEquals("v=0 respuesta", session.answerSdp)
        assertEquals(server.url("/api/v1/stream/sessions/s-42").toString(), session.sessionUrl)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/stream/rooms/r1/whep", request.path)
        assertEquals("Bearer guest-jwt", request.getHeader("Authorization"))
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/sdp"))
        assertEquals("v=0 oferta", request.body.readUtf8())
    }

    @Test
    fun `WHEP sin Location deja la sesion vacia`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(201).setBody("v=0"))

        assertEquals("", api.whep("r1", "o").sessionUrl)
    }

    @Test
    fun `los errores de WHEP traen el codigo del servidor`() {
        server.enqueue(json("""{"error":"ROOM_FULL","message":"lleno"}""", 409))
        val full = failure { api.whep("r1", "o") }
        assertEquals(409, full.status)
        assertEquals("ROOM_FULL", full.code)

        server.enqueue(json("""{"error":"ROOM_DISABLED"}""", 410))
        assertEquals("ROOM_DISABLED", failure { api.whep("r1", "o") }.code)

        server.enqueue(MockResponse().setResponseCode(404))
        assertEquals("STREAM_NOT_FOUND", failure { api.whep("r1", "o") }.code)

        server.enqueue(MockResponse().setResponseCode(502).setBody("<html>bad gateway</html>"))
        val bad = failure { api.whep("r1", "o") }
        assertEquals(502, bad.status)
        assertEquals("UNAVAILABLE", bad.code)
    }

    @Test
    fun `un fallo de ICE tambien es un StreamError`() {
        server.enqueue(json("""{"error":"TOKEN_EXPIRED"}""", 401))

        val e = failure { api.iceServers("r1") }

        assertEquals(401, e.status)
        assertEquals("TOKEN_EXPIRED", e.code)
    }

    @Test
    fun `sin red el error es de estado 0 y se puede reintentar`() {
        server.shutdown()

        val e = failure { api.iceServers("r1") }

        assertEquals(0, e.status)
        assertEquals("NETWORK", e.code)
        assertTrue(!StreamProtocol.isTerminal(e))
    }

    @Test
    fun `cada intento pide un ticket nuevo con los servidores ICE y WHEP lo reutiliza`() = runBlocking {
        server.enqueue(json("""{"ice_servers":[]}"""))
        server.enqueue(MockResponse().setResponseCode(201).setBody("v=0"))
        api.iceServers("r1")
        token = "stream-2"
        api.whep("r1", "o")
        assertEquals(1, ticketsRequested)
        assertEquals("Bearer guest-jwt", server.takeRequest().getHeader("Authorization"))
        assertEquals("Bearer guest-jwt", server.takeRequest().getHeader("Authorization"))

        server.enqueue(json("""{"ice_servers":[]}"""))
        api.iceServers("r1")
        assertEquals(2, ticketsRequested)
        assertEquals("Bearer stream-2", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `un ticket rechazado es un error terminal con su codigo y no llega a stream-core`() {
        ticketError = TicketRejected(SyncProtocol.CLOSE_REVOKED, "Forbidden")
        val revoked = failure { api.iceServers("r1") }
        assertEquals("TOKEN_REVOKED", revoked.code)
        assertTrue(StreamProtocol.isTerminal(revoked))

        ticketError = TicketRejected(SyncProtocol.CLOSE_EXPIRED, "Invalid token")
        assertEquals("TOKEN_EXPIRED", failure { api.iceServers("r1") }.code)

        ticketError = TicketRejected(SyncProtocol.CLOSE_ROOM_CLOSED, "Disabled")
        assertEquals("ROOM_DISABLED", failure { api.iceServers("r1") }.code)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `un fallo transitorio al pedir el ticket se puede reintentar`() {
        ticketError = ApiError(0, "sin red")
        val network = failure { api.iceServers("r1") }
        assertEquals("NETWORK", network.code)
        assertTrue(!StreamProtocol.isTerminal(network))

        ticketError = ApiError(503, "x")
        val unavailable = failure { api.iceServers("r1") }
        assertEquals("UNAVAILABLE", unavailable.code)
        assertTrue(!StreamProtocol.isTerminal(unavailable))
    }

    @Test
    fun `sin token no se hace ninguna peticion`() {
        token = null

        val e = failure { api.whep("r1", "o") }

        assertEquals(401, e.status)
        assertEquals("UNAUTHORIZED", e.code)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `cerrar la sesion hace DELETE con el token`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200))

        api.delete(server.url("/api/v1/stream/sessions/s-1").toString())

        val request = server.takeRequest()
        assertEquals("DELETE", request.method)
        assertEquals("/api/v1/stream/sessions/s-1", request.path)
        assertEquals("Bearer guest-jwt", request.getHeader("Authorization"))
    }

    @Test
    fun `cerrar una sesion que ya no existe o sin red no falla`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(404))
        api.delete(server.url("/api/v1/stream/sessions/s-1").toString())

        api.delete("") // sesión desconocida: no hay nada que cerrar
        assertEquals(1, server.requestCount)

        server.shutdown()
        api.delete("http://127.0.0.1:1/api/v1/stream/sessions/x") // sin red: tampoco falla
    }
}
