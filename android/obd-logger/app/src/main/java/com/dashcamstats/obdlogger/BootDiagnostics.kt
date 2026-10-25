package com.dashcamstats.obdlogger

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

internal const val BOOT_DIAGNOSTIC_HISTORY_LIMIT = 8
internal const val BOOT_DIAGNOSTIC_MAX_BYTES = 32 * 1_024
private val bootDiagnosticRecordLock = Any()
private val BOOT_DIAGNOSTIC_EARLIEST = Instant.parse("2020-01-01T00:00:00Z")
private val BOOT_DIAGNOSTIC_LATEST = Instant.parse("2100-01-01T00:00:00Z")
private val BOOT_DIAGNOSTIC_TRANSITIONS = setOf(
    "first_observation", "same_process", "process_restart", "package_update", "kernel_boot", "unknown",
)
private val BOOT_DIAGNOSTIC_REASONS = setOf(
    "unknown", "reboot", "cold", "power_key", "usb", "watchdog", "kernel_panic", "thermal",
    "hard_reset", "adb", "user_requested", "software_update", "recovery", "bootloader",
)
private val BOOT_DIAGNOSTIC_REASON_SOURCES = setOf("sys_boot_reason", "ro_boot_bootreason", "unavailable")
private val BOOT_DIAGNOSTIC_EXIT_REASONS = setOf(
    "unavailable", "not_recorded", "unknown", "exit_self", "signaled", "low_memory", "crash",
    "native_crash", "anr", "initialization_failure", "permission_change", "excessive_resource_usage",
    "user_requested", "user_stopped", "dependency_died", "other", "freezer", "package_state_change",
    "package_updated",
)

/** A category reported by the firmware is evidence, not an independently established cause. */
internal data class BootReportedReason(val reason: String, val source: String) {
    val confidence: String get() = if (reason in setOf("unknown", "reboot")) "unknown" else "reported"
}

internal fun boundedBootReason(systemReason: String?, bootloaderReason: String?): BootReportedReason {
    fun category(value: String?): String? = when (value?.takeIf { it.length <= 96 }?.trim()?.lowercase(java.util.Locale.ROOT)) {
        "reboot" -> "reboot"
        "cold", "cold_boot" -> "cold"
        "power_key", "powerkey" -> "power_key"
        "usb" -> "usb"
        "watchdog", "reboot,watchdog" -> "watchdog"
        "kernel_panic", "kernel-panic", "reboot,kernel_panic" -> "kernel_panic"
        "thermal", "shutdown,thermal", "reboot,thermal" -> "thermal"
        "hard_reset", "hard" -> "hard_reset"
        "reboot,adb", "adb" -> "adb"
        "reboot,userrequested", "reboot,user_requested", "userrequested" -> "user_requested"
        "reboot,ota", "reboot,software_update" -> "software_update"
        "recovery", "reboot,recovery" -> "recovery"
        "bootloader", "reboot,bootloader" -> "bootloader"
        else -> null
    }
    category(systemReason)?.let { return BootReportedReason(it, "sys_boot_reason") }
    category(bootloaderReason)?.let { return BootReportedReason(it, "ro_boot_bootreason") }
    return BootReportedReason("unknown", "unavailable")
}

/** This identity is app-private and never projected into public status. */
internal data class BootDiagnosticObservation(
    val bootId: String?,
    val bootCount: Int?,
    val processToken: String,
    val elapsedMillis: Long,
    val observedAtUtc: String,
    val appVersionCode: Int,
    val buildGitSha: String,
    val reportedReason: BootReportedReason,
) {
    val valid: Boolean get() = (bootId == null || validDiagnosticUuid(bootId)) &&
        (bootCount == null || bootCount >= 0) && validDiagnosticUuid(processToken) && elapsedMillis >= 0 &&
        validDiagnosticTime(observedAtUtc) && appVersionCode in 1..1_000_000 &&
        (buildGitSha == "unknown" || buildGitSha.matches(Regex("[0-9a-f]{12}"))) &&
        reportedReason.reason in BOOT_DIAGNOSTIC_REASONS && reportedReason.source in BOOT_DIAGNOSTIC_REASON_SOURCES
}

