package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.HttpURLConnection
import java.net.URL

@RunWith(RobolectricTestRunner::class)
class LoggerConfigTest {
    private lateinit var context: Context

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
    }

    @After
    fun clearPreferences() {
        context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE).edit().clear().commit()
    }

    @Test
    fun freshInstallHasNoWebhookCredential() {
        assertEquals("", LoggerPreferences.load(context).webhookApiKey)
    }

    @Test
    fun existingExplicitWebhookCredentialIsPreserved() {
        context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE).edit()
            .putString("webhook_api_key", "configured-value")
            .commit()

        assertEquals("configured-value", LoggerPreferences.load(context).webhookApiKey)
    }

    @Test
    fun cleartextWebhookIsLimitedToPrivateOrLoopbackAddresses() {
        assertTrue(isAllowedWebhookUrl("http://192.168.50.4:8199/api/ingest/webhook"))
        assertTrue(isAllowedWebhookUrl("http://10.0.0.2/api/ingest/webhook"))
        assertTrue(isAllowedWebhookUrl("http://172.31.0.2/api/ingest/webhook"))
        assertTrue(isAllowedWebhookUrl("http://localhost/api/ingest/webhook"))
        assertFalse(isAllowedWebhookUrl("http://example.com/hook"))
        assertFalse(isAllowedWebhookUrl("http://172.32.0.2/hook"))
        assertFalse(isAllowedWebhookUrl("ftp://192.168.1.16/hook"))
    }

    @Test
    fun httpsWebhookAllowsDnsWithoutUrlCredentialsOrFragments() {
        assertTrue(isAllowedWebhookUrl("https://dashcam.example.test/api/ingest/webhook"))
        assertFalse(isAllowedWebhookUrl("https://user:secret@dashcam.example.test/hook"))
        assertFalse(isAllowedWebhookUrl("https://dashcam.example.test/hook#fragment"))
        assertFalse(isAllowedWebhookUrl("https://dashcam.example.test/other"))
        assertFalse(isAllowedWebhookUrl("https://dashcam.example.test/api/ingest/webhook?redirect=true"))
    }

    @Test
    fun enabledWebhookRequiresBoundedExplicitCredential() {
        val base = LoggerConfig(false, false, "", "vehicle", "logger")
        assertEquals("webhook_api_key_required", webhookConfigurationError(base))
        assertEquals(null, webhookConfigurationError(base.copy(webhookEnabled = false)))
        assertEquals(
            "webhook_api_key_too_long",
            webhookConfigurationError(base.copy(webhookApiKey = "x".repeat(513))),
        )
        assertEquals(null, webhookConfigurationError(base.copy(webhookApiKey = "configured-value")))
    }

    @Test
    fun authenticatedWebhookConnectionDoesNotFollowRedirects() {
        val connection = FakeHttpConnection()
        configureWebhookConnection(connection, "configured-value")

        assertFalse(connection.instanceFollowRedirects)
        assertEquals("POST", connection.requestMethod)
        assertEquals("configured-value", connection.getRequestProperty("X-API-Key"))
    }

    @Test
    fun webhookResponseCategoriesAreBoundedAndActionable() {
        assertEquals(WebhookResult(true, "webhook_ok"), classifyWebhookResponse(204))
        assertEquals("webhook_auth_rejected", classifyWebhookResponse(401).reasonCode)
        assertEquals("webhook_client_error", classifyWebhookResponse(429).reasonCode)
        assertEquals("webhook_server_error", classifyWebhookResponse(503).reasonCode)
        assertEquals("webhook_unexpected_response", classifyWebhookResponse(302).reasonCode)
    }

    private class FakeHttpConnection : HttpURLConnection(URL("http://127.0.0.1/unused")) {
        override fun connect() = Unit
        override fun disconnect() = Unit
        override fun usingProxy(): Boolean = false
    }
}
