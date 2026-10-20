package com.dashcamstats.obdlogger

import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal const val WEBHOOK_CREDENTIAL_UNAVAILABLE = "webhook_credential_unavailable"
internal val preferenceWriteLock = Any()

/** commit() can return false after changing memory. Restore that state as well as retrying disk. */
internal fun commitPreferenceUpdate(preferences: SharedPreferences, editor: SharedPreferences.Editor) {
    synchronized(preferenceWriteLock) {
        val before = preferences.all
        if (runCatching { editor.commit() }.getOrDefault(false)) return
        runCatching {
            val restore = preferences.edit().clear()
            for ((key, value) in before) {
                when (value) {
                    is String -> restore.putString(key, value)
                    is Boolean -> restore.putBoolean(key, value)
                    is Int -> restore.putInt(key, value)
                    is Long -> restore.putLong(key, value)
                    is Float -> restore.putFloat(key, value)
                    is Set<*> -> restore.putStringSet(key, value.filterIsInstance<String>().toSet())
                }
            }
            restore.commit()
        }
        throw WebhookCredentialStorageException()
    }
}

/** No platform exception or credential is included in this error or its cause. */
class WebhookCredentialStorageException : IllegalStateException(
    "Could not securely save the webhook key; settings were not saved. Try again or re-enter the key.",
)

internal data class StoredWebhookCredential(val value: String = "", val error: String? = null) {
    override fun toString(): String = "StoredWebhookCredential(configured=${value.isNotEmpty()}, error=$error)"
}

internal interface WebhookCredentialCipher {
    fun encrypt(value: String): String
    fun decrypt(value: String): String
}

/** The wrapping key stays in Android Keystore; only authenticated ciphertext enters preferences. */
internal object AndroidWebhookCredentialCipher : WebhookCredentialCipher {
    private const val ALIAS = "dashcam_obd_webhook_v1"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private val aad = "com.dashcamstats.obdlogger/webhook_api_key/v1".toByteArray(Charsets.UTF_8)

    @Synchronized
    private fun key(create: Boolean): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        // A missing key must never replace the key needed to recover existing ciphertext.
        check(create) { "webhook wrapping key is unavailable" }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setKeySize(256)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    // Boot automation must work without an interactive authentication prompt.
                    .setUserAuthenticationRequired(false)
                    .build(),
            )
            generateKey()
        }
    }

    override fun encrypt(value: String): String = encryptWebhookCredential(value, key(create = true))

    override fun decrypt(value: String): String = decryptWebhookCredential(value, key(create = false))

    internal fun encryptWebhookCredential(value: String, key: SecretKey): String {
        require(value.length in 1..512)
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
            init(Cipher.ENCRYPT_MODE, key)
            updateAAD(aad)
        }
        check(cipher.iv.size == 12)
        val encoded = Base64.getEncoder()
        return "v1:${encoded.encodeToString(cipher.iv)}:${encoded.encodeToString(cipher.doFinal(value.toByteArray(Charsets.UTF_8)))}"
    }

    internal fun decryptWebhookCredential(value: String, key: SecretKey): String {
        require(value.length <= 4_096)
        val parts = value.split(':')
        require(parts.size == 3 && parts[0] == "v1")
        val decoder = Base64.getDecoder()
        val iv = decoder.decode(parts[1])
        val encrypted = decoder.decode(parts[2])
        require(iv.size == 12 && encrypted.size in 17..2_064)
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
            init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, iv))
            updateAAD(aad)
        }
        return Charsets.UTF_8.newDecoder().decode(java.nio.ByteBuffer.wrap(cipher.doFinal(encrypted)))
            .toString().also { require(it.length in 1..512) }
    }
}

internal class WebhookCredentialStore(
    private val preferences: SharedPreferences,
    private val cipher: WebhookCredentialCipher = AndroidWebhookCredentialCipher,
) {
    fun load(): StoredWebhookCredential = try {
        if (preferences.contains(ENCRYPTED_KEY)) {
            val encrypted = checkNotNull(preferences.getString(ENCRYPTED_KEY, null))
            val value = cipher.decrypt(encrypted)
            // Never consult an old plaintext value when an encrypted credential exists.
            if (preferences.contains(LEGACY_KEY)) {
                commitPreferenceUpdate(preferences, preferences.edit().remove(LEGACY_KEY))
            }
            StoredWebhookCredential(value)
        } else {
            val legacy = preferences.getString(LEGACY_KEY, "")?.trim().orEmpty()
            if (legacy.isEmpty()) {
                if (preferences.contains(LEGACY_KEY)) {
                    commitPreferenceUpdate(preferences, preferences.edit().remove(LEGACY_KEY))
                }
                StoredWebhookCredential()
            } else {
                val encrypted = verifiedEncryption(legacy)
                // One synchronous atomic preference commit: remove plaintext only with ciphertext.
                commitPreferenceUpdate(
                    preferences, preferences.edit().putString(ENCRYPTED_KEY, encrypted).remove(LEGACY_KEY),
                )
                StoredWebhookCredential(legacy)
            }
        }
    } catch (_: Exception) {
        // Retain the original stored value for retry/recovery; never fall back to plaintext.
        StoredWebhookCredential(error = WEBHOOK_CREDENTIAL_UNAVAILABLE)
    }

    fun writeTo(editor: SharedPreferences.Editor, value: String, preserveUnreadable: Boolean) {
        if (preserveUnreadable && value.isBlank()) return
        val encrypted = if (value.isBlank()) null else verifiedEncryption(value)
        editor.remove(LEGACY_KEY)
        if (encrypted == null) editor.remove(ENCRYPTED_KEY) else editor.putString(ENCRYPTED_KEY, encrypted)
    }

    private fun verifiedEncryption(value: String): String = try {
        cipher.encrypt(value).also { check(cipher.decrypt(it) == value) }
    } catch (_: Exception) {
        throw WebhookCredentialStorageException()
    }

    companion object {
        const val LEGACY_KEY = "webhook_api_key"
        const val ENCRYPTED_KEY = "webhook_api_key_encrypted_v1"
    }
}