internal data class BootProcessExit(val reason: String, val atUtc: String)
internal data class BootProcessExits(val available: Boolean, val entries: List<BootProcessExit> = emptyList())

/** No PID, raw property, exception text, stack trace, process description or extra boot UUID is exposed. */
data class BootDiagnosticEntry(
    val transition: String,
    val observedAtUtc: String,
    val bootCount: Int?,
    val bootCountDelta: Int?,
    val bootReason: String,
    val bootReasonSource: String,
    val previousProcessExit: String,
    val previousProcessExitAtUtc: String?,
    val appVersionCode: Int,
    val buildGitSha: String,
) {
    internal val valid: Boolean get() = transition in BOOT_DIAGNOSTIC_TRANSITIONS &&
        validDiagnosticTime(observedAtUtc) && (bootCount == null || bootCount >= 0) &&
        (bootCountDelta == null || bootCountDelta in 1..1_000) && bootReason in BOOT_DIAGNOSTIC_REASONS &&
        bootReasonSource in BOOT_DIAGNOSTIC_REASON_SOURCES && previousProcessExit in BOOT_DIAGNOSTIC_EXIT_REASONS &&
        (previousProcessExitAtUtc == null || validDiagnosticTime(previousProcessExitAtUtc)) &&
        appVersionCode in 1..1_000_000 && (buildGitSha == "unknown" || buildGitSha.matches(Regex("[0-9a-f]{12}")))

    fun toJson(): JSONObject = JSONObject()
        .put("transition", transition).put("observed_at_utc", observedAtUtc)
        .put("boot_count", bootCount ?: JSONObject.NULL).put("boot_count_delta", bootCountDelta ?: JSONObject.NULL)
        .put("boot_reason", bootReason).put("boot_reason_source", bootReasonSource)
        .put("cause_confidence", if (bootReason in setOf("unknown", "reboot")) "unknown" else "reported")
        .put("previous_process_exit", previousProcessExit)
        .put("previous_process_exit_at_utc", previousProcessExitAtUtc ?: JSONObject.NULL)
        .put("app_version_code", appVersionCode).put("build_git_sha", buildGitSha)
}

data class BootDiagnosticsSnapshot(
    val storageState: String = "pending",
    val current: BootDiagnosticEntry? = null,
    val history: List<BootDiagnosticEntry> = emptyList(),
) {
    fun toJson(): JSONObject = (current?.toJson() ?: JSONObject()
        .put("transition", "unknown").put("observed_at_utc", JSONObject.NULL)
        .put("boot_count", JSONObject.NULL).put("boot_count_delta", JSONObject.NULL)
        .put("boot_reason", "unknown").put("boot_reason_source", "unavailable")
        .put("cause_confidence", "unknown").put("previous_process_exit", "unavailable")
        .put("previous_process_exit_at_utc", JSONObject.NULL).put("app_version_code", BuildConfig.VERSION_CODE)
        .put("build_git_sha", BuildConfig.BUILD_GIT_SHA))
        .put("schema_version", 1).put("storage_state", storageState)
        .put("history", JSONArray(history.takeLast(BOOT_DIAGNOSTIC_HISTORY_LIMIT).map { it.toJson() }))
}

internal data class BootDiagnosticsRecord(val observation: BootDiagnosticObservation, val history: List<BootDiagnosticEntry>)
internal data class BootDiagnosticsRead(val state: String, val record: BootDiagnosticsRecord? = null)

internal interface BootDiagnosticsStore {
    fun read(): BootDiagnosticsRead
    fun write(record: BootDiagnosticsRecord)
}

