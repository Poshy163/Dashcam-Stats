package com.dashcamstats.obdlogger

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.time.Instant

internal data class TrialSettingChange(
    val revision: Int,
    val enabled: Boolean,
    val fallbackReason: String?,
    val source: String,
    val changedAt: String?,
) {
    fun json(): JSONObject = JSONObject().put("revision", revision).put("enabled", enabled)
        .put("fallback_reason", fallbackReason ?: JSONObject.NULL).put("source", source)
        .put("changed_at_utc", changedAt ?: JSONObject.NULL)
}

internal data class TrialSettingsSnapshot(
    val enabled: Boolean,
    val fallbackReason: String?,
    val revision: Int,
    val source: String,
    val changedAt: String?,
    val storageState: String,
    val error: String?,
    val legacyKeyPresent: Boolean,
    val recentChanges: List<TrialSettingChange>,
    internal val token: String,
) {
    fun json(): JSONObject = JSONObject().put("schema_version", 1).put("enabled", enabled)
        .put("fallback_reason", fallbackReason ?: JSONObject.NULL).put("revision", revision)
        .put("source", source).put("changed_at_utc", changedAt ?: JSONObject.NULL)
        .put("storage_state", storageState).put("error", error ?: JSONObject.NULL)
        .put("legacy_key_present", legacyKeyPresent)
        .put("recent_changes", JSONArray(recentChanges.map { it.json() }))
}

/** A stale form must never clear a fallback that happened after the form was rendered. */
internal class StaleTrialSettingsException : IllegalStateException("Trial state changed; review it and apply again.")
internal class TrialSettingsStorageException : IllegalStateException("Trial setting could not be saved; baseline remains selected.")

internal interface TrialSettingsStorage {
    fun read(): ByteArray?
    fun write(bytes: ByteArray)
}

internal data class LegacyTrialSetting(val present: Boolean, val enabled: Boolean, val fallbackReason: String?)

