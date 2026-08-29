package com.dashcamstats.obdlogger

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import kotlin.math.min

class ObdLoggerService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var database: ObdDatabase
    private lateinit var exporter: BundleExporter
    private var worker: Job? = null
    private var client: ElmBleClient? = null
    private var lastDriveId: String? = null
    private var lastDriveFinished: String? = null
    private var startupAllowed = false
    private val statusWriteGate = StatusWriteGate()

    override fun onCreate() {
        super.onCreate()
        val config = LoggerPreferences.load(this)
        when (ServiceStartupGate.decide(config.canRun, hasLoggerPermissions(this))) {
            ServiceStartupDecision.STOP_PERMISSION_REQUIRED -> {
                LoggerPreferences.disable(this)
                runCatching {
                    StatusPublisher.error(
                        this,
                        config.ownershipTransferred,
                        "permission_required",
                        "Nearby Devices or notification permission is missing; open the logger to re-enable it",
                    )
                }
                stopSelf()
                return
            }
            ServiceStartupDecision.STOP_DISABLED -> {
                runCatching {
                    StatusPublisher.error(
                        this,
                        config.ownershipTransferred,
                        "disabled",
                        "logger enable, ownership, identity, adapter or engine-gate configuration is invalid",
                    )
                }
                stopSelf()
                return
            }
            ServiceStartupDecision.START -> startupAllowed = true
        }
        database = ObdDatabase(this)
        exporter = BundleExporter(this, database)
        createNotificationChannel()
        startConnectedDeviceForeground("Starting safely")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!startupAllowed) {
            stopSelfResult(startId)
            return START_NOT_STICKY
        }
        if (worker?.isActive != true) worker = scope.launch { runLogger() }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        worker?.cancel()
        client?.closeNow()
        scope.cancel()
        // Do not close SQLite here: the IO coroutine can be between its final DML and
        // setTransactionSuccessful when Android calls onDestroy on the main thread.
        // Closing underneath it creates the very partial-write race WAL is meant to avoid.
        // Process teardown closes the handle; a normal next service instance opens it again.
        super.onDestroy()
    }

    private suspend fun runLogger() {
        var recovered = false
        var failure = 0
        while (scope.isActive) {
            var config: LoggerConfig? = null
            var retryDelayMillis: Long? = null
            try {
                if (!recovered) {
                    database.recoverInterrupted()
                    database.lastCompletedDrive()?.let { last ->
                        lastDriveId = last.driveId
                        lastDriveFinished = last.finishedAtUtc
                    }
                    recovered = true
                }
                val loadedConfig = LoggerPreferences.load(this)
                config = loadedConfig
                if (!loadedConfig.canRun) {
                    publish("disabled", loadedConfig, "ownership or logger enable gate is off")
                    stopSelf()
                    return
                }
                if (!hasLoggerPermissions(this)) {
                    LoggerPreferences.disable(this)
                    publish(
                        "permission_required",
                        loadedConfig,
                        "Nearby Devices or notification permission was revoked",
                    )
                    stopSelf()
                    return
                }
                if (DeviceFiles.removableRootOrNull(this) == null) {
                    publish(
                        "storage_unavailable",
                        loadedConfig,
                        "removable TF storage is not mounted; logging and export are paused",
                    )
                    updateNotification("Waiting for removable TF storage")
                    retryDelayMillis = 30_000
                } else {
                    // A process death after finishDrive(), or a prior filesystem failure, leaves
                    // the complete drive waiting for this next safe export attempt.
                    drainPendingExports()
                    runOneConnection(loadedConfig)
                    failure = 0
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                // The failed stage may have committed startDrive before a later DB/status call
                // failed. Re-run interrupted-drive recovery before any new connection attempt.
                recovered = false
                failure += 1
                val message = safeError(error)
                val statusConfig = config ?: runCatching { LoggerPreferences.load(this) }.getOrNull()
                if (statusConfig != null) publish("backoff", statusConfig, message)
                runCatching { updateNotification("Waiting after logger failure") }
                val delaySeconds = if (failure <= 5) min(1L shl failure, 32L) else 300L
                retryDelayMillis = delaySeconds * 1000
                if (failure > 5) failure = 0
            } finally {
                try {
                    client?.disconnect(false)
                } catch (_: Exception) {
                    // A tainted or vanished link is closed by BluetoothGatt itself.
                }
                client = null
            }
            retryDelayMillis?.let { delay(it) }
        }
    }

    private suspend fun runOneConnection(config: LoggerConfig) {
        updateNotification("Checking adapter voltage")
        publish("parked", config)
        val elm = ElmBleClient(this, config.adapterAddress)
        client = elm
        elm.connect()
        val lifecycle = EngineLifecycle(
            voltageOn = config.voltageOn,
            voltageOff = config.voltageOff,
            graceMillis = config.offGraceSeconds * 1000,
        )
        val voltage = elm.initialize()
        if (!lifecycle.observeParkedVoltage(voltage)) {
            elm.disconnect()
            updateNotification("Parked; next safe voltage check in 30 s")
            publish("parked", config)
            delay(30_000)
            return
        }
        publish("probing", config)
        updateNotification("Proving ECU response")
        val supported = elm.proveEcu()
        val protocolNumber = try {
            elm.queryProtocolNumber()
        } catch (_: Exception) {
            null
        }
        check(lifecycle.acceptChecksumValidEcuProof(true))
        val driveId = uuid7()
        val startedAt = Instant.now().toString()
        database.startDrive(
            DriveRecord(
                driveId = driveId,
                vehicleId = config.vehicleId,
                adapterId = "ble-${sha256(config.adapterAddress.uppercase().toByteArray()).take(12)}",
                loggerId = config.loggerId,
                loggerVersion = BuildConfig.VERSION_NAME,
                startedAtUtc = startedAt,
                originalTimezone = ZoneId.systemDefault().id,
                startReason = "charging_voltage_and_checksum_valid_0100",
                obdProtocol = "AUTO, ISO 9141-2",
            ),
        )
        database.addDiagnostic(
            driveId,
            "protocol_change",
            JSONObject()
                .put("protocol", "AUTO, ISO 9141-2")
                .put("protocol_number", protocolNumber ?: JSONObject.NULL),
            startedAt,
        )
        database.addDiagnostic(
            driveId,
            "mode01_support",
            JSONObject().put("supported_pids", JSONArray(supported.sorted())),
            startedAt,
        )
        publish("ecu_online", config)
        updateNotification("Recording vehicle telemetry")
        try {
            recordDrive(config, elm, driveId, supported, lifecycle)
        } catch (error: Exception) {
            database.incrementError(driveId)
            database.addDiagnostic(
                driveId,
                if (error is ElmProtocolException) "parser_failure" else "connection_failure",
                JSONObject()
                    .put("category", if (error is ElmProtocolException) "live_pid" else "ble_or_elm")
                    .put("message", safeError(error)),
            )
            lastDriveFinished = database.finishDrive(driveId, "connection_lost", false)
            lastDriveId = driveId
            drainPendingExports()
            throw error
        }
        elm.disconnect()
        lastDriveFinished = database.finishDrive(driveId, "engine_stopped", true)
        lastDriveId = driveId
        drainPendingExports()
        publish("parked", config)
    }

    private suspend fun recordDrive(
        config: LoggerConfig,
        elm: ElmBleClient,
        driveId: String,
        supported: Set<Int>,
        lifecycle: EngineLifecycle,
    ) {
        val diagnosticScan = DiagnosticScan(elm, driveId)
        var sequence = 0L
        while (scope.isActive) {
            val cycleStarted = SystemClock.elapsedRealtime()
            val requested = ObdPollPlan.requestedPids(sequence, supported)
            val values = linkedMapOf<String, Any>()
            val missing = mutableListOf<Int>()
            for (pid in requested) {
                val decoded = elm.query(pid)
                if (decoded.isEmpty()) missing += pid else values.putAll(decoded)
            }
            elm.readVoltage()?.let { values["adapter_voltage"] = it }
            values.putAll(ElmProtocol.estimates(values))
            val sampleTimestamp = Instant.now().toString()
            database.addSample(
                SampleRecord(
                    driveId = driveId,
                    sequence = sequence,
                    timestampUtc = sampleTimestamp,
                    values = values,
                    parserQuality = if (missing.isEmpty()) "ok" else "partial",
                    missingPids = missing,
                ),
            )
            // At most one sparse diagnostic command is allowed after a committed sample.
            // Even a 6-second prompt timeout therefore cannot turn a scan into a minute-long
            // hole in the fast stream. ElmBleClient's mutex still serializes the FFF1 channel.
            diagnosticScan.runOne(sequence, sampleTimestamp)
            val running = lifecycle.remainsRecording(
                SystemClock.elapsedRealtime(),
                values["adapter_voltage"] as? Double,
                values["engine_rpm"] as? Double,
            )
            if (!running) return
            sequence += 1
            publish("ecu_online", config)
            delay((5_000 - (SystemClock.elapsedRealtime() - cycleStarted)).coerceAtLeast(0))
        }
    }

    private data class DiagnosticStep(
        val category: String,
        val operation: suspend (String) -> Unit,
    )

    private inner class DiagnosticScan(
        private val elm: ElmBleClient,
        private val driveId: String,
    ) {
        private val steps = OneStepPerCycleQueue<DiagnosticStep>()
        private var nextScanAt = Long.MAX_VALUE
        private var freezeSupported = emptySet<Int>()
        private var freezeDtc: String? = null
        private val freezeValues = linkedMapOf<String, Any>()
        private val freezeMissing = mutableListOf<String>()
        private val freezeFailure = DiagnosticScanFailureTracker()

        init {
            enqueueScan()
        }

        suspend fun runOne(cycle: Long, observedAtUtc: String) {
            if (steps.isEmpty && cycle >= nextScanAt) enqueueScan()
            val step = steps.take(cycle) ?: return
            try {
                step.operation(observedAtUtc)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                val status = diagnosticProbeStatus(error)
                when {
                    step.category == "readiness" -> storeScanStatus(
                        "readiness_scan_complete", status, observedAtUtc,
                    )
                    step.category == "mode09_support" -> storeScanStatus(
                        "mode09_support_scan_complete", status, observedAtUtc,
                    )
                    step.category.startsWith("freeze_frame") -> storeScanStatus(
                        "freeze_frame_scan_complete",
                        status.also(freezeFailure::failed),
                        observedAtUtc,
                    )
                }
                database.addDiagnostic(
                    driveId,
                    diagnosticFailureKind(error),
                    JSONObject()
                        .put("category", step.category)
                        .put("message", safeError(error)),
                    observedAtUtc,
                )
                database.incrementError(driveId)
            }
            if (steps.isEmpty) nextScanAt = cycle + 30
        }

        private fun enqueueScan() {
            freezeSupported = emptySet()
            freezeDtc = null
            freezeValues.clear()
            freezeMissing.clear()
            freezeFailure.reset()
            add("readiness") { observedAtUtc ->
                val payload = elm.queryPayload(0x01)
                if (payload == null) {
                    storeScanStatus("readiness_scan_complete", "no_data", observedAtUtc)
                    return@add
                }
                val decoded = ElmProtocol.readiness(payload)
                database.addDiagnostic(
                    driveId,
                    "mil_state",
                    JSONObject().put("on", decoded["mil_on"] as Boolean),
                    observedAtUtc,
                )
                database.addDiagnostic(
                    driveId,
                    "readiness",
                    JSONObject()
                        .put("supported", JSONArray(decoded["supported"] as List<*>))
                        .put("incomplete", JSONArray(decoded["incomplete"] as List<*>))
                        .put("complete", decoded["complete"] as Boolean)
                        .put("confirmed_dtc_count", decoded["confirmed_dtc_count"] as Int)
                        .put("ignition_type", decoded["ignition_type"] as String),
                    observedAtUtc,
                )
                storeScanStatus("readiness_scan_complete", "ok", observedAtUtc)
            }
            val dtcScan = DtcScanCompletionTracker()
            for ((mode, kind) in listOf(
                0x03 to "confirmed_dtcs",
                0x07 to "pending_dtcs",
                0x0A to "permanent_dtcs",
            )) {
                add(kind) { observedAtUtc ->
                    try {
                        val result = elm.queryDtcsWithStatus(mode)
                        database.addDiagnostic(
                            driveId,
                            kind,
                            JSONObject().put("codes", JSONArray(result.codes)),
                            observedAtUtc,
                        )
                        storeDtcModeStatus(mode, result.status, observedAtUtc)
                        dtcScan.markSuccessful(mode)
                    } catch (error: Exception) {
                        storeDtcModeStatus(mode, probeStatus(error), observedAtUtc)
                        throw error
                    }
                }
            }
            add("dtc_scan_complete") { observedAtUtc ->
                if (dtcScan.isComplete) {
                    database.addDiagnostic(
                        driveId,
                        "dtc_scan_complete",
                        JSONObject().put(
                            "modes",
                            JSONArray(DtcScanCompletionTracker.REQUIRED_MODES.sorted()),
                        ),
                        observedAtUtc,
                    )
                }
            }
            add("mode09_support") { observedAtUtc ->
                val supported = elm.queryMode09Supported()
                if (supported.isNotEmpty()) {
                    database.addDiagnostic(
                        driveId,
                        "mode09_support",
                        JSONObject().put("supported_pids", JSONArray(supported.sorted())),
                        observedAtUtc,
                    )
                }
                storeScanStatus(
                    "mode09_support_scan_complete",
                    if (supported.isEmpty()) "no_data" else "ok",
                    observedAtUtc,
                )
            }
            // 0900 is advisory on the Tiida: paired count/data bits are advertised
            // unusually. Enqueue these bounded read-only probes independently so even a
            // rejected/malformed 0900 response cannot suppress them.
            val directProbes = ElmProtocol.mode09DirectProbePids()
            for ((pid, category) in listOf(
                0x03 to "calibration_id_message_count",
                0x05 to "calibration_verification_number_message_count",
            )) {
                if (pid in directProbes) {
                    add(category) { timestamp ->
                        try {
                            val count = elm.queryMode09Count(pid)
                            if (count != null) {
                                database.addDiagnostic(
                                    driveId,
                                    "mode09_count",
                                    JSONObject().put("pid", pid).put("count", count),
                                    timestamp,
                                )
                            }
                            storeMode09ProbeStatus(
                                pid, if (count == null) "no_data" else "ok", timestamp,
                            )
                        } catch (error: Exception) {
                            storeMode09ProbeStatus(pid, probeStatus(error), timestamp)
                            throw error
                        }
                    }
                }
            }
            if (0x04 in directProbes) {
                add("calibration_id") { timestamp ->
                    try {
                        val value = elm.queryCalibrationId()
                        if (value != null) {
                            database.addDiagnostic(
                                driveId,
                                "calibration_id",
                                JSONObject().put("value", value),
                                timestamp,
                            )
                        }
                        storeMode09ProbeStatus(
                            0x04, if (value == null) "no_data" else "ok", timestamp,
                        )
                    } catch (error: Exception) {
                        storeMode09ProbeStatus(0x04, probeStatus(error), timestamp)
                        throw error
                    }
                }
            }
            if (0x06 in directProbes) {
                add("calibration_verification_numbers") { timestamp ->
                    try {
                        val values = elm.queryCalibrationVerificationNumbers()
                        if (values.isNotEmpty()) {
                            database.addDiagnostic(
                                driveId,
                                "calibration_verification_numbers",
                                JSONObject().put("values", JSONArray(values)),
                                timestamp,
                            )
                        }
                        storeMode09ProbeStatus(
                            0x06, if (values.isEmpty()) "no_data" else "ok", timestamp,
                        )
                    } catch (error: Exception) {
                        storeMode09ProbeStatus(0x06, probeStatus(error), timestamp)
                        throw error
                    }
                }
            }
            add("freeze_frame_support") { observedAtUtc -> readFreezeSupport(observedAtUtc) }
        }

        private suspend fun readFreezeSupport(observedAtUtc: String) {
            val payload = elm.queryFreezeFramePayload(0x00, 4)
            if (payload == null) {
                storeScanStatus(
                    "freeze_frame_scan_complete", "no_data", observedAtUtc,
                )
                return
            }
            val mask = payload.fold(0L) { value, byte ->
                (value shl 8) or (byte.toLong() and 0xFF)
            }
            freezeSupported = (1..32).filter {
                mask and (1L shl (32 - it)) != 0L
            }.toSet()
            add("freeze_frame_dtc") { timestamp -> readFreezeDtc(timestamp) }
        }

        private suspend fun readFreezeDtc(observedAtUtc: String) {
            val payload = elm.queryFreezeFramePayload(0x02, 2)
            freezeDtc = payload?.let { ElmProtocol.dtcs(listOf(it)).firstOrNull() }
            if (freezeDtc == null) {
                storeFreeze(
                    JSONObject()
                        .put("status", "empty")
                        .put("frame", 0)
                        .put("dtc", JSONObject.NULL)
                        .put("values", JSONObject()),
                    observedAtUtc,
                )
                return
            }
            for (pid in (freezeSupported intersect ElmProtocol.freezeFramePids).sorted()) {
                add("freeze_frame_pid_%02X".format(pid)) { _ -> readFreezePid(pid) }
            }
            add("freeze_frame_complete") { timestamp ->
                val finalStatus = freezeFailure.finalStatus("ok")
                if (finalStatus == "ok") {
                    storeFreeze(
                        JSONObject()
                            .put("status", "ok")
                            .put("frame", 0)
                            .put("dtc", freezeDtc)
                            .put(
                                "supported_pids",
                                JSONArray(freezeSupported.sorted().map { "%02X".format(it) }),
                            )
                            .put("missing_pids", JSONArray(freezeMissing))
                            .put("values", JSONObject(freezeValues as Map<*, *>)),
                        timestamp,
                    )
                } else {
                    storeScanStatus("freeze_frame_scan_complete", finalStatus, timestamp)
                }
            }
        }

        private suspend fun readFreezePid(pid: Int) {
            try {
                val payload = elm.queryFreezeFramePayload(pid, ElmProtocol.pidLengths.getValue(pid))
                if (payload == null) {
                    freezeMissing += "%02X".format(pid)
                } else {
                    freezeValues.putAll(ElmProtocol.decode(pid, payload))
                }
            } catch (_: ElmCommandRejectedException) {
                // A rejection of one optional advertised PID is a missing value, matching
                // the direct coordinator; it does not invalidate the rest of the frame.
                freezeMissing += "%02X".format(pid)
            }
        }

        private fun storeFreeze(payload: JSONObject, observedAtUtc: String) {
            database.addDiagnostic(driveId, "freeze_frame", payload, observedAtUtc)
            storeScanStatus(
                "freeze_frame_scan_complete", payload.getString("status"), observedAtUtc,
            )
        }

        private fun storeScanStatus(kind: String, status: String, observedAtUtc: String) {
            database.addDiagnostic(
                driveId,
                kind,
                JSONObject().put("status", status),
                observedAtUtc,
            )
        }

        private fun storeMode09ProbeStatus(pid: Int, status: String, observedAtUtc: String) {
            database.addDiagnostic(
                driveId,
                "mode09_probe_status",
                JSONObject().put("pid", pid).put("status", status),
                observedAtUtc,
            )
        }

        private fun storeDtcModeStatus(mode: Int, status: String, observedAtUtc: String) {
            database.addDiagnostic(
                driveId,
                "dtc_mode_status",
                JSONObject().put("mode", mode).put("status", status),
                observedAtUtc,
            )
        }

        private fun probeStatus(error: Exception): String = diagnosticProbeStatus(error)

        private fun add(category: String, operation: suspend (String) -> Unit) {
            steps.add(DiagnosticStep(category, operation))
        }
    }

    private fun drainPendingExports() {
        exporter.reconcileMissingExports()
        for (driveId in database.prepareCompletedExports()) exportSafely(driveId)
        try {
            exporter.enforceRetention()
        } catch (error: Exception) {
            val config = runCatching { LoggerPreferences.load(this) }.getOrNull()
            if (config != null) {
                runCatching {
                    StatusPublisher.error(
                        this,
                        config.ownershipTransferred,
                        "retention_error",
                        safeError(error),
                    )
                }
            }
        }
    }

    private fun exportSafely(driveId: String) {
        try {
            exporter.export(driveId)
        } catch (error: Exception) {
            val config = runCatching { LoggerPreferences.load(this) }.getOrNull()
            if (error is RemovableStorageUnavailableException && config != null) {
                publish("storage_unavailable", config, safeError(error))
            } else if (config != null) {
                runCatching {
                    StatusPublisher.error(
                        this,
                        config.ownershipTransferred,
                        "export_error",
                        safeError(error),
                    )
                }
            }
        }
    }

    private fun publish(state: String, config: LoggerConfig, error: String? = null) {
        val signature = listOf(
            state,
            config.ownershipTransferred.toString(),
            lastDriveId.orEmpty(),
            lastDriveFinished.orEmpty(),
            error.orEmpty(),
        ).joinToString("\u0000")
        if (!statusWriteGate.shouldWrite(signature, SystemClock.elapsedRealtime())) return
        val status = PublicStatus(
            state = state,
            ownershipEnabled = config.ownershipTransferred,
            lastDriveId = lastDriveId,
            lastDriveFinishedAtUtc = lastDriveFinished,
            lastError = error,
            lastErrorAtUtc = error?.let { Instant.now().toString() },
        )
        runCatching {
            StatusPublisher.publish(this, status)
        }.onFailure {
            statusWriteGate.writeFailed(signature)
        }
    }

    private fun safeError(error: Exception): String {
        val raw = error.message ?: error.javaClass.simpleName
        return raw.replace(Regex("[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"), "<adapter>").take(240)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(
                    CHANNEL,
                    getString(R.string.notification_channel),
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    description = "Persistent read-only vehicle logger status"
                    setShowBadge(false)
                },
            )
        }
    }

    private fun notification(message: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL)
        } else {
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("Dashcam OBD logger")
            .setContentText(message)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
    }

    private fun startConnectedDeviceForeground(message: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification(message),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification(message))
        }
    }

    private fun updateNotification(message: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(message))
    }

    companion object {
        private const val CHANNEL = "obd_logger"
        private const val NOTIFICATION_ID = 1107
    }
}