/** Separate diagnostic state: it cannot grant an ignition edge, wake hold or sleep deadline. */
internal class AtomicBootDiagnosticsStore(private val file: File) : BootDiagnosticsStore {
    @Synchronized
    override fun read(): BootDiagnosticsRead {
        if (!file.exists()) return BootDiagnosticsRead("missing")
        val text = try {
            file.inputStream().use { input ->
                val bytes = ByteArray(BOOT_DIAGNOSTIC_MAX_BYTES + 1)
                var size = 0
                while (size < bytes.size) {
                    val count = input.read(bytes, size, bytes.size - size)
                    if (count < 0) break
                    require(count > 0)
                    size += count
                }
                if (size > BOOT_DIAGNOSTIC_MAX_BYTES) return BootDiagnosticsRead("invalid")
                String(bytes, 0, size, Charsets.UTF_8)
            }
        } catch (_: Exception) { return BootDiagnosticsRead("read_failed") }
        return runCatching {
            val envelope = diagnosticObject(text)
            require(envelope.keys().asSequence().toSet() == setOf("payload", "sha256"))
            val payload = envelope.get("payload") as? String ?: error("invalid payload")
            require(envelope.get("sha256") == diagnosticHash(payload))
            val body = diagnosticObject(payload)
            require(body.keys().asSequence().toSet() == setOf("schema_version", "observation", "history"))
            require(body.diagnosticInteger("schema_version") == 1L)
            val observation = decodeObservation(body.getJSONObject("observation"))
            val rows = body.getJSONArray("history")
            require(rows.length() in 1..BOOT_DIAGNOSTIC_HISTORY_LIMIT)
            val history = (0 until rows.length()).map { decodeEntry(rows.getJSONObject(it)) }
            require(history.last().observedAtUtc == observation.observedAtUtc)
            BootDiagnosticsRead("ready", BootDiagnosticsRecord(observation, history))
        }.getOrElse { BootDiagnosticsRead("invalid") }
    }

    @Synchronized
    override fun write(record: BootDiagnosticsRecord) {
        require(record.observation.valid && record.history.size in 1..BOOT_DIAGNOSTIC_HISTORY_LIMIT &&
            record.history.all { it.valid } && record.history.last().observedAtUtc == record.observation.observedAtUtc)
        val payload = JSONObject().put("schema_version", 1).put("observation", encodeObservation(record.observation))
            .put("history", JSONArray(record.history.map { it.toJson() })).toString()
        val bytes = JSONObject().put("payload", payload).put("sha256", diagnosticHash(payload))
            .toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= BOOT_DIAGNOSTIC_MAX_BYTES)
        val pending = File(file.path + ".new")
        try {
            check(file.parentFile?.let { it.isDirectory || it.mkdirs() } != false)
            FileOutputStream(pending).use { output -> output.write(bytes); output.fd.sync() }
            Files.move(pending.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (error: Exception) {
            pending.delete()
            throw error
        }
    }

    private fun encodeObservation(value: BootDiagnosticObservation) = JSONObject()
        .put("boot_id", value.bootId ?: JSONObject.NULL).put("boot_count", value.bootCount ?: JSONObject.NULL)
        .put("process_token", value.processToken).put("elapsed_ms", value.elapsedMillis)
        .put("observed_at_utc", value.observedAtUtc).put("app_version_code", value.appVersionCode)
        .put("build_git_sha", value.buildGitSha).put("boot_reason", value.reportedReason.reason)
        .put("boot_reason_source", value.reportedReason.source)

    private fun decodeObservation(value: JSONObject): BootDiagnosticObservation {
        require(value.keys().asSequence().toSet() == setOf("boot_id", "boot_count", "process_token", "elapsed_ms",
            "observed_at_utc", "app_version_code", "build_git_sha", "boot_reason", "boot_reason_source"))
        return BootDiagnosticObservation(value.diagnosticTextOrNull("boot_id"), value.diagnosticCountOrNull("boot_count"),
            value.diagnosticText("process_token"), value.diagnosticInteger("elapsed_ms"), value.diagnosticText("observed_at_utc"),
            value.diagnosticCount("app_version_code"), value.diagnosticText("build_git_sha"),
            BootReportedReason(value.diagnosticText("boot_reason"), value.diagnosticText("boot_reason_source"))).also { require(it.valid) }
    }

    private fun decodeEntry(value: JSONObject): BootDiagnosticEntry {
        require(value.keys().asSequence().toSet() == setOf("transition", "observed_at_utc", "boot_count", "boot_count_delta",
            "boot_reason", "boot_reason_source", "cause_confidence", "previous_process_exit", "previous_process_exit_at_utc",
            "app_version_code", "build_git_sha"))
        val result = BootDiagnosticEntry(value.diagnosticText("transition"), value.diagnosticText("observed_at_utc"),
            value.diagnosticCountOrNull("boot_count"), value.diagnosticCountOrNull("boot_count_delta"),
            value.diagnosticText("boot_reason"), value.diagnosticText("boot_reason_source"), value.diagnosticText("previous_process_exit"),
            value.diagnosticTextOrNull("previous_process_exit_at_utc"), value.diagnosticCount("app_version_code"),
            value.diagnosticText("build_git_sha"))
        require(result.valid && value.diagnosticText("cause_confidence") ==
            if (result.bootReason in setOf("unknown", "reboot")) "unknown" else "reported")
        return result
    }
}

internal class BootDiagnosticsRecorder(private val store: BootDiagnosticsStore) {
    fun observe(value: BootDiagnosticObservation, exits: BootProcessExits): BootDiagnosticsSnapshot =
        synchronized(bootDiagnosticRecordLock) { observeLocked(value, exits) }