/** Separate from general settings: neither another form save nor an app restart can rewrite intent. */
internal class TrialSettingsRepository(
    private val storage: TrialSettingsStorage,
    private val legacy: () -> LegacyTrialSetting,
    private val migratedRevision: () -> Int?,
    private val markMigrated: (Int) -> Boolean,
    private val mirror: (TrialSettingChange) -> Boolean,
    private val readBarrier: () -> TrialSettingChange?,
    private val writeBarrier: (TrialSettingChange?) -> Boolean,
    private val now: () -> String? = { Instant.now().toString() },
) {
    private data class Record(val state: TrialSettingChange, val legacyPresent: Boolean, val changes: List<TrialSettingChange>)
    private var lastGood: Record? = null
    private var failure: String? = null
    private var knownMigrated = false
    private var cached: TrialSettingsSnapshot? = null

    @Synchronized fun snapshot(): TrialSettingsSnapshot {
        cached?.let { return it }
        val bytes = try { storage.read() } catch (_: Exception) {
            knownMigrated = true
            runCatching { markMigrated(1) }
            return unavailable("read_failed", "unreadable")
        }
        val token = bytes?.let(::digest) ?: "missing"
        val anchor = runCatching { migratedRevision() }.getOrElse { return unavailable("read_failed", token) }
        val record = if (bytes == null) {
            if (knownMigrated || anchor != null) return unavailable("missing_record", token)
            // Establish the one-time boundary before attempting the first primary write.
            // A failed migration must not later reinterpret an old ON preference as new intent.
            knownMigrated = true
            runCatching { markMigrated(1) }
            val previous = runCatching { legacy() }.getOrElse { return unavailable("legacy_invalid", token) }
            if (previous.fallbackReason != null && previous.fallbackReason !in PollTrialPlan.fallbackReasons)
                return unavailable("legacy_invalid", token)
            val initial = TrialSettingChange(1, previous.enabled && previous.fallbackReason == null,
                previous.fallbackReason, "legacy_migration", null)
            val migrated = Record(initial, previous.present, listOf(initial))
            if (!write(migrated)) return unavailable("write_failed", token)
            migrated
        } else {
            knownMigrated = true
            runCatching { decode(bytes) }.getOrElse {
                runCatching { if (migratedRevision() == null) markMigrated(1) }
                return unavailable("corrupt_record", token)
            }
        }
        // The independent revision anchor must never be moved back by an older,
        // checksum-valid file restored after a partial storage/rename rollback.
        if (anchor != null && anchor > record.state.revision) return unavailable("corrupt_record", token)
        lastGood = record
        knownMigrated = true
        // A disabling decision writes this safety barrier before replacing an armed record.
        // If the primary write fails, a fresh process still cannot revive that old ON state.
        val barrier = runCatching { readBarrier()?.also {
            require(it.revision > 0 && !it.enabled && it.source in setOf("operator", "health") &&
                ((it.source == "operator" && it.fallbackReason == null) ||
                    (it.source == "health" && it.fallbackReason in PollTrialPlan.fallbackReasons)))
        } }.getOrElse { return unavailable("read_failed", token) }
        if (barrier != null && barrier.revision >= record.state.revision && record.state != barrier) {
            val blocked = Record(barrier, record.legacyPresent,
                (record.changes.filter { it.revision < barrier.revision } + barrier).takeLast(HISTORY_LIMIT))
            lastGood = blocked
            if (!write(blocked)) return unavailable("write_failed", token)
            failure = null
            return remember(ready(blocked, synchronize(blocked), digest(encode(blocked))))
        }
        if (failure != null) return unavailable(checkNotNull(failure), token)
        return remember(ready(record, synchronize(record), digest(encode(record))))
    }

    @Synchronized fun change(enabled: Boolean, reason: String?, expected: TrialSettingsSnapshot? = null): TrialSettingsSnapshot {
        require(reason == null || reason in PollTrialPlan.fallbackReasons)
        require(!enabled || reason == null)
        // Ordinary polls use the validated in-process state. An explicit repair can
        // retry disk access; a newly readable value makes an old form stale for review.
        if (cached?.storageState == "unavailable") { cached = null; failure = null }
        val before = snapshot()
        if (expected != null && (before.token != expected.token || before.revision != expected.revision))
            throw StaleTrialSettingsException()
        if (before.storageState == "ready" && before.enabled == enabled &&
            (before.fallbackReason == reason || (!enabled && reason == null && before.fallbackReason != null))) {
            val existing = checkNotNull(lastGood)
            return remember(ready(existing, synchronize(existing), digest(encode(existing))))
        }
        val previous = lastGood
        val oldRevision = maxOf(previous?.state?.revision ?: 0, runCatching { migratedRevision() }.getOrNull() ?: 0)
        if (oldRevision == Int.MAX_VALUE || (enabled && oldRevision == Int.MAX_VALUE - 1)) {
            failure = "revision_exhausted"; unavailable("revision_exhausted", before.token)
            throw TrialSettingsStorageException()
        }
        val change = TrialSettingChange(oldRevision + 1, enabled, reason,
            if (reason == null) "operator" else "health", boundedTime(now()))
        val next = Record(change, previous?.legacyPresent ?: before.legacyKeyPresent,
            ((previous?.changes ?: emptyList()) + change).takeLast(HISTORY_LIMIT))
        if (!enabled) runCatching { writeBarrier(change) }
        if (!write(next)) {
            if (!enabled) lastGood = next
            failure = "write_failed"; unavailable("write_failed", before.token)
            throw TrialSettingsStorageException()
        }
        lastGood = next
        failure = null
        knownMigrated = true
        return remember(ready(next, synchronize(next), digest(encode(next))))
    }

    private fun synchronize(record: Record): String? {
        val mirrored = runCatching { mirror(record.state) }.getOrDefault(false)
        val cleared = mirrored && runCatching { writeBarrier(null) }.getOrDefault(false)
        return if (mirrored && cleared) null else "mirror_failed"
    }

    private fun ready(record: Record, warning: String?, token: String) = TrialSettingsSnapshot(
        record.state.enabled, record.state.fallbackReason, record.state.revision, record.state.source,
        record.state.changedAt, "ready", warning, record.legacyPresent, record.changes, token)

    private fun unavailable(error: String, token: String): TrialSettingsSnapshot {
        val previous = lastGood
        return remember(TrialSettingsSnapshot(false, previous?.state?.fallbackReason, previous?.state?.revision ?: 0,
            previous?.state?.source ?: "unknown", previous?.state?.changedAt, "unavailable", error,
            previous?.legacyPresent ?: false, previous?.changes ?: emptyList(), "$token/$error"))
    }

    private fun remember(value: TrialSettingsSnapshot): TrialSettingsSnapshot = value.also { cached = it }

    private fun write(record: Record): Boolean = runCatching { storage.write(encode(record)); true }.getOrDefault(false)

    private fun encode(record: Record): ByteArray {
        val payload = JSONObject().put("schema_version", 1).put("state", record.state.json())
            .put("legacy_key_present", record.legacyPresent)
            .put("recent_changes", JSONArray(record.changes.map { it.json() })).toString()
        return JSONObject().put("payload", payload).put("sha256", digest(payload.toByteArray(Charsets.UTF_8)))
            .toString().toByteArray(Charsets.UTF_8).also { require(it.size <= MAX_BYTES) }
    }

    private fun decode(bytes: ByteArray): Record {
        require(bytes.size in 1..MAX_BYTES)
        val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
        val envelope = strictObject(text)
        require(envelope.keys().asSequence().toSet() == setOf("payload", "sha256"))
        val payload = envelope.get("payload") as? String ?: error("invalid payload")
        val checksum = envelope.get("sha256") as? String ?: error("invalid checksum")
        require(checksum.matches(Regex("[0-9a-f]{64}")) && checksum == digest(payload.toByteArray(Charsets.UTF_8)))
        val body = strictObject(payload)
        require(body.keys().asSequence().toSet() == setOf("schema_version", "state", "legacy_key_present", "recent_changes"))
        require(body.get("schema_version") is Int && body.get("legacy_key_present") is Boolean)
        require(body.getInt("schema_version") == 1)
        val state = decodeChange(body.getJSONObject("state"))
        val rows = body.getJSONArray("recent_changes")
        require(rows.length() in 1..HISTORY_LIMIT)
        val changes = (0 until rows.length()).map { decodeChange(rows.getJSONObject(it)) }
        require(changes.last() == state && changes.zipWithNext().all { (a, b) -> a.revision < b.revision })
        return Record(state, body.getBoolean("legacy_key_present"), changes)
    }

    companion object {
        const val HISTORY_LIMIT = 8
        const val MAX_BYTES = 16_384
        private val SOURCES = setOf("legacy_migration", "operator", "health")
        internal fun decodeChange(value: JSONObject): TrialSettingChange {
            require(value.keys().asSequence().toSet() == setOf("revision", "enabled", "fallback_reason", "source", "changed_at_utc"))
            val revision = value.getLong("revision").also { require(it in 1..Int.MAX_VALUE.toLong()) }.toInt()
            val source = (value.get("source") as? String ?: error("invalid source")).also { require(it in SOURCES) }
            require(value.get("revision") is Int || value.get("revision") is Long)
            require(value.get("enabled") is Boolean)
            val enabled = value.getBoolean("enabled")
            require(revision != Int.MAX_VALUE || !enabled)
            val reason = if (value.isNull("fallback_reason")) null else
                (value.get("fallback_reason") as? String ?: error("invalid fallback"))
            require(reason == null || reason in PollTrialPlan.fallbackReasons)
            require(!enabled || reason == null)
            require(source != "health" || (!enabled && reason != null))
            require(source != "operator" || reason == null)
            val at = if (value.isNull("changed_at_utc")) null else
                (value.get("changed_at_utc") as? String ?: error("invalid change time")).also {
                require(boundedTime(it) == it)
            }
            return TrialSettingChange(revision, enabled, reason, source, at)
        }
        private fun boundedTime(value: String?): String? = value?.takeIf {
            it.length <= 40 && runCatching { Instant.parse(it).epochSecond in 1_577_836_800L..4_102_444_800L }.getOrDefault(false)
        }
        private fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }

        internal fun decodeBarrier(text: String): TrialSettingChange = decodeChange(strictObject(text)).also {
            require(it.source in setOf("operator", "health") && !it.enabled)
        }

        private fun strictObject(text: String): JSONObject {
            val tokener = JSONTokener(text)
            val value = tokener.nextValue() as? JSONObject ?: error("object required")
            require(tokener.nextClean() == '\u0000' && !tokener.more())
            // These private records are emitted canonically by this encoder. This also
            // rejects duplicate keys, comments and coercive/non-JSON token spellings.
            require(value.toString() == text)
            return value
        }
    }
}

