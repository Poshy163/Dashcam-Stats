package com.dashcamstats.obdlogger

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Duration
import java.time.Instant
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

internal const val APP_EVENT_SCHEMA_VERSION = 1
internal const val APP_EVENT_RING_LIMIT = 2_048
internal const val APP_EVENT_SNAPSHOT_LIMIT = 512
internal const val APP_EVENT_SNAPSHOT_MAX_BYTES = 512 * 1_024
internal const val APP_EVENT_RETENTION_DAYS = 7L

internal val APP_EVENT_KINDS = setOf(
    "app.boot",
    "app.service",
    "network.wifi",
    "power.sleep_window",
    "obd.ble_connection",
    "obd.elm_session",
    "obd.ecu_session",
    "obd.poll_health",
    "drive.lifecycle",
    "ingest.handoff",
    "radio.observation",
    "bundle.export",
    "receipt.verification",
)

internal val APP_EVENT_LEVELS = setOf("info", "warning", "error")

internal val APP_EVENT_OUTCOMES = setOf(
    "started",
    "succeeded",
    "failed",
    "retrying",
    "connected",
    "disconnected",
    "available",
    "lost",
    "requested",
    "acknowledged",
    "resumed",
    "verified",
    "changed",
    "skipped",
    "completed",
    "interrupted",
    "recovered",
    "observed",
    "pruned",
)

internal val APP_EVENT_REASON_CODES = setOf(
    "boot_completed",
    "package_replaced",
    "service_started",
    "service_stopped",
    "start_command",
    "uncaught_restart",
    "wifi_available",
    "wifi_lost",
    "default_network_changed",
    "backup_active",
    "wifi_connected",
    "wifi_disconnected",
    "server_owned",
    "ingestion_state_unknown",
    "property_refused",
    "readback_unavailable",
    "readback_mismatch",
    "scheduled_connect",
    "adapter_discovered",
    "adapter_not_found",
    "gatt_connected",
    "gatt_disconnected",
    "gatt_error",
    "gatt_timeout",
    "services_ready",
    "notifications_ready",
    "elm_ready",
    "elm_timeout",
    "protocol_search_failed",
    "adapter_voltage_valid",
    "ecu_proof_valid",
    "ecu_offline",
    "bus_silent",
    "engine_running",
    "engine_stopped",
    "voltage_below_start",
    "voltage_below_stop",
    "connection_lost",
    "connection_failed",
    "backoff_scheduled",
    "retry_woken",
    "ble_callback",
    "sleep_wake",
    "screen_on",
    "user_present",
    "power_connected",
    "acc_on",
    "acc_off",
    "engine_detected",
    "device_restart",
    "ingestion_requested",
    "request_observed",
    "quiesce_entered",
    "quiesce_acknowledged",
    "resume_observed",
    "resume_completed",
    "request_expired",
    "bluetooth_on",
    "bluetooth_off",
    "hotspot_on",
    "hotspot_off",
    "state_unknown",
    "export_started",
    "export_completed",
    "export_failed",
    "receipt_verified",
    "receipt_invalid",
    "retention_pruned",
    "first_sample_persisted",
    "drive_summary",
    "cadence_gap",
    "poll_timeout",
    "manual_request",
    "configuration_disabled",
    "storage_unavailable",
    "permission_denied",
    "unknown",
)

