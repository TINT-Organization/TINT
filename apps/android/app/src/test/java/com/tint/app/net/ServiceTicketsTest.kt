package com.tint.app.net

import com.tint.app.sync.SyncProtocol
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** ADR 0030: un ticket por servicio, pedido con el guest token. */
class ServiceTicketsTest {
    private lateinit var server: MockWebServer
    private lateinit var tokens: InMemoryTokenStore
    private lateinit var tickets: ServiceTickets

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        tokens = InMemoryTokenStore().apply { guestToken = "guest-jwt" }
        val api = ApiClient(server.url("/api/v1").toString().removeSuffix("/"), OkHttpClient(), tokens)
        tickets = ServiceTickets(api, tokens)
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun json(body: String, code: Int = 200) =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    @Test
    fun `pide el ticket del servicio con el guest token aunque haya sesion de host`() = runBlocking {
        tokens.hostToken = "firebase"
        server.enqueue(json("""{"token":"sync-t","expires_at":"2026-11-23T10:00:00Z","audience":"tint:sync"}"""))

        val token = tickets.token("sala 1", TicketService.SYNC)

        assertEquals("sync-t", token)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/rooms/sala+1/tickets/sync", request.path)
        assertEquals("Bearer guest-jwt", request.getHeader("Authorization"))
    }

    @Test
    fun `cada canal pide su propio servicio`() = runBlocking {
        for (service in TicketService.entries) {
            server.enqueue(json("""{"token":"t-${service.wire}"}"""))
            assertEquals("t-${service.wire}", tickets.token("r1", service))
            assertEquals("/api/v1/rooms/r1/tickets/${service.wire}", server.takeRequest().path)
        }
        assertEquals(listOf("sync", "social", "voice", "stream"), TicketService.entries.map { it.wire })
    }

    @Test
    fun `sin guest token no pide nada`() = runBlocking {
        tokens.guestToken = null

        assertNull(tickets.token("r1", TicketService.SOCIAL))
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `los rechazos de identity-api equivalen a cierres terminales`() = runBlocking {
        val cases = listOf(
            json("""{"error":"Forbidden"}""", 403) to SyncProtocol.CLOSE_REVOKED,
            json("""{"error":"Token revoked"}""", 401) to SyncProtocol.CLOSE_REVOKED,
            json("""{"error":"Invalid token"}""", 401) to SyncProtocol.CLOSE_EXPIRED,
            json("""{"error":"Disabled"}""", 410) to SyncProtocol.CLOSE_ROOM_CLOSED,
            json("""{"error":"Not found"}""", 404) to SyncProtocol.CLOSE_ROOM_CLOSED,
        )
        for ((response, code) in cases) {
            tokens.guestToken = "guest-jwt"
            server.enqueue(response)
            try {
                tickets.token("r1", TicketService.VOICE)
                fail("esperaba TicketRejected")
            } catch (e: TicketRejected) {
                assertEquals(code, e.closeCode)
            }
        }
    }

    @Test
    fun `retirado o caducado borra el guest token (falla cerrado)`() = runBlocking {
        server.enqueue(json("""{"error":"Forbidden"}""", 403))
        runCatching { tickets.token("r1", TicketService.SYNC) }
        assertNull(tokens.guestToken)
    }

    @Test
    fun `un 5xx o la red son transitorios y no tocan el guest token`() = runBlocking {
        server.enqueue(json("""{"error":"x"}""", 503))
        try {
            tickets.token("r1", TicketService.STREAM)
            fail("esperaba ApiError")
        } catch (e: ApiError) {
            assertEquals(503, e.status)
        }
        assertEquals("guest-jwt", tokens.guestToken)
    }

    @Test
    fun `una respuesta sin token es un error transitorio`() = runBlocking {
        server.enqueue(json("""{}"""))
        try {
            tickets.token("r1", TicketService.SYNC)
            fail("esperaba ApiError")
        } catch (e: ApiError) {
            assertEquals(0, e.status)
        }
    }
}