/** Incomplete .new files are never promoted; only a synced atomic replacement is authoritative. */
internal class AtomicTrialSettingsStorage(private val file: File) : TrialSettingsStorage {
    private val pending = File(file.path + ".new")

    @Synchronized override fun read(): ByteArray? {
        if (!file.exists()) return null
        return file.inputStream().use { input ->
            val bytes = ByteArray(TrialSettingsRepository.MAX_BYTES + 1)
            var size = 0
            while (size < bytes.size) {
                val count = input.read(bytes, size, bytes.size - size)
                if (count < 0) break
                check(count > 0)
                size += count
            }
            bytes.copyOf(size)
        }
    }

    @Synchronized override fun write(bytes: ByteArray) {
        require(bytes.size in 1..TrialSettingsRepository.MAX_BYTES)
        try {
            check(file.parentFile?.let { it.isDirectory || it.mkdirs() } != false)
            FileOutputStream(pending).use { output -> output.write(bytes); output.fd.sync() }
            Files.move(pending.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            check(read()?.contentEquals(bytes) == true)
        } catch (error: Exception) {
            pending.delete()
            throw error
        }
    }
}

/** Android wiring stays outside the state machine so disk and preference failures are testable. */
internal object TrialSettings {
    private const val MIGRATED = "poll_trial_state_revision_v1"
    private const val BARRIER = "poll_trial_pending_disable_v1"
    private val repositories = linkedMapOf<String, TrialSettingsRepository>()