internal val APP_EVENT_METRIC_LIMITS = mapOf(
    "elapsed_ms" to (0.0..604_800_000.0),
    "attempt" to (0.0..10_000.0),
    "retry_delay_ms" to (0.0..86_400_000.0),
    "scan_ms" to (0.0..3_600_000.0),
    "connect_ms" to (0.0..3_600_000.0),
    "discovery_ms" to (0.0..3_600_000.0),
    "subscribe_ms" to (0.0..3_600_000.0),
    "elm_init_ms" to (0.0..3_600_000.0),
    "ecu_probe_ms" to (0.0..3_600_000.0),
    "silent_cycles" to (0.0..1_000.0),
    "first_sample_ms" to (0.0..3_600_000.0),
    "poll_cycle_ms" to (0.0..3_600_000.0),
    "polling_duty_cycle_percent" to (0.0..100.0),
    "sleep_target_s" to (0.0..3_600.0),
    "sleep_observed_s" to (0.0..3_600.0),
    "wifi_frequency_mhz" to (0.0..10_000.0),
    "sample_count" to (0.0..1_000_000_000.0),
    "pending_bundle_count" to (0.0..1_000_000.0),
    "bundle_bytes" to (0.0..536_870_912.0),
    "receipt_count" to (0.0..1_000_000.0),
    "gap_count" to (0.0..1_000_000_000.0),
    "timeout_count" to (0.0..1_000_000_000.0),
    "command_count" to (0.0..1_000_000_000.0),
    "consecutive_failures" to (0.0..1_000_000.0),
    "queue_depth" to (0.0..1_000_000.0),
)

internal val APP_EVENT_METRIC_KEYS = APP_EVENT_METRIC_LIMITS.keys

internal data class AppEventProducer(
    val appVersionName: String,
    val appVersionCode: Int,
    val buildGitSha: String,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("app_version_name", appVersionName.take(64))
        .put("app_version_code", appVersionCode.coerceAtLeast(1))
        .put(
            "build_git_sha",
            buildGitSha.takeIf { it.matches(Regex("^[0-9a-f]{12}$")) } ?: "unknown",
        )

    companion object {
        fun current(): AppEventProducer = AppEventProducer(
            appVersionName = BuildConfig.VERSION_NAME,
            appVersionCode = BuildConfig.VERSION_CODE,
            buildGitSha = BuildConfig.BUILD_GIT_SHA,
        )
    }
}

internal data class AppEventDraft(
    val occurredAtUtc: String,
    val sessionId: String,
    val kind: String,
    val level: String,
    val outcome: String,
    val reasonCode: String?,
    val driveId: String?,
    val metrics: Map<String, Number>,
)

/**
 * Private durable event ring plus an atomic, projection-only removable-storage snapshot.
 *
 * Callers can never supply free text. All dimensions are exact code allowlists, nullable fields
 * stay present, and metric values are finite numeric scalars under fixed keys.
 */
internal fun interface AppEventDraftSink {
    fun appendDraft(eventDraft: AppEventDraft): Boolean
}

