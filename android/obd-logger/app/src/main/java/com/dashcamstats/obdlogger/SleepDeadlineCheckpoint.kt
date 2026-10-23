package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.json.JSONTokener
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest

private const val CHECKPOINT_SCHEMA_VERSION = 1
private const val CHECKPOINT_MAX_BYTES = 2_048
private val CHECKPOINT_KEYS = setOf(
    "schema_version", "boot_id", "boot_count", "started_elapsed_ms", "finished_elapsed_ms",
    "ignition_on", "observed_window_s", "off_lower_elapsed_ms", "off_upper_elapsed_ms", "window_s",
)

/** Private restart continuity only: this is never an instruction to reset the native countdown. */
internal data class SleepDeadlineCheckpoint(
    val boot: SleepBootIdentity,
    val startedElapsedMillis: Long,
    val finishedElapsedMillis: Long,
    val ignitionOn: Boolean,
    val observedWindowSeconds: Int,
    val offLowerElapsedMillis: Long? = null,
    val offUpperElapsedMillis: Long? = null,
    val windowSeconds: Int? = null,
) {
    val valid: Boolean get() {
        if (boot.bootId == null || boot.bootCount?.let { it < 0 } == true ||
            !boot.bootId.matches(Regex("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")) ||
            startedElapsedMillis < 0 || finishedElapsedMillis < startedElapsedMillis ||
            finishedElapsedMillis - startedElapsedMillis > DEADLINE_CAPTURE_MAX_MS ||
            observedWindowSeconds !in 1..3_600
        ) return false
        val lower = offLowerElapsedMillis ?: return false
        val upper = offUpperElapsedMillis ?: return false
        val window = windowSeconds ?: return false
        return !ignitionOn && lower >= 0 && upper >= lower && upper <= finishedElapsedMillis &&
            upper - lower <= DEADLINE_OBSERVATION_MAX_AGE_MS + DEADLINE_CAPTURE_MAX_MS &&
            window in 1..3_600 && lower <= Long.MAX_VALUE - window * 1_000L &&
            finishedElapsedMillis < lower + window * 1_000L
    }

    val evidence: SleepDeadlineEvidence get() = SleepDeadlineEvidence(
        boot.bootId, boot.bootCount, finishedElapsedMillis, ignitionOn,
        offLowerElapsedMillis, offUpperElapsedMillis, windowSeconds,
    )
}

internal interface SleepDeadlineCheckpointStore {
    fun read(): SleepDeadlineCheckpoint?
    fun write(checkpoint: SleepDeadlineCheckpoint?)
}

/**
 * Atomic, fsynced app-private state survives an in-place update, but is excluded from backup.
 * The hash detects damaged bytes; privacy and Android's app UID provide the trust boundary.
 * Invalidations are committed tombstones rather than a deletion that could expose an old backup.
 */
internal class AtomicSleepDeadlineCheckpointStore(file: File) : SleepDeadlineCheckpointStore {
    private val file = file
    private val pendingFile = File(file.path + ".new")

    @Synchronized
    override fun read(): SleepDeadlineCheckpoint? {
        val record = runCatching {
            file.inputStream().use { input ->
                val bytes = ByteArray(CHECKPOINT_MAX_BYTES + 1)
                var size = 0
                while (size < bytes.size) {
                    val count = input.read(bytes, size, bytes.size - size)
                    if (count < 0) break
                    require(count > 0)
                    size += count
                }
                require(size <= CHECKPOINT_MAX_BYTES)
                strictObject(String(bytes, 0, size, Charsets.UTF_8))
            }
        }.getOrNull() ?: return null
        return runCatching {
            require(record.keys().asSequence().toSet() == setOf("payload", "sha256"))
            val payloadText = record.get("payload") as? String ?: error("invalid payload")
            require(record.get("sha256") == sha256(payloadText))
            val payload = strictObject(payloadText)
            require(payload.keys().asSequence().toSet() == CHECKPOINT_KEYS)
            require(payload.integer("schema_version") == CHECKPOINT_SCHEMA_VERSION.toLong())
            val bootId = payload.nullableString("boot_id")
            val count = payload.nullableInteger("boot_count")
            require(count == null || count in 0..Int.MAX_VALUE)
            val on = payload.get("ignition_on") as? Boolean ?: error("invalid ACC")
            val observedWindow = payload.integer("observed_window_s")
            val window = payload.nullableInteger("window_s")
            require(observedWindow in 1..3_600 && (window == null || window in 1..3_600))
            SleepDeadlineCheckpoint(
                SleepBootIdentity(bootId, count?.toInt()), payload.integer("started_elapsed_ms"),
                payload.integer("finished_elapsed_ms"), on, observedWindow.toInt(),
                payload.nullableInteger("off_lower_elapsed_ms"), payload.nullableInteger("off_upper_elapsed_ms"),
                window?.toInt(),
            ).takeIf { it.valid }
        }.getOrNull()
    }

    @Synchronized
    override fun write(checkpoint: SleepDeadlineCheckpoint?) {
        require(checkpoint == null || checkpoint.valid)
        if (checkpoint == null) {
            // Revocation must not recover an old edge if the process dies while committing its
            // tombstone. Keeping the older committed file is appropriate only for a valid heartbeat.
            removeRecoverableFiles()
        }
        val payload = checkpoint?.let {
            JSONObject().put("schema_version", CHECKPOINT_SCHEMA_VERSION)
                .put("boot_id", it.boot.bootId ?: JSONObject.NULL)
                .put("boot_count", it.boot.bootCount ?: JSONObject.NULL)
                .put("started_elapsed_ms", it.startedElapsedMillis)
                .put("finished_elapsed_ms", it.finishedElapsedMillis)
                .put("ignition_on", it.ignitionOn)
                .put("observed_window_s", it.observedWindowSeconds)
                .put("off_lower_elapsed_ms", it.offLowerElapsedMillis ?: JSONObject.NULL)
                .put("off_upper_elapsed_ms", it.offUpperElapsedMillis ?: JSONObject.NULL)
                .put("window_s", it.windowSeconds ?: JSONObject.NULL)
        }?.toString() ?: "null"
        val record = JSONObject().put("payload", payload).put("sha256", sha256(payload))
            .toString().toByteArray(Charsets.UTF_8)
        try {
            check(file.parentFile?.let { it.isDirectory || it.mkdirs() } != false)
            FileOutputStream(pendingFile).use { output ->
                output.write(record)
                output.fd.sync()
            }
            Files.move(pendingFile.toPath(), file.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (error: Exception) {
            removeRecoverableFiles()
            throw error
        }
    }

    private fun removeRecoverableFiles() {
        for (candidate in listOf(file, File(file.path + ".bak"), File(file.path + ".new"))) {
            check(!candidate.exists() || candidate.delete()) { "checkpoint revocation unavailable" }
        }
    }

    private fun JSONObject.integer(key: String): Long = when (val value = get(key)) {
        is Int -> value.toLong()
        is Long -> value
        else -> error("invalid integer")
    }

    private fun JSONObject.nullableInteger(key: String): Long? =
        if (get(key) === JSONObject.NULL) null else integer(key)

    private fun JSONObject.nullableString(key: String): String? =
        if (get(key) === JSONObject.NULL) null else get(key) as? String ?: error("invalid text")

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    private fun strictObject(text: String): JSONObject {
        val parser = JSONTokener(text)
        val value = parser.nextValue() as? JSONObject ?: error("invalid object")
        require(parser.nextClean() == '\u0000')
        return value
    }
}