    @Synchronized fun repository(context: Context): TrialSettingsRepository {
        val file = File(context.noBackupFilesDir, "poll-trial-state-v1.json")
        return repositories.getOrPut(file.absolutePath) {
            val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
            TrialSettingsRepository(AtomicTrialSettingsStorage(file), legacy = {
                LegacyTrialSetting(preferences.contains("poll_trial_enabled"),
                    preferences.getBoolean("poll_trial_enabled", false),
                    preferences.getString("poll_trial_fallback_reason", null))
            }, migratedRevision = {
                if (!preferences.contains(MIGRATED)) null else preferences.getInt(MIGRATED, 0).also { require(it > 0) }
            }, markMigrated = { revision ->
                if (preferences.contains(MIGRATED)) true
                else commit(preferences, preferences.edit().putInt(MIGRATED, revision))
            }, mirror = { state ->
                if (preferences.getInt(MIGRATED, 0) > state.revision) false
                else if (preferences.getInt(MIGRATED, 0) == state.revision &&
                    preferences.contains("poll_trial_enabled") && preferences.getBoolean("poll_trial_enabled", false) == state.enabled &&
                    preferences.getString("poll_trial_fallback_reason", null) == state.fallbackReason) true
                else commit(preferences, preferences.edit().putInt(MIGRATED, state.revision)
                    .putBoolean("poll_trial_enabled", state.enabled).putString("poll_trial_fallback_reason", state.fallbackReason))
            }, readBarrier = {
                preferences.getString(BARRIER, null)?.let { TrialSettingsRepository.decodeBarrier(it) }
            }, writeBarrier = { state ->
                if (state == null && !preferences.contains(BARRIER)) true
                else commit(preferences, preferences.edit().putString(BARRIER, state?.json()?.toString()))
            })
        }
    }

    private fun commit(preferences: SharedPreferences, editor: SharedPreferences.Editor): Boolean =
        runCatching { commitPreferenceUpdate(preferences, editor); true }.getOrDefault(false)

}

/** Rendering and checkbox changes only edit a draft; only an explicit Apply can persist it. */
internal class TrialSettingsDraft(snapshot: TrialSettingsSnapshot) {
    var base = snapshot; private set
    var selected = snapshot.enabled; private set
    var dirty = false; private set

    fun refresh(snapshot: TrialSettingsSnapshot) {
        if (!dirty) reload(snapshot)
    }
    fun select(enabled: Boolean) { selected = enabled; dirty = enabled != base.enabled }
    fun reload(snapshot: TrialSettingsSnapshot) { base = snapshot; selected = snapshot.enabled; dirty = false }
    fun apply(repository: TrialSettingsRepository): TrialSettingsSnapshot =
        repository.change(selected, null, base).also(::reload)
}