internal class AppEventJournal(
    context: Context,
    private val now: () -> Instant = Instant::now,
    private val sourceUuid: () -> UUID = UUID::randomUUID,
    private val producer: AppEventProducer = AppEventProducer.current(),
    private val snapshotRoot: () -> File? = {
        DeviceFiles.removableRootOrNull(context) ?: DeviceFiles.fallbackStatusRoot(context)
    },
    preferences: SharedPreferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE),
) : AppEventDraftSink {
    private val preferences = preferences
    private val projectionPending = AtomicBoolean(false)

    fun append(
        sessionId: String,
        kind: String,
        level: String,
        outcome: String,
        reasonCode: String? = null,
        driveId: String? = null,
        metrics: Map<String, Number> = emptyMap(),
    ): Boolean = appendDraft(
        AppEventDraft(
            occurredAtUtc = now().toString(),
            sessionId = sessionId,
            kind = kind,
            level = level,
            outcome = outcome,
            reasonCode = reasonCode,
            driveId = driveId,
            metrics = metrics,
        ),
    )

    override fun appendDraft(eventDraft: AppEventDraft): Boolean = runCatching {
        val instant = now()
        val draft = validateDraft(eventDraft)
        synchronized(GLOBAL_LOCK) {
            val sourceId = persistentSourceId()
            val loaded = loadStoredEvents(instant)
            val previousSequence = maxOf(
                preferences.getLong(KEY_LAST_SEQUENCE, 0L).coerceAtLeast(0L),
                loaded.lastOrNull()?.optLong("sequence") ?: 0L,
            )
            check(previousSequence < Long.MAX_VALUE) { "event sequence exhausted" }
            val sequence = previousSequence + 1L
            val retained = loaded
                .toMutableList()
                .apply { add(eventJson(sequence, draft)) }
                .takeLast(APP_EVENT_RING_LIMIT)
            check(
                preferences.edit()
                    .putString(KEY_SOURCE_ID, sourceId)
                    .putLong(KEY_LAST_SEQUENCE, sequence)
                    .putString(KEY_EVENTS, JSONArray(retained).toString())
                    .commit(),
            ) { "event journal persistence failed" }
            val root = snapshotRoot()
            if (root == null || runCatching { publishAt(root, sourceId, retained, instant) }.isFailure) {
                projectionPending.set(true)
            } else {
                projectionPending.set(false)
            }
        }
        true
    }.getOrDefault(false)

    fun publishSnapshot(): Boolean = synchronized(GLOBAL_LOCK) {
        val published = runCatching {
            val instant = now()
            val sourceId = persistentSourceId()
            val retained = loadStoredEvents(instant)
            val root = snapshotRoot() ?: return@runCatching false
            publishAt(root, sourceId, retained, instant)
            true
        }.getOrDefault(false)
        projectionPending.set(!published)
        published
    }

    internal fun hasPendingProjection(): Boolean = projectionPending.get()

    internal fun buildSnapshot(
        sourceId: String,
        retained: List<JSONObject>,
        generatedAt: Instant,
    ): JSONObject {
        var projected = retained.takeLast(APP_EVENT_SNAPSHOT_LIMIT)
        while (true) {
            val first = projected.firstOrNull()?.getLong("sequence") ?: 0L
            val last = projected.lastOrNull()?.getLong("sequence") ?: 0L
            val body = JSONObject()
                .put("schema_version", APP_EVENT_SCHEMA_VERSION)
                .put("source_id", sourceId)
                .put("generated_at_utc", generatedAt.toString())
                .put("first_sequence", first)
                .put("last_sequence", last)
                .put("producer", producer.toJson())
                .put("events", JSONArray(projected))
            if (
                body.toString().toByteArray(Charsets.UTF_8).size <= APP_EVENT_SNAPSHOT_MAX_BYTES ||
                projected.isEmpty()
            ) {
                return body
            }
            projected = projected.drop(1)
        }
    }

    private fun persistentSourceId(): String {
        val stored = preferences.getString(KEY_SOURCE_ID, null)
        stored?.takeIf(::isCanonicalUuid)?.let { return it }
        return sourceUuid().toString().also { generated ->
            check(preferences.edit().putString(KEY_SOURCE_ID, generated).commit()) {
                "event source persistence failed"
            }
        }
    }

    private fun loadStoredEvents(reference: Instant): List<JSONObject> {
        val encoded = preferences.getString(KEY_EVENTS, null) ?: return emptyList()
        val cutoff = reference.minus(Duration.ofDays(APP_EVENT_RETENTION_DAYS))
        return runCatching {
            val array = JSONArray(encoded)
            buildList {
                for (index in 0 until array.length()) {
                    val event = array.optJSONObject(index) ?: continue
                    if (!isStoredEventValid(event, cutoff, reference)) continue
                    add(event)
                }
            }.sortedBy { it.getLong("sequence") }
                .distinctBy { it.getLong("sequence") }
                .takeLast(APP_EVENT_RING_LIMIT)
        }.getOrDefault(emptyList())
    }

    private fun publishAt(
        root: File,
        sourceId: String,
        retained: List<JSONObject>,
        generatedAt: Instant,
    ) {
        root.mkdirs()
        val partial = File(root, "events.json.partial")
        val target = File(root, "events.json")
        FileOutputStream(partial).use { output ->
            output.write(buildSnapshot(sourceId, retained, generatedAt).toString().toByteArray())
            output.fd.sync()
        }
        try {
            Files.move(
                partial.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (error: Exception) {
            partial.delete()
            throw IllegalStateException("could not atomically publish app events", error)
        }
    }

    private fun validateDraft(draft: AppEventDraft): AppEventDraft {
        check(isCanonicalUuid(draft.sessionId)) { "invalid event session" }
        check(draft.kind in APP_EVENT_KINDS) { "invalid event kind" }
        check(draft.level in APP_EVENT_LEVELS) { "invalid event level" }
        check(draft.outcome in APP_EVENT_OUTCOMES) { "invalid event outcome" }
        check(draft.reasonCode == null || draft.reasonCode in APP_EVENT_REASON_CODES) {
            "invalid event reason"
        }
        check(draft.driveId == null || isSafeDriveId(draft.driveId)) { "invalid event drive" }
        check(draft.metrics.all { (key, value) ->
            val numeric = value.toDouble()
            val limit = APP_EVENT_METRIC_LIMITS[key]
            numeric.isFinite() && limit != null && numeric in limit
        }) { "invalid event metric" }
        Instant.parse(draft.occurredAtUtc)
        return draft
    }

    private fun eventJson(sequence: Long, draft: AppEventDraft): JSONObject {
        val metricJson = JSONObject()
        for ((key, value) in draft.metrics) {
            when (value) {
                is Byte, is Short, is Int, is Long -> metricJson.put(key, value.toLong())
                else -> metricJson.put(key, value.toDouble())
            }
        }
        return JSONObject()
            .put("sequence", sequence)
            .put("occurred_at_utc", draft.occurredAtUtc)
            .put("session_id", draft.sessionId)
            .put("kind", draft.kind)
            .put("level", draft.level)
            .put("outcome", draft.outcome)
            .put("reason_code", draft.reasonCode ?: JSONObject.NULL)
            .put("drive_id", draft.driveId ?: JSONObject.NULL)
            .put("metrics", metricJson)
    }

    private fun isStoredEventValid(event: JSONObject, cutoff: Instant, reference: Instant): Boolean =
        runCatching {
            if (event.keysSet() != EVENT_FIELDS) return@runCatching false
            val sequence = event.getLong("sequence")
            val occurred = Instant.parse(event.getString("occurred_at_utc"))
            val reason = event.optNullableString("reason_code")
            val drive = event.optNullableString("drive_id")
            val metrics = event.getJSONObject("metrics")
            sequence > 0L && occurred >= cutoff && occurred <= reference.plusSeconds(300) &&
                isCanonicalUuid(event.getString("session_id")) &&
                event.getString("kind") in APP_EVENT_KINDS &&
                event.getString("level") in APP_EVENT_LEVELS &&
                event.getString("outcome") in APP_EVENT_OUTCOMES &&
                (reason == null || reason in APP_EVENT_REASON_CODES) &&
                (drive == null || isSafeDriveId(drive)) &&
                metrics.keysSet().all { key ->
                    val metric = metrics.opt(key)
                    val limit = APP_EVENT_METRIC_LIMITS[key]
                    metric is Number && limit != null && metric.toDouble().isFinite() &&
                        metric.toDouble() in limit
                }
        }.getOrDefault(false)

    companion object {
        private const val PREFERENCES = "obd_app_event_journal"
        private const val KEY_SOURCE_ID = "source_id"
        private const val KEY_LAST_SEQUENCE = "last_sequence"
        private const val KEY_EVENTS = "events_json"
        private val GLOBAL_LOCK = Any()
        private val EVENT_FIELDS = setOf(
            "sequence",
            "occurred_at_utc",
            "session_id",
            "kind",
            "level",
            "outcome",
            "reason_code",
            "drive_id",
            "metrics",
        )
    }
}

/**
 * Non-blocking, ordered service facade. Disk work is confined to one IO consumer; a full queue
 * drops the new event and the next successful drain records a numeric gap summary.
 */
internal class AppEventEmitter(
    private val journal: AppEventDraftSink,
    private val sessionId: String,
    private val scope: CoroutineScope,
    private val now: () -> Instant = Instant::now,
    capacity: Int = 256,
) {
    private val queue = Channel<AppEventDraft>(capacity)
    private val depth = AtomicInteger(0)
    private val dropped = AtomicLong(0L)
    private val terminal = AtomicReference<AppEventDraft?>(null)
    private val worker = scope.launch {
        for (draft in queue) {
            depth.updateAndGet { value -> (value - 1).coerceAtLeast(0) }
            persistPendingGap()
            persistOrCountDrop(draft)
        }
        terminal.getAndSet(null)?.let { draft ->
            persistPendingGap()
            persistOrCountDrop(draft)
        }
    }

    init {
        require(capacity in 1..4_096)
    }

    fun emit(
        kind: String,
        level: String,
        outcome: String,
        reasonCode: String? = null,
        driveId: String? = null,
        metrics: Map<String, Number> = emptyMap(),
    ): Boolean {
        val queuedDepth = depth.incrementAndGet()
        val withDepth = LinkedHashMap(metrics)
        withDepth.putIfAbsent("queue_depth", queuedDepth)
        val result = queue.trySend(
            AppEventDraft(
                occurredAtUtc = now().toString(),
                sessionId = sessionId,
                kind = kind,
                level = level,
                outcome = outcome,
                reasonCode = reasonCode,
                driveId = driveId,
                metrics = withDepth,
            ),
        )
        if (result.isFailure) {
            depth.updateAndGet { value -> (value - 1).coerceAtLeast(0) }
            dropped.incrementAndGet()
        }
        return result.isSuccess
    }

    fun closeBestEffort() {
        queue.close()
    }

    /** Reserve the service terminal record outside the bounded producer queue, then drain. */
    fun closeWithTerminal(
        kind: String,
        level: String,
        outcome: String,
        reasonCode: String? = null,
        driveId: String? = null,
        metrics: Map<String, Number> = emptyMap(),
    ): Boolean {
        val withDepth = LinkedHashMap(metrics)
        withDepth.putIfAbsent("queue_depth", depth.get())
        val installed = terminal.compareAndSet(
            null,
            AppEventDraft(
                occurredAtUtc = now().toString(),
                sessionId = sessionId,
                kind = kind,
                level = level,
                outcome = outcome,
                reasonCode = reasonCode,
                driveId = driveId,
                metrics = withDepth,
            ),
        )
        queue.close()
        return installed
    }

    internal fun queuedCount(): Int = depth.get()

    internal fun droppedCount(): Long = dropped.get()

    internal fun workerJob() = worker

    private fun recordDropped(count: Long) {
        if (count <= 0L) return
        dropped.updateAndGet { current ->
            if (current >= 1_000_000_000L - count) 1_000_000_000L else current + count
        }
    }

    private fun persistPendingGap() {
        val gap = dropped.getAndSet(0L)
        if (gap <= 0L) return
        val persisted = runCatching {
            journal.appendDraft(
                AppEventDraft(
                    occurredAtUtc = now().toString(),
                    sessionId = sessionId,
                    kind = "app.service",
                    level = "warning",
                    outcome = "pruned",
                    reasonCode = "unknown",
                    driveId = null,
                    metrics = mapOf(
                        "gap_count" to gap.coerceAtMost(1_000_000_000L),
                        "queue_depth" to depth.get(),
                    ),
                ),
            )
        }.getOrDefault(false)
        if (!persisted) recordDropped(gap)
    }

    private fun persistOrCountDrop(draft: AppEventDraft) {
        if (!runCatching { journal.appendDraft(draft) }.getOrDefault(false)) {
            recordDropped(1L)
        }
    }
}

private fun JSONObject.keysSet(): Set<String> = buildSet {
    val iterator = keys()
    while (iterator.hasNext()) add(iterator.next())
}

private fun JSONObject.optNullableString(key: String): String? =
    if (isNull(key)) null else getString(key)

private fun isCanonicalUuid(value: String): Boolean = runCatching {
    val parsed = UUID.fromString(value)
    parsed.toString() == value && parsed.version() in setOf(4, 7)
}.getOrDefault(false)