    private fun observeLocked(value: BootDiagnosticObservation, exits: BootProcessExits): BootDiagnosticsSnapshot {
        if (!value.valid) return BootDiagnosticsSnapshot("probe_unavailable")
        val previous = try { store.read() } catch (_: Exception) { BootDiagnosticsRead("read_failed") }
        if (previous.state !in setOf("ready", "missing")) return BootDiagnosticsSnapshot(previous.state)
        val old = previous.record?.observation
        val transition = transition(old, value)
        val countDelta = if (transition == "kernel_boot" && old?.bootCount != null && value.bootCount != null) {
            (value.bootCount.toLong() - old.bootCount).takeIf { it in 1..1_000 }?.toInt()
        } else null
        val exit = if (old != null && exits.available) exits.entries.asSequence()
            .filter { it.reason in BOOT_DIAGNOSTIC_EXIT_REASONS && validDiagnosticTime(it.atUtc) }
            .filter { Instant.parse(it.atUtc) >= Instant.parse(old.observedAtUtc) &&
                Instant.parse(it.atUtc) <= Instant.parse(value.observedAtUtc) }
            .maxByOrNull { Instant.parse(it.atUtc) } else null
        val entry = BootDiagnosticEntry(transition, value.observedAtUtc, value.bootCount, countDelta,
            value.reportedReason.reason, value.reportedReason.source,
            exit?.reason ?: if (exits.available && old != null) "not_recorded" else "unavailable", exit?.atUtc,
            value.appVersionCode, value.buildGitSha)
        val previousHistory = previous.record?.history.orEmpty()
        if (transition == "same_process") return BootDiagnosticsSnapshot("ready", entry, previousHistory)
        val history = (previousHistory + entry).takeLast(BOOT_DIAGNOSTIC_HISTORY_LIMIT)
        return try {
            store.write(BootDiagnosticsRecord(value, history))
            BootDiagnosticsSnapshot("ready", entry, history)
        } catch (_: Exception) { BootDiagnosticsSnapshot("write_failed", entry, previousHistory) }
    }

