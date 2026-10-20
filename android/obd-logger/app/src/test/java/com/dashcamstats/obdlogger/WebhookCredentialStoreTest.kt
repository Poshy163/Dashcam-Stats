package com.dashcamstats.obdlogger

import android.content.Context
import android.content.SharedPreferences
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/** Uses the real AES/GCM envelope with a disposable JVM key, never a production credential. */
internal class TestWebhookCredentialCipher : WebhookCredentialCipher {
    private val key: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    override fun encrypt(value: String): String = AndroidWebhookCredentialCipher.encryptWebhookCredential(value, key)
    override fun decrypt(value: String): String = AndroidWebhookCredentialCipher.decryptWebhookCredential(value, key)
}

@RunWith(RobolectricTestRunner::class)
class WebhookCredentialStoreTest {
    private lateinit var context: Context
    private lateinit var prefs: SharedPreferences
    private lateinit var cipher: TestWebhookCredentialCipher
    private lateinit var store: WebhookCredentialStore

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        prefs = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        cipher = TestWebhookCredentialCipher()
        store = WebhookCredentialStore(prefs, cipher)
    }

    @Test
    fun legacyCredentialMigratesAndSurvivesNewStoreInstance() {
        prefs.edit().putString(WebhookCredentialStore.LEGACY_KEY, " configured-value ").commit()
        assertEquals(StoredWebhookCredential("configured-value"), store.load())
        assertFalse(prefs.contains(WebhookCredentialStore.LEGACY_KEY))
        val encoded = prefs.getString(WebhookCredentialStore.ENCRYPTED_KEY, "")!!
        assertFalse(encoded.contains("configured-value"))
        assertEquals("configured-value", WebhookCredentialStore(prefs, cipher).load().value)
    }

    @Test
    fun encryptionUsesFreshIvAndRejectsWrongKeyAndModifiedCiphertext() {
        val first = cipher.encrypt("configured-value")
        val second = cipher.encrypt("configured-value")
        assertNotEquals(first, second)
        assertEquals("configured-value", cipher.decrypt(first))
        assertThrows(Exception::class.java) { TestWebhookCredentialCipher().decrypt(first) }
        val parts = first.split(':')
        val bytes = java.util.Base64.getDecoder().decode(parts[2])
        bytes[0] = (bytes[0].toInt() xor 1).toByte()
        val changed = "${parts[0]}:${parts[1]}:${java.util.Base64.getEncoder().encodeToString(bytes)}"
        assertThrows(Exception::class.java) { cipher.decrypt(changed) }
    }

    @Test
    fun malformedAndOversizedEnvelopesAreRejected() {
        listOf("", "v2:bad:bad", "v1:AA==:AA==", "v1:*:*", "x".repeat(4097)).forEach {
            assertThrows(Exception::class.java) { cipher.decrypt(it) }
        }
        val unicode = "\uD83D\uDE97".repeat(256)
        assertEquals(unicode, cipher.decrypt(cipher.encrypt(unicode)))
        assertThrows(Exception::class.java) { cipher.encrypt("x".repeat(513)) }
    }

    @Test
    fun failedMigrationRetainsPlaintextForRetryButDoesNotUseIt() {
        prefs.edit().putString(WebhookCredentialStore.LEGACY_KEY, "configured-value").commit()
        val unavailable = WebhookCredentialStore(prefs, failingCipher())
        assertEquals(StoredWebhookCredential(error = WEBHOOK_CREDENTIAL_UNAVAILABLE), unavailable.load())
        assertEquals("configured-value", prefs.getString(WebhookCredentialStore.LEGACY_KEY, null))
        assertFalse(prefs.contains(WebhookCredentialStore.ENCRYPTED_KEY))
        assertEquals("configured-value", store.load().value)
    }

    @Test
    fun failedMigrationCommitRestoresMemoryAndDoesNotReturnUnpersistedCredential() {
        prefs.edit().putString(WebhookCredentialStore.LEGACY_KEY, "configured-value").commit()
        val failingPreferences = failingDiskPreferences()
        assertEquals(
            StoredWebhookCredential(error = WEBHOOK_CREDENTIAL_UNAVAILABLE),
            WebhookCredentialStore(failingPreferences, cipher).load(),
        )
        assertTrue(prefs.contains(WebhookCredentialStore.LEGACY_KEY))
        assertFalse(prefs.contains(WebhookCredentialStore.ENCRYPTED_KEY))
    }

    @Test
    fun failedSettingsCommitRestoresAlreadyChangedMemoryIncludingCredential() {
        val config = LoggerConfig(false, false, "", "vehicle", "logger", webhookApiKey = "configured-value")
        LoggerPreferences.save(context, config, store)
        val before = prefs.all
        val failedPreferences = failingDiskPreferences()
        val editor = failedPreferences.edit().putBoolean("enabled", true).putString("vehicle_id", "changed")
        WebhookCredentialStore(failedPreferences, cipher).writeTo(editor, "replacement", false)
        assertThrows(WebhookCredentialStorageException::class.java) {
            commitPreferenceUpdate(failedPreferences, editor)
        }
        assertEquals(before, prefs.all)
        assertEquals("configured-value", store.load().value)
    }

    @Test
    fun unreadableCiphertextNeverFallsBackToStalePlaintextOrDeletesEvidence() {
        prefs.edit()
            .putString(WebhookCredentialStore.ENCRYPTED_KEY, "v1:corrupt:encrypted")
            .putString(WebhookCredentialStore.LEGACY_KEY, "stale-value")
            .commit()
        assertEquals(StoredWebhookCredential(error = WEBHOOK_CREDENTIAL_UNAVAILABLE), store.load())
        assertEquals("v1:corrupt:encrypted", prefs.getString(WebhookCredentialStore.ENCRYPTED_KEY, null))
        assertEquals("stale-value", prefs.getString(WebhookCredentialStore.LEGACY_KEY, null))
    }

    @Test
    fun settingsSaveEncryptsCredentialAndFailedEncryptionPreservesSettings() {
        val config = LoggerConfig(false, false, "", "vehicle", "logger", webhookApiKey = "configured-value")
        LoggerPreferences.save(context, config, store)
        assertFalse(prefs.contains(WebhookCredentialStore.LEGACY_KEY))
        assertEquals("configured-value", LoggerPreferences.load(context, store).webhookApiKey)
        val before = prefs.all
        val failure = assertThrows(WebhookCredentialStorageException::class.java) {
            LoggerPreferences.save(context, config.copy(vehicleId = "changed"), WebhookCredentialStore(prefs, failingCipher()))
        }
        assertEquals(before, prefs.all)
        assertNull(failure.cause)
        assertFalse(failure.toString().contains("sensitive-platform-error"))
    }

    @Test
    fun unrelatedSavePreservesUnreadableCredentialUntilExplicitReplacement() {
        prefs.edit().putString(WebhookCredentialStore.ENCRYPTED_KEY, "unreadable").commit()
        val unavailable = LoggerPreferences.load(context, store)
        assertEquals(WEBHOOK_CREDENTIAL_UNAVAILABLE, unavailable.webhookCredentialError)
        assertEquals(WEBHOOK_CREDENTIAL_UNAVAILABLE, webhookConfigurationError(unavailable))
        LoggerPreferences.save(context, unavailable.copy(webhookEnabled = false, vehicleId = "changed"), store)
        assertEquals("unreadable", prefs.getString(WebhookCredentialStore.ENCRYPTED_KEY, null))
        assertEquals("changed", prefs.getString("vehicle_id", null))
        LoggerPreferences.save(context, unavailable.copy(webhookApiKey = "replacement"), store)
        assertEquals(StoredWebhookCredential("replacement"), store.load())
    }

    @Test
    fun startupGateDoesNotDecryptOrMigrateCredentials() {
        prefs.edit().putString(WebhookCredentialStore.LEGACY_KEY, "configured-value").commit()
        val config = LoggerPreferences.load(context, includeWebhookCredential = false)
        assertEquals("", config.webhookApiKey)
        assertNull(config.webhookCredentialError)
        assertEquals("configured-value", prefs.getString(WebhookCredentialStore.LEGACY_KEY, null))
        assertFalse(prefs.contains(WebhookCredentialStore.ENCRYPTED_KEY))
    }

    @Test
    fun startupGateDoesNotWaitForCredentialLoadMonitor() {
        val entered = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val slowCipher = object : WebhookCredentialCipher by cipher {
            override fun decrypt(value: String): String {
                entered.countDown()
                check(release.await(5, java.util.concurrent.TimeUnit.SECONDS))
                return cipher.decrypt(value)
            }
        }
        prefs.edit().putString(WebhookCredentialStore.ENCRYPTED_KEY, cipher.encrypt("configured-value")).commit()
        val executor = java.util.concurrent.Executors.newSingleThreadExecutor()
        try {
            val pending = executor.submit<LoggerConfig> {
                LoggerPreferences.load(context, WebhookCredentialStore(prefs, slowCipher))
            }
            assertTrue(entered.await(5, java.util.concurrent.TimeUnit.SECONDS))
            // If this enters the credential monitor, the worker times out and the assertion fails.
            assertEquals("", LoggerPreferences.load(context, includeWebhookCredential = false).webhookApiKey)
            assertEquals(1L, release.count)
            release.countDown()
            assertEquals("configured-value", pending.get(5, java.util.concurrent.TimeUnit.SECONDS).webhookApiKey)
        } finally {
            release.countDown()
            executor.shutdownNow()
        }
    }

    @Test
    fun diagnosticStringRepresentationsRedactCredential() {
        assertFalse(StoredWebhookCredential("configured-value").toString().contains("configured-value"))
        assertFalse(LoggerConfig(false, false, "", "vehicle", "logger", webhookApiKey = "configured-value")
            .toString().contains("configured-value"))
    }

    private fun failingCipher(): WebhookCredentialCipher = object : WebhookCredentialCipher {
        override fun encrypt(value: String): String = error("sensitive-platform-error")
        override fun decrypt(value: String): String = error("sensitive-platform-error")
    }

    private fun failingDiskPreferences(): SharedPreferences = object : SharedPreferences by prefs {
        override fun edit(): SharedPreferences.Editor {
            val editor = prefs.edit()
            return object : SharedPreferences.Editor by editor {
                override fun commit(): Boolean {
                    // Android updates memory before it reports a failed disk write.
                    editor.commit()
                    return false
                }
                override fun clear(): SharedPreferences.Editor { editor.clear(); return this }
                override fun putString(key: String?, value: String?): SharedPreferences.Editor {
                    editor.putString(key, value); return this
                }
                override fun remove(key: String?): SharedPreferences.Editor { editor.remove(key); return this }
                override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor {
                    editor.putBoolean(key, value); return this
                }
            }
        }
    }
}