    private fun transition(old: BootDiagnosticObservation?, value: BootDiagnosticObservation): String {
        if (value.bootId == null && value.bootCount == null) return "unknown"
        if (old == null) return "first_observation"
        if (old.bootId != null && value.bootId != null && old.bootId == value.bootId &&
            old.bootCount != null && value.bootCount != null && old.bootCount != value.bootCount) return "unknown"
        if ((old.bootId == null || value.bootId == null) && old.bootCount != null && value.bootCount != null &&
            value.bootCount < old.bootCount) return "unknown"
        val sameBoot = when {
            old.bootId != null && value.bootId != null -> old.bootId == value.bootId
            old.bootCount != null && value.bootCount != null -> old.bootCount == value.bootCount
            else -> return "unknown"
        }
        if (!sameBoot) return "kernel_boot"
        if (value.elapsedMillis < old.elapsedMillis) return "unknown"
        if (old.appVersionCode != value.appVersionCode || old.buildGitSha != value.buildGitSha) return "package_update"
        return if (old.processToken == value.processToken) "same_process" else "process_restart"
    }
}

private object BootDiagnosticProcess { val token: String = UUID.randomUUID().toString() }

/** Called off the service startup path. snapshot() only reads an immutable volatile value. */
internal class BootDiagnostics(
    private val context: Context,
    private val store: BootDiagnosticsStore = AtomicBootDiagnosticsStore(File(context.noBackupFilesDir, "boot-diagnostics-v1.json")),
) {
    @Volatile private var latest = BootDiagnosticsSnapshot()
    fun snapshot(): BootDiagnosticsSnapshot = latest

    fun observeServiceStart() {
        latest = runCatching {
            val boot = readSleepBootIdentity(context)
            val observation = BootDiagnosticObservation(boot.bootId, boot.bootCount, BootDiagnosticProcess.token,
                SystemClock.elapsedRealtime(), Instant.now().toString(), BuildConfig.VERSION_CODE, BuildConfig.BUILD_GIT_SHA,
                boundedBootReason(readDiagnosticProperty("sys.boot.reason"), readDiagnosticProperty("ro.boot.bootreason")))
            BootDiagnosticsRecorder(store).observe(observation, readOwnProcessExits(context))
        }.getOrElse { BootDiagnosticsSnapshot("probe_unavailable") }
    }
}

// ApplicationExitInfo.REASON_* are stable platform integers. No Android 30 class is
// resolved by this pure mapper on older firmware; the platform query is separately gated.
internal fun boundedExitReason(reason: Int): String = when (reason) {
    1 -> "exit_self"
    2 -> "signaled"
    3 -> "low_memory"
    4 -> "crash"
    5 -> "native_crash"
    6 -> "anr"
    7 -> "initialization_failure"
    8 -> "permission_change"
    9 -> "excessive_resource_usage"
    10 -> "user_requested"
    11 -> "user_stopped"
    12 -> "dependency_died"
    13 -> "other"
    14 -> "freezer"
    15 -> "package_state_change"
    16 -> "package_updated"
    else -> "unknown"
}

internal fun boundedProcessExits(sdk: Int, reader: () -> List<BootProcessExit>): BootProcessExits =
    if (sdk < Build.VERSION_CODES.R) BootProcessExits(false) else runCatching {
        BootProcessExits(true, reader().take(4).filter {
            it.reason in BOOT_DIAGNOSTIC_EXIT_REASONS && validDiagnosticTime(it.atUtc)
        })
    }.getOrElse { BootProcessExits(false) }

private fun readOwnProcessExits(context: Context): BootProcessExits = boundedProcessExits(Build.VERSION.SDK_INT) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) emptyList() else {
        val manager = checkNotNull(context.getSystemService(ActivityManager::class.java))
        manager.getHistoricalProcessExitReasons(context.packageName, 0, 4)
            .filter { it.processName == context.packageName }
            .map { BootProcessExit(boundedExitReason(it.reason), Instant.ofEpochMilli(it.timestamp).toString()) }
    }
}

private fun readDiagnosticProperty(name: String): String? = runCatching {
    val property = Class.forName("android.os.SystemProperties").getMethod("get", String::class.java)
        .invoke(null, name) as? String
    property?.takeIf { it.length <= 96 }
}.getOrNull()

private fun validDiagnosticUuid(value: String) = value.matches(Regex("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"))
private fun validDiagnosticTime(value: String): Boolean = value.length <= 40 && runCatching {
    val time = Instant.parse(value)
    time in BOOT_DIAGNOSTIC_EARLIEST..BOOT_DIAGNOSTIC_LATEST
}.getOrDefault(false)
private fun diagnosticHash(value: String) = MessageDigest.getInstance("SHA-256")
    .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
private fun diagnosticObject(text: String): JSONObject {
    val parser = JSONTokener(text)
    val value = parser.nextValue() as? JSONObject ?: error("invalid object")
    require(parser.nextClean() == '\u0000')
    return value
}
private fun JSONObject.diagnosticInteger(key: String): Long = when (val value = get(key)) {
    is Int -> value.toLong()
    is Long -> value
    else -> error("invalid integer")
}
private fun JSONObject.diagnosticCount(key: String): Int = diagnosticInteger(key).also { require(it in 0..Int.MAX_VALUE) }.toInt()
private fun JSONObject.diagnosticCountOrNull(key: String): Int? = if (get(key) === JSONObject.NULL) null else diagnosticCount(key)
private fun JSONObject.diagnosticTextOrNull(key: String): String? = if (get(key) === JSONObject.NULL) null
    else get(key) as? String ?: error("invalid text")

private fun JSONObject.diagnosticText(key: String): String = get(key) as? String ?: error("invalid text")
