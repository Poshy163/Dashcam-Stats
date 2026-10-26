package com.dashcamstats.obdlogger

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
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
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

class ObdLoggerService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val eventScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var database: ObdDatabase
    private lateinit var exporter: BundleExporter
    private var worker: Job? = null
    private var client: ElmBleClient? = null
    private var currentDriveId: String? = null
    private var currentPollPlanVersion = ObdPollPlan.VERSION
    private val trialFallbackLatch = PollTrialFallbackLatch()
    private var lastDriveId: String? = null
    private var lastDriveFinished: String? = null
    private var lastSampleAt: String? = null
    private var ingestionRequestId: String? = null
    private var startupRecoveredDrive: DriveFinalization? = null
    private val pipelineMetrics = PipelineMetrics()
    private val foregroundFirstStartupGate = ForegroundFirstStartupGate()
    @Volatile
    private var startupAllowed = false
    private var pendingStartupRecoveryReason = "process_terminated"
    @Volatile
    private var collaboratorsInitialized = false
    private val statusWriteGate = StatusWriteGate()
    private var sleepWindowController: AdaptiveSleepWindowController? = null
    @Volatile private var bootDiagnostics: BootDiagnostics? = null
    private var temperatureCapture: TemperatureCapture? = null
    private lateinit var eventJournal: AppEventJournal
    private lateinit var eventEmitter: AppEventEmitter
    private val eventProjectionRetryQueued = AtomicBoolean(false)
    private var lastEventProjectionRetryAtMillis = 0L
    private val eventSessionId = UUID.randomUUID().toString()
    private var lastIngestionEventRequestId: String? = null
    private var lastAcknowledgedEventRequestId: String? = null
    private var ingestionEventStateKnown = false
    private var connectionAttempt = 0
    private var connectionStartedElapsedMillis: Long? = null
    private var driveStartedElapsedMillis: Long? = null
    private val parkedEventGate = ParkedObservationEventGate()
    @Volatile
    private var lastStatusState = "starting"
    @Volatile
    private var lastStatusError: String? = null
    private var sleepStatusPublishJob: Job? = null
    private val retryWakeSignals = Channel<LoggerWakeReason>(Channel.CONFLATED)
    private var retryWakeReceiverRegistered = false
    private var motionJournal: MotionJournal? = null
    private var clipIndexer: ClosedClipIndexer? = null
    private var passiveMotion: PassiveMotionObserver? = null
    private var movementVisibleLaunch = false
    private val movementLifecycleLock = Any()
    private var movementStopping = false

    private val retryWakeReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val reason = when (intent.action) {
                BluetoothAdapter.ACTION_STATE_CHANGED -> {
                    if (
                        intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR) ==
                        BluetoothAdapter.STATE_ON
                    ) {
                        LoggerWakeReason.BLUETOOTH_ON
                    } else {
                        null
                    }
                }
                Intent.ACTION_SCREEN_ON -> LoggerWakeReason.SCREEN_ON
                Intent.ACTION_USER_PRESENT -> LoggerWakeReason.USER_PRESENT
                Intent.ACTION_POWER_CONNECTED -> LoggerWakeReason.POWER_CONNECTED
                else -> null
            }
            if (reason != null) retryWakeSignals.trySend(reason)
        }
    }

    private sealed interface RecordingExit {
        data object EngineStopped : RecordingExit
        data class Quiesce(val requestRead: IngestionRequestRead) : RecordingExit
    }

    override fun onCreate() {
        super.onCreate()
        eventJournal = AppEventJournal(applicationContext)
        eventEmitter = AppEventEmitter(eventJournal, eventSessionId, eventScope)
        emitEvent("app.service", "info", "started", "service_started")
        val config = LoggerPreferences.load(this, includeWebhookCredential = false)
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
            ServiceStartupDecision.START -> {
                createNotificationChannel()
                startConnectedDeviceForeground("Starting safely")
                foregroundFirstStartupGate.markForegroundStarted()
                startupAllowed = true
                runCatching { registerRetryWakeReceiver() }
                sleepWindowController = runCatching {
                    AdaptiveSleepWindowController(
                        applicationContext,
                        scope,
                        observation = ::recordSleepWindowObservation,
                    ).also { it.start() }
                }.getOrNull()
                // Temperature evidence has its own supervised lifetime. It is not a
                // child of the OBD worker, so reloads and ingestion quiescing cannot
                // stop it. Neither startup nor recovery changes native sleep admission.
                temperatureCapture = runCatching {
                    TemperatureCapture.create(applicationContext, scope).also { it.start() }
                }.getOrNull()
                // Advisory startup history is independent of the native OFF tracker.
                // Do not put exit-history or private-file IO ahead of foreground or sleep recovery.
                scope.launch {
                    runCatching {
                        BootDiagnostics(applicationContext).also {
                            bootDiagnostics = it
                            it.observeServiceStart()
                        }
                    }
                }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        emitEvent("app.service", "info", "observed", "start_command")
        if (!startupAllowed) {
            stopSelfResult(startId)
            return START_NOT_STICKY
        }
        movementVisibleLaunch = intent?.getBooleanExtra(EXTRA_MOVEMENT_FROM_VISIBLE_ACTIVITY, false) == true
        passiveMotion?.start(movementVisibleLaunch) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, notification("Recording telemetry and passive movement evidence"),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
            }
        }
        when (
            serviceWorkerDecision(
                workerActive = worker?.isActive == true,
                configurationReloadRequested = intent?.action == ACTION_RELOAD_CONFIGURATION,
            )
        ) {
            ServiceWorkerDecision.START -> {
                pendingStartupRecoveryReason = startupRecoveryReason(
                    intent?.getStringExtra(EXTRA_STARTUP_RECOVERY_REASON),
                )
                worker = scope.launch { initializeAndRunLogger() }
            }
            ServiceWorkerDecision.RESTART_FOR_CONFIGURATION -> {
                val previous = checkNotNull(worker)
                worker = scope.launch {
                    previous.cancelAndJoin()
                    client?.disconnect(false)
                    client = null
                    pendingStartupRecoveryReason = "process_terminated"
                    if (collaboratorsInitialized) runLogger() else initializeAndRunLogger()
                }
            }
            ServiceWorkerDecision.KEEP_RUNNING -> Unit
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        if (::eventEmitter.isInitialized) {
            eventEmitter.closeWithTerminal(
                "app.service",
                "info",
                "completed",
                "service_stopped",
            )
            eventScope.launch {
                withTimeoutOrNull(2_000L) { eventEmitter.workerJob().join() }
                eventScope.cancel()
            }
        } else {
            eventScope.cancel()
        }
        worker?.cancel()
        client?.closeNow()
        if (retryWakeReceiverRegistered) {
            runCatching { unregisterReceiver(retryWakeReceiver) }
            retryWakeReceiverRegistered = false
        }
        retryWakeSignals.close()
        sleepWindowController?.close()
        sleepWindowController = null
        temperatureCapture?.close()
        temperatureCapture = null
        synchronized(movementLifecycleLock) {
            movementStopping = true
            passiveMotion?.stop()
            clipIndexer?.stop()
            motionJournal?.stop()
        }
        scope.cancel()
        // Do not close SQLite here: the IO coroutine can be between its final DML and
        // setTransactionSuccessful when Android calls onDestroy on the main thread.
        // Closing underneath it creates the very partial-write race WAL is meant to avoid.
        // Process teardown closes the handle; a normal next service instance opens it again.
        super.onDestroy()
    }

    private fun registerRetryWakeReceiver() {
        val filter = IntentFilter().apply {
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
            addAction(Intent.ACTION_POWER_CONNECTED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(retryWakeReceiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(retryWakeReceiver, filter)
        }
        retryWakeReceiverRegistered = true
    }

    /**
     * Database construction can checkpoint, copy and validate an existing database during an
     * upgrade. Keep all of that work on the service IO scope and only publish the lateinit fields
     * after both collaborators were constructed successfully.
     */
    private suspend fun initializeAndRunLogger() {
        try {
            val initializedDatabase = foregroundFirstStartupGate.afterForeground {
                ObdDatabase(this@ObdLoggerService)
            }
            val initializedExporter = BundleExporter(this@ObdLoggerService, initializedDatabase)
            database = initializedDatabase
            exporter = initializedExporter
            collaboratorsInitialized = true
            // Optional reporting has independent failure handling and cannot disable telemetry.
            runCatching {
                synchronized(movementLifecycleLock) {
                    if (!movementStopping && motionJournal == null) {
                        MotionJournal(applicationContext).also { journal ->
                            motionJournal = journal
                            clipIndexer = ClosedClipIndexer(applicationContext, journal, scope, canAnalyze = {
                                val sleep = sleepWindowController?.snapshot()
                                journal.parkedNow() && sleep?.ingestionStateKnown == true &&
                                    !sleep.ingestionRequestActive && currentDriveId == null
                            })
                            passiveMotion = PassiveMotionObserver(applicationContext, journal).also { observer ->
                                // A background restart requires background location permission.
                                observer.start(movementVisibleLaunch) {
                                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) startForeground(
                                        NOTIFICATION_ID, notification("Recording telemetry and passive movement evidence"),
                                        ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
                                }
                            }
                        }
                    }
                }
            }
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            startupAllowed = false
            val config = runCatching { LoggerPreferences.load(this@ObdLoggerService) }.getOrNull()
            val message = safeError(error)
            runCatching {
                StatusPublisher.error(
                    this@ObdLoggerService,
                    config?.ownershipTransferred == true,
                    "startup_failed",
                    message,
                )
            }
            runCatching { updateNotification("Logger startup failed") }
            stopSelf()
            return
        }
        runLogger()
    }

    private suspend fun runLogger() {
        var recovered = false
        var recoveryReason = pendingStartupRecoveryReason
        var clearLeaseAfterBoot = recoveryReason == "device_restart"
        val connectionRetry = ConnectionRetryController()
        val reconnectAttempts = ReconnectAttemptTracker()
        while (scope.isActive) {
            var config: LoggerConfig? = null
            var retryDelayMillis: Long? = null
            var retryDeviceRoot: File? = null
            var retryMayBePreemptedByIngestion = false
            try {
                if (!recovered) {
                    val recoveredDrives = database.recoverInterrupted(recoveryReason)
                    for (drive in recoveredDrives) {
                        database.addDiagnostic(
                            drive.driveId,
                            "pipeline_metrics",
                            pipelineMetrics.snapshot().toJson(),
                            drive.finalisedAtUtc,
                        )
                        emitEvent(
                            kind = "drive.lifecycle",
                            level = "warning",
                            outcome = "recovered",
                            reasonCode = if (recoveryReason == "device_restart") {
                                "device_restart"
                            } else {
                                "uncaught_restart"
                            },
                            driveId = drive.driveId,
                            metrics = mapOf("sample_count" to drive.sampleCount),
                        )
                    }
                    startupRecoveredDrive = recoveredDrives.lastOrNull()
                    database.lastCompletedDrive()?.let { last ->
                        lastDriveId = last.driveId
                        lastDriveFinished = last.finishedAtUtc
                        lastSampleAt = last.lastSampleAtUtc
                    }
                    recovered = true
                    recoveryReason = "process_terminated"
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
                val deviceRoot = DeviceFiles.removableRootOrNull(this)
                if (deviceRoot == null) {
                    publish(
                        "storage_unavailable",
                        loadedConfig,
                        "removable TF storage is not mounted; logging and export are paused",
                    )
                    updateNotification("Waiting for removable TF storage")
                    retryDelayMillis = 30_000
                } else {
                    if (clearLeaseAfterBoot) {
                        IngestionQuiesceFiles.clearLeaseAfterBoot(deviceRoot)
                        ingestionRequestId = null
                        clearLeaseAfterBoot = false
                    }
                    when (val requestRead = readIngestionRequest(deviceRoot)) {
                        IngestionRequestRead.Absent -> {
                            val resumed = IngestionQuiesceFiles.clearAcknowledgement(deviceRoot)
                            ingestionRequestId = null
                            if (resumed) {
                                emitEvent("ingest.handoff", "info", "resumed", "resume_completed")
                                publish("parked", loadedConfig)
                            }
                            val reconnecting = reconnectAttempts.nextAttemptIsReconnect()
                            try {
                                runOneConnection(
                                    loadedConfig,
                                    deviceRoot,
                                    reconnecting,
                                    onEcuProof = {
                                        connectionRetry.progressConfirmed()
                                        clearRetryWakeSignals()
                                    },
                                )
                            } catch (error: Exception) {
                                if (error !is CancellationException) {
                                    reconnectAttempts.connectionFailed()
                                }
                                throw error
                            }
                            startupRecoveredDrive = null
                            connectionRetry.progressConfirmed()
                        }
                        is IngestionRequestRead.Invalid -> {
                            ingestionRequestId = null
                            publish("ingestion_request_invalid", loadedConfig, requestRead.reason)
                            updateNotification("Ingestion request is invalid; OBD polling paused")
                            retryDelayMillis = 1_000
                        }
                        is IngestionRequestRead.Valid -> {
                            ingestionRequestId = requestRead.request.requestId
                            prepareParkedForIngestion(deviceRoot, loadedConfig, requestRead.request)
                            retryDelayMillis = 500
                            connectionRetry.progressConfirmed()
                        }
                    }
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                // The failed stage may have committed startDrive before a later DB/status call
                // failed. Re-run interrupted-drive recovery before any new connection attempt.
                recovered = false
                recoveryReason = "process_terminated"
                val message = safeError(error)
                val statusConfig = config ?: runCatching { LoggerPreferences.load(this) }.getOrNull()
                val root = DeviceFiles.removableRootOrNull(this)
                val request = root?.let(::readIngestionRequest)
                retryDeviceRoot = root
                retryMayBePreemptedByIngestion = root != null
                if (root != null && request is IngestionRequestRead.Valid) {
                    runCatching {
                        IngestionQuiesceFiles.publishFailed(root, request.request, message)
                    }
                }
                if (statusConfig != null) publish("backoff", statusConfig, message)
                runCatching { updateNotification("Waiting after logger failure") }
                val failure = connectionRetry.failed()
                retryDelayMillis = failure.delayMillis
                emitEvent(
                    kind = "obd.ble_connection",
                    level = "warning",
                    outcome = "retrying",
                    reasonCode = "backoff_scheduled",
                    metrics = mapOf(
                        "attempt" to connectionAttempt,
                        "consecutive_failures" to failure.consecutiveFailures,
                        "retry_delay_ms" to failure.delayMillis,
                    ),
                )
            } finally {
                try {
                    client?.disconnect(false)
                } catch (_: Exception) {
                    // A tainted or vanished link is closed by BluetoothGatt itself.
                }
                client = null
                connectionStartedElapsedMillis = null
            }
            retryDelayMillis?.let { delayMillis ->
                val result = awaitInterruptibleDelay(
                    durationMillis = delayMillis,
                    wakeSignals = retryWakeSignals,
                    preempted = {
                        retryMayBePreemptedByIngestion && retryDeviceRoot?.let { root ->
                            runCatching {
                                readIngestionRequest(root) !is IngestionRequestRead.Absent
                            }.getOrDefault(true)
                        } == true
                    },
                    monotonicMillis = SystemClock::elapsedRealtime,
                )
                if (result is InterruptibleWaitResult.Woken) {
                    connectionRetry.externalWakeObserved()
                    emitEvent(
                        "app.service",
                        "info",
                        "resumed",
                        result.reason.eventReasonCode(),
                    )
                }
            }
        }
    }

    private suspend fun runOneConnection(
        config: LoggerConfig,
        deviceRoot: File,
        reconnecting: Boolean,
        onEcuProof: () -> Unit,
    ) {
        trialFallbackLatch.beforeConnection { LoggerPreferences.setPollTrial(this, false, it) }
        updateNotification("Checking adapter voltage")
        publish("parked", config)
        if (reconnecting) pipelineMetrics.reconnectAttempted()
        val driveClock = MonotonicUtcClock(
            anchorUtc = Instant.now(),
            anchorElapsedMillis = SystemClock.elapsedRealtime(),
            elapsedRealtimeMillis = SystemClock::elapsedRealtime,
        )
        val elm = ElmBleClient(
            this,
            config.adapterAddress,
            pipelineMetrics,
            driveClock::nowUtc,
            if (config.voltageOnlyMode || BuildConfig.VOLTAGE_ONLY_AUDIT) {
                ElmCommandPolicy.VOLTAGE_ONLY
            } else {
                ElmCommandPolicy.FULL_OBD
            },
        )
        client = elm
        connectionAttempt = (connectionAttempt + 1).coerceAtMost(10_000)
        val connectStarted = SystemClock.elapsedRealtime()
        connectionStartedElapsedMillis = connectStarted
        if (reconnecting) {
            emitEvent(
                "obd.ble_connection",
                "info",
                "started",
                "scheduled_connect",
                metrics = mapOf("attempt" to connectionAttempt),
            )
        }
        try {
            elm.connect()
        } catch (error: Exception) {
            emitEvent(
                "obd.ble_connection",
                "error",
                "failed",
                bleConnectionFailureReason(error),
                metrics = mapOf(
                    "attempt" to connectionAttempt,
                    "connect_ms" to boundedElapsedMillis(connectStarted),
                ),
            )
            throw error
        }
        if (reconnecting) {
            emitEvent(
                "obd.ble_connection",
                "info",
                "succeeded",
                "notifications_ready",
                metrics = mapOf(
                    "attempt" to connectionAttempt,
                    "connect_ms" to boundedElapsedMillis(connectStarted),
                ),
            )
        }
        val lifecycle = EngineLifecycle(
            voltageOn = config.voltageOn,
            voltageOff = config.voltageOff,
            graceMillis = config.offGraceSeconds * 1000,
        )
        val controlPresent = {
            readIngestionRequest(deviceRoot) !is IngestionRequestRead.Absent
        }
        val parkedVoltage = try {
            elm.probeAdapterVoltage(controlPresent)
        } catch (_: ElmQuiesceRequestedException) {
            elm.disconnect(false)
            client = null
            return
        } catch (error: Exception) {
            emitEvent(
                "obd.elm_session",
                "error",
                "failed",
                if (error is ElmCommandTimeoutException) "elm_timeout" else "connection_failed",
            )
            throw error
        }
        val engineCandidate = parkedProbeMayInitialize(
            config.voltageOnlyMode || BuildConfig.VOLTAGE_ONLY_AUDIT,
            parkedVoltage,
            config.voltageOn,
        )
        val parkedTransition = parkedEventGate.changed(
            if (engineCandidate) {
                ParkedObservationBand.ENGINE_CANDIDATE
            } else {
                ParkedObservationBand.BELOW_START
            },
        )
        if (parkedVoltage != null && parkedTransition) {
            emitEvent("obd.elm_session", "info", "verified", "adapter_voltage_valid")
        }
        if (controlPresent()) {
            elm.disconnect(false)
            client = null
            return
        }
        val voltageOnlyMode = config.voltageOnlyMode || BuildConfig.VOLTAGE_ONLY_AUDIT
        if (!engineCandidate) {
            if (parkedTransition) {
                emitEvent("obd.ecu_session", "info", "skipped", "voltage_below_start")
            }
            // No protocol was opened and no ECU command was sent, so a local GATT close is the
            // complete parked teardown. In particular, do not turn a cheap voltage probe into an
            // ATZ/configuration/ATPC cycle while the engine remains off.
            elm.disconnect(false)
            client = null
            // Startup/export repair is intentionally below the first safe voltage observation.
            // Slow removable-storage work must not delay discovering that the engine is running.
            drainPendingExports()
            updateNotification(
                "Parked; next safe voltage check in ${config.parkedIntervalSeconds} s",
            )
            publish("parked", config)
            waitForNextParkedProbe(deviceRoot, config.parkedIntervalSeconds * 1_000)
            return
        }
        if (!reconnecting) {
            emitEvent(
                "obd.ble_connection",
                "info",
                "succeeded",
                "notifications_ready",
                metrics = mapOf(
                    "attempt" to connectionAttempt,
                    "connect_ms" to boundedElapsedMillis(connectStarted),
                ),
            )
        }
        val elmStarted = SystemClock.elapsedRealtime()
        emitEvent("obd.elm_session", "info", "started", "adapter_voltage_valid")
        val voltage = try {
            elm.initialize(controlPresent).also {
                emitEvent(
                    "obd.elm_session",
                    "info",
                    "succeeded",
                    "elm_ready",
                    metrics = mapOf("elm_init_ms" to boundedElapsedMillis(elmStarted)),
                )
            }
        } catch (_: ElmQuiesceRequestedException) {
            elm.disconnect(false)
            client = null
            return
        } catch (error: Exception) {
            emitEvent(
                "obd.elm_session",
                "error",
                "failed",
                if (error is ElmCommandTimeoutException) "elm_timeout" else "protocol_search_failed",
                metrics = mapOf("elm_init_ms" to boundedElapsedMillis(elmStarted)),
            )
            throw error
        }
        if (controlPresent()) {
            elm.disconnect(false)
            client = null
            return
        }
        if (!lifecycle.observeParkedVoltage(voltage)) {
            emitEvent("obd.ecu_session", "info", "skipped", "voltage_below_stop")
            elm.disconnect(closeProtocol = !controlPresent())
            client = null
            drainPendingExports()
            updateNotification(
                "Parked; next safe voltage check in ${config.parkedIntervalSeconds} s",
            )
            publish("parked", config)
            waitForNextParkedProbe(deviceRoot, config.parkedIntervalSeconds * 1_000)
            return
        }
        publish("probing", config)
        updateNotification("Proving ECU response")
        val ecuProbeStarted = SystemClock.elapsedRealtime()
        emitEvent("obd.ecu_session", "info", "started", "engine_detected")
        val supported = try {
            elm.proveEcu(controlPresent).also {
                emitEvent(
                    "obd.ecu_session",
                    "info",
                    "succeeded",
                    "ecu_proof_valid",
                    metrics = mapOf("ecu_probe_ms" to boundedElapsedMillis(ecuProbeStarted)),
                )
            }
        } catch (_: ElmQuiesceRequestedException) {
            elm.disconnect(false)
            client = null
            return
        } catch (error: Exception) {
            emitEvent(
                "obd.ecu_session",
                "error",
                "failed",
                if (error is ElmCommandTimeoutException) "poll_timeout" else "ecu_offline",
                metrics = mapOf("ecu_probe_ms" to boundedElapsedMillis(ecuProbeStarted)),
            )
            throw error
        }
        if (controlPresent()) {
            elm.disconnect(false)
            client = null
            return
        }
        val protocolNumber = elm.queryProtocolNumber()
        if (controlPresent()) {
            elm.disconnect(false)
            client = null
            return
        }
        check(lifecycle.acceptChecksumValidEcuProof(true))
        // A checksum-valid ECU response is meaningful recovery even though this connection may
        // remain inside runOneConnection for the rest of the drive. Do not let old failures keep
        // escalating the next reconnect delay toward its cap.
        onEcuProof()
        val pollPlanVersion = LoggerPreferences.pollPlanForNewDrive(this, supported)
        val startedAt = driveClock.nowUtc()
        val driveId = uuid7(Instant.parse(startedAt).toEpochMilli())
        elm.beginDriveEvidence()
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
                pollPlanVersion = pollPlanVersion,
            ),
        )
        currentDriveId = driveId
        currentPollPlanVersion = pollPlanVersion
        driveStartedElapsedMillis = SystemClock.elapsedRealtime()
        emitEvent("drive.lifecycle", "info", "started", "engine_detected", driveId)
        elm.onResponseCountSuffixDisabled = {
            emitEvent(
                "obd.elm_session",
                "info",
                "changed",
                "response_count_suffix_unsupported",
                driveId,
            )
        }
        // The two PIDs that cannot change during a drive are read once here, not every
        // twelve cycles: their answers ride along in every sample so nothing downstream has
        // to learn a new shape, and the bus time they used to cost goes to live values.
        val staticValues = linkedMapOf<String, Any>()
        try {
            for (pid in ObdPollPlan.staticOnce.filter(supported::contains)) {
                if (controlPresent()) break
                try {
                    staticValues.putAll(elm.query(pid))
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (error: ElmProtocolException) {
                    // A malformed one-off answer is a missing static value, not a lost drive.
                    recordParserFailure(error)
                }
            }
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
            when (
                val exit = if (pollPlanVersion == PollTrialPlan.VERSION) recordTrialDrive(
                    config, elm, driveId, supported, lifecycle, deviceRoot, driveClock, staticValues,
                ) else recordDrive(
                    config, elm, driveId, supported, lifecycle, deviceRoot, driveClock, staticValues,
                )
            ) {
                RecordingExit.EngineStopped -> {
                    val finalised = finalizeActiveDrive(
                        driveId,
                        "engine_stopped",
                        elm.lastSuccessfulResponseAtUtc,
                        driveClock.nowUtc(),
                        driveClock::nowUtc,
                    )
                    drainPendingExports()
                    elm.disconnect()
                    client = null
                    publish("parked", config)
                    check(finalised.status == "complete")
                }
                is RecordingExit.Quiesce -> {
                    emitEvent(
                        "ingest.handoff",
                        "info",
                        "changed",
                        "quiesce_entered",
                        driveId,
                    )
                    val request = (exit.requestRead as? IngestionRequestRead.Valid)?.request
                    val finalised = finalizeActiveDrive(
                        driveId,
                        "ingestion_requested",
                        elm.lastSuccessfulResponseAtUtc,
                        driveClock.nowUtc(),
                        driveClock::nowUtc,
                    )
                    val exported = exportTerminalDriveForIngestion(finalised)
                    drainPendingExportsStrict()
                    database.checkpointForIngestion()
                    elm.disconnect(false)
                    client = null
                    if (request == null) {
                        publish(
                            "ingestion_request_invalid",
                            config,
                            (exit.requestRead as IngestionRequestRead.Invalid).reason,
                        )
                    } else if (requestStillActive(deviceRoot, request)) {
                        ingestionRequestId = request.requestId
                        IngestionQuiesceFiles.publishReady(
                            deviceRoot,
                            request,
                            acknowledgementMetadata(finalised, exported),
                        )
                        emitAcknowledgementOnce(request.requestId, finalised.driveId)
                        publish("ingestion_ready", config)
                        updateNotification("OBD persisted; ready for ingestion")
                    } else {
                        ingestionRequestId = null
                        IngestionQuiesceFiles.clearAcknowledgement(deviceRoot)
                        publish("parked", config, "ingestion request removed before acknowledgement")
                    }
                }
            }
        } catch (caught: Exception) {
            val error = if (pollPlanVersion == PollTrialPlan.VERSION && caught !is PollTrialFallbackException &&
                PollTrialPlan.transportFault(caught, elm.isSessionTainted)) {
                runCatching { trialFallbackLatch.disable("transport_fault") { LoggerPreferences.setPollTrial(this, false, it) } }
                PollTrialFallbackException("transport_fault", caught)
            } else caught
            val reason = when (error) {
                is PollTrialFallbackException -> "poll_trial_fallback"
                is CancellationException -> if (
                    runCatching { LoggerPreferences.load(this).canRun }.getOrDefault(true)
                ) {
                    "process_terminated"
                } else {
                    "administratively_disabled"
                }
                is ElmCommandTimeoutException -> "command_timeout"
                is ElmProtocolException -> "parser_failure"
                is android.database.SQLException -> "database_fault"
                else -> "connection_lost"
            }
            val faultAtUtc = driveClock.nowUtc()
            if (error is ElmProtocolException) recordParserFailure(error)
            runCatching { database.incrementError(driveId) }
            runCatching { database.recordProcessingError(driveId, safeError(error)) }
            runCatching {
                database.addDiagnostic(
                    driveId,
                    if (error is ElmProtocolException) "parser_failure" else "connection_failure",
                    JSONObject()
                        .put("category", when (error) {
                            is PollTrialFallbackException -> "poll_trial_fallback"
                            is ElmProtocolException -> "live_pid"
                            else -> "ble_or_elm"
                        })
                        .put("message", safeError(error)),
                    faultAtUtc,
                )
            }
            runCatching {
                finalizeActiveDrive(
                    driveId,
                    reason,
                    elm.lastSuccessfulResponseAtUtc,
                    faultAtUtc,
                    driveClock::nowUtc,
                )
            }
            drainPendingExports()
            throw error
        }
    }

    /**
     * Re-establish the ECU session after the bus has gone silent under a healthy adapter.
     *
     * Returns the supported-PID set to poll from here on: the freshly proved one when the
     * ECU answers again, or the previous one when it does not, so a failed recovery never
     * narrows the plan and silently drops signals that were working before.
     *
     * Deliberately best effort. Every failure mode here -- the ECU still absent, the
     * adapter dropping mid-probe, a quiesce arriving -- is one where continuing to poll is
     * strictly better than ending the drive, because the adapter is still answering and
     * voltage alone still proves the engine is running. The value is that the attempt and
     * its outcome are now *recorded*, which is exactly what drive 01a05d40 lacked.
     */
    private suspend fun recoverSilentBus(
        elm: ElmBleClient,
        driveId: String,
        deviceRoot: File,
        current: Set<Int>,
    ): Set<Int> {
        val startedAt = SystemClock.elapsedRealtime()
        emitEvent(
            "obd.ecu_session",
            "warning",
            "observed",
            "bus_silent",
            driveId = driveId,
            metrics = mapOf("silent_cycles" to ECU_SILENCE_RECOVERY_CYCLES.toLong()),
        )
        return try {
            val reproved = elm.proveEcu { currentQuiesceRequest(deviceRoot) != null }
            emitEvent(
                "obd.ecu_session",
                "info",
                "recovered",
                "ecu_proof_valid",
                driveId = driveId,
                metrics = mapOf("ecu_probe_ms" to boundedElapsedMillis(startedAt)),
            )
            reproved.ifEmpty { current }
        } catch (_: ElmQuiesceRequestedException) {
            // The transfer wants the adapter. The loop's own quiesce check owns that exit.
            current
        } catch (error: Exception) {
            emitEvent(
                "obd.ecu_session",
                "error",
                "failed",
                if (error is ElmCommandTimeoutException) "poll_timeout" else "ecu_offline",
                driveId = driveId,
                metrics = mapOf("ecu_probe_ms" to boundedElapsedMillis(startedAt)),
            )
            current
        }
    }

    private suspend fun recordDrive(
        config: LoggerConfig,
        elm: ElmBleClient,
        driveId: String,
        supported: Set<Int>,
        lifecycle: EngineLifecycle,
        deviceRoot: File,
        driveClock: MonotonicUtcClock,
        staticValues: Map<String, Any> = emptyMap(),
    ): RecordingExit {
        val diagnosticScan = DiagnosticScan(elm, driveId)
        val malformedLivePids = LivePidMalformedTracker()
        val sparseDiagnosticBudget = SparseDiagnosticBudgetTracker()
        val diagnosticScheduler = SparseDiagnosticScheduler()
        val liveScheduler = LivePollScheduler(SystemClock::elapsedRealtime)
        val pollTiming = PollTiming(driveOriginMillis = driveStartedElapsedMillis, monotonicMillis = SystemClock::elapsedRealtime)
        fun quiesceRequest() = pollTiming.measureStage(PollCycleStage.QUIESCE_CHECKS) {
            currentQuiesceRequest(deviceRoot)
        }
        var timingFlushReason = "drive_end"
        var sequence = 0L
        var cadenceOverrun = false
        // The vehicle bus can go silent while the adapter stays perfectly healthy: ATRV is
        // answered by the ELM itself and needs no ECU, so every cycle still "succeeds" and
        // persists a sample carrying nothing but adapter voltage. Observed on drive
        // 01a05d40: the ECU proof passed, then 79 consecutive samples arrived with every
        // bus PID missing and the drive was filed as complete with zero errors. Counting
        // consecutive fully-missing cycles is what turns that from silence into evidence.
        var busSilentCycles = 0
        var activeSupported = supported
        try {
            while (scope.isActive) {
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                val cycleStarted = SystemClock.elapsedRealtime()
                pollTiming.startCycle()
                val diagnosticReserved = diagnosticScheduler.reserveCycle(
                    sequence, diagnosticScan.prepareCycle(sequence),
                )
                val requested = ObdPollPlan.requestedPids(
                    sequence, activeSupported, diagnosticReserved,
                )
                val values = linkedMapOf<String, Any>()
                val missing = mutableListOf<Int>()
                val cycle = liveScheduler.beginCycle(sequence, requested, cycleStarted)
                var liveCommandAttempts = 0
                while (true) {
                    val quiesce = quiesceRequest()
                    val step = cycle.next(preempted = quiesce != null)
                    if (step is LivePollStep.Interrupted) {
                        persistPartialSample(
                            driveId,
                            sequence,
                            values,
                            missing + step.remainingPids,
                            driveClock.nowUtc(),
                            pollTiming,
                        )
                        return RecordingExit.Quiesce(checkNotNull(quiesce))
                    }
                    if (step === LivePollStep.Complete) break
                    if (step is LivePollStep.Deferred) {
                        missing += step.pid
                        continue
                    }
                    if (step === LivePollStep.ReadVoltage) {
                        val voltageStarted = SystemClock.elapsedRealtime()
                        try {
                            elm.readVoltage()?.let { values["adapter_voltage"] = it }
                        } catch (cancelled: CancellationException) {
                            throw cancelled
                        } catch (error: Exception) {
                            persistPartialSample(
                                driveId, sequence, values, missing + cycle.remainingPids,
                                driveClock.nowUtc(), pollTiming,
                            )
                            throw error
                        } finally {
                            val elapsed = boundedElapsedMillis(voltageStarted)
                            pollTiming.observeStage(PollCycleStage.AUXILIARY_REQUESTS, voltageStarted)
                            sparseDiagnosticBudget.observeCommand(elapsed)
                            liveScheduler.observeCommand(elapsed)
                        }
                        continue
                    }
                    val pid = (step as LivePollStep.QueryPid).pid
                    if (!malformedLivePids.shouldPoll(pid, sequence)) {
                        pollTiming.cooldownSkip(pid)
                        missing += pid
                        continue
                    }
                    cycle.attempted(pid)
                    liveCommandAttempts += 1
                    val commandStarted = SystemClock.elapsedRealtime()
                    val result = try {
                        timedLivePidPoll(pid, pollTiming) {
                            if (it in PollTrialPlan.critical) elm.query(it) else elm.queryOptional(it)
                        }
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (error: Exception) {
                        if (error is ElmCommandTimeoutException && error.promptRecovered && pid !in PollTrialPlan.critical) {
                            missing += pid
                            malformedLivePids.recordMalformed(pid, sequence)
                            recordRecoveredOptionalTimeout(driveId, pid, driveClock)
                            null
                        } else {
                            persistPartialSample(
                                driveId, sequence, values, missing + pid + cycle.remainingPids,
                                driveClock.nowUtc(), pollTiming,
                            )
                            throw error
                        }
                    } finally {
                        val elapsed = boundedElapsedMillis(commandStarted)
                        sparseDiagnosticBudget.observeCommand(elapsed)
                        liveScheduler.observeCommand(elapsed)
                    }
                    if (result == null) continue
                    when (result) {
                        LivePidPollResult.Missing -> {
                            malformedLivePids.recordValid(pid)
                            missing += pid
                        }
                        is LivePidPollResult.Values -> {
                            malformedLivePids.recordValid(pid)
                            values.putAll(result.decoded)
                            if (pid == 0x0D) (result.decoded["vehicle_speed"] as? Number)?.toDouble()?.let {
                                motionJournal?.speed(it)
                            }
                        }
                        is LivePidPollResult.Malformed -> {
                            missing += pid
                            recordParserFailure(result.error)
                            val message = safeError(result.error)
                            malformedLivePids.recordMalformed(pid, sequence)
                            database.incrementError(driveId)
                            database.addDiagnostic(
                                driveId,
                                "parser_failure",
                                JSONObject()
                                    .put("category", "live_pid_%02X".format(pid))
                                    .put("message", message),
                                driveClock.nowUtc(),
                            )
                        }
                    }
                }
                val primaryTailStarted = SystemClock.elapsedRealtime()
                values.putAll(staticValues)
                values.putAll(ElmProtocol.estimates(values))
                val sampleTimestamp = driveClock.nowUtc()
                val rowAges = pollTiming.takeRowAges()
                val samplePersisted = pollTiming.measureStage(PollCycleStage.PERSISTENCE) { persistSample(
                    SampleRecord(
                        driveId = driveId,
                        sequence = sequence,
                        timestampUtc = sampleTimestamp,
                        values = values,
                        parserQuality = if (missing.isEmpty()) "ok" else "partial",
                        missingPids = missing,
                    ),
                ) }
                if (samplePersisted) pollTiming.rowPersisted(rowAges)
                // A cycle where every requested PID went unanswered is the bus being silent,
                // not a slow sample. One such cycle is ordinary -- a single dropped frame, a
                // reconnect -- so recovery waits for a run of them.
                busSilentCycles = if (liveCommandAttempts > 0 && missing.size == requested.size) {
                    busSilentCycles + 1
                } else {
                    0
                }
                if (busSilentCycles == ECU_SILENCE_RECOVERY_CYCLES) {
                    activeSupported = recoverSilentBus(elm, driveId, deviceRoot, activeSupported)
                    busSilentCycles = 0
                }
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                val liveCycleElapsed = SystemClock.elapsedRealtime() - cycleStarted
                // A reservation frees optional live work without displacing due MAF. The fault
                // command and the still-outstanding full tail must fit the same five-second cycle;
                // already measured parse/persist work is not charged twice.
                val diagnosticBudgetMillis = sparseDiagnosticBudget.requiredBudgetMillis()
                var diagnosticMillis = 0L
                val diagnosticAllowed = samplePersisted && busSilentCycles == 0 &&
                    ObdPollPlan.mayRunSparseDiagnostic(
                        liveCycleElapsed, diagnosticBudgetMillis,
                        liveScheduler.remainingTailMillis(boundedElapsedMillis(primaryTailStarted)),
                    )
                if (diagnosticAllowed) {
                    val diagnosticStarted = SystemClock.elapsedRealtime()
                    try {
                        if (diagnosticScan.runOne(sequence, sampleTimestamp)) {
                            diagnosticScheduler.observeAttempt(sequence)
                        }
                    } finally {
                        pollTiming.observeStage(PollCycleStage.DIAGNOSTICS, diagnosticStarted)
                        diagnosticMillis = boundedElapsedMillis(diagnosticStarted)
                        sparseDiagnosticBudget.observeCommand(diagnosticMillis)
                    }
                }
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                val running = lifecycle.remainsRecording(
                    SystemClock.elapsedRealtime(),
                    values["adapter_voltage"] as? Double,
                    values["engine_rpm"] as? Double,
                )
                if (!running) {
                    pollTiming.finishCycle(completed = samplePersisted)
                    return RecordingExit.EngineStopped
                }
                val pollCycleMillis = boundedElapsedMillis(cycleStarted)
                val overrun = pollCycleMillis > ObdPollPlan.TARGET_CYCLE_MILLIS
                if (overrun != cadenceOverrun) {
                    cadenceOverrun = overrun
                    emitEvent(
                        kind = "obd.poll_health",
                        level = if (overrun) "warning" else "info",
                        outcome = if (overrun) "observed" else "recovered",
                        reasonCode = if (overrun) "cadence_gap" else "drive_summary",
                        driveId = driveId,
                        metrics = mapOf(
                            "poll_cycle_ms" to pollCycleMillis,
                            "polling_duty_cycle_percent" to (
                                pollCycleMillis.toDouble() /
                                    ObdPollPlan.TARGET_CYCLE_MILLIS.toDouble() * 100.0
                                ).coerceIn(0.0, 100.0),
                            "gap_count" to if (overrun) 1 else 0,
                            "command_count" to liveCommandAttempts,
                        ),
                    )
                }
                sequence += 1
                pollTiming.measureStage(PollCycleStage.STATUS_PUBLISH) { publish("ecu_online", config) }
                pollTiming.finishCycle(completed = samplePersisted)
                emitPollTiming(driveId, pollTiming, "periodic", driveClock)
                liveScheduler.observeTail((boundedElapsedMillis(primaryTailStarted) - diagnosticMillis).coerceAtLeast(0))
                awaitLivePollDeadline(
                    cycleStartedMillis = cycleStarted,
                    monotonicMillis = SystemClock::elapsedRealtime,
                    preempted = { !scope.isActive || quiesceRequest() != null },
                )
            }
            throw CancellationException("logger scope stopped")
        } catch (error: Exception) {
            timingFlushReason = "partial_failure"
            throw error
        } finally {
            pollTiming.finishCycle()
            emitPollTiming(driveId, pollTiming, timingFlushReason, driveClock)
        }
    }

    /** Separate loop keeps the established version-7 command order/admission untouched. */
    private suspend fun recordTrialDrive(
        config: LoggerConfig,
        elm: ElmBleClient,
        driveId: String,
        supported: Set<Int>,
        lifecycle: EngineLifecycle,
        deviceRoot: File,
        driveClock: MonotonicUtcClock,
        staticValues: Map<String, Any>,
    ): RecordingExit {
        val scheduler = PollTrialScheduler(SystemClock::elapsedRealtime)
        val health = PollTrialHealth()
        val malformed = LivePidMalformedTracker(nextOpportunityRetryPids = setOf(0x10))
        val timing = PollTiming(driveStartedElapsedMillis, PollTrialPlan.TARGET_MILLIS, SystemClock::elapsedRealtime)
        val quiesceReader = TrialQuiesceReader(SystemClock::elapsedRealtime) { currentQuiesceRequest(deviceRoot) }
        fun quiesceRequest() = timing.measureStage(PollCycleStage.QUIESCE_CHECKS) {
            quiesceReader.read()
        }
        val diagnosticScan = DiagnosticScan(elm, driveId)
        val origin = SystemClock.elapsedRealtime()
        val diagnosticSchedule = PollTrialDiagnosticSchedule(origin)
        var sequence = 0L
        var flushReason = "drive_end"
        var lastVoltage: Double? = null
        var lastVoltageAt: Long? = null

        fun fallback(reason: String, cause: Throwable? = null): Nothing {
            // Synchronous durable preference update precedes the fresh-connection recovery path.
            runCatching { trialFallbackLatch.disable(reason) { LoggerPreferences.setPollTrial(this, false, it) } }
            throw PollTrialFallbackException(reason, cause)
        }

        fun partial(values: Map<String, Any>, missing: List<Int>, fields: Map<Int, Set<String>>) {
            if (values.isEmpty()) return
            val capture = driveClock.capture()
            val prepared = prepareTimedTrialRow(values, fields, timing.takeRowAges(capture.elapsedMillis), missing)
            partialSampleAfterTransportFailure(driveId, sequence, capture.timestampUtc, prepared.values, prepared.missing)
                ?.copy(pidResponseAgeMs = prepared.ages)?.let {
                    if (timing.measureStage(PollCycleStage.PERSISTENCE) { persistSample(it) }) timing.rowPersisted(prepared.ages)
                }
        }

        try {
            while (scope.isActive) {
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                val started = SystemClock.elapsedRealtime()
                timing.startCycle()
                val wallCycle = PollTrialPlan.cooldownCycle(started - origin)
                val reserved = diagnosticSchedule.reserve(started, diagnosticScan.prepareCycle(wallCycle))
                val requested = PollTrialPlan.critical.toMutableList()
                val values = linkedMapOf<String, Any>()
                val decodedFields = linkedMapOf<Int, Set<String>>()
                val missing = mutableListOf<Int>()
                val criticalOutcomes = PollTrialPlan.critical
                    .associateWith { CriticalPollOutcome.UNPROVED }.toMutableMap()
                val commands = PollTrialPlan.critical.toMutableList()
                var commandIndex = 0
                var auxiliarySelected = false
                try {
                    while (true) {
                        if (commandIndex == commands.size && !auxiliarySelected) {
                            // Evaluate wall-clock due times after the trio, when auxiliary work
                            // can actually start, so a 5s voltage deadline rounds to ~6s, not 9s.
                            val auxiliary = scheduler.commands(supported).drop(PollTrialPlan.critical.size)
                            commands.addAll(auxiliary)
                            requested.addAll(auxiliary.filter { it >= 0 })
                            auxiliarySelected = true
                        }
                        if (commandIndex == commands.size) break
                        val pid = commands[commandIndex]
                        quiesceRequest()?.let {
                            partial(values, missing + commands.drop(commandIndex).filter { it >= 0 }, decodedFields)
                            return RecordingExit.Quiesce(it)
                        }
                        commandIndex += 1
                        val optional = pid !in PollTrialPlan.critical
                        if (!scheduler.mayRunCommand(pid, started, reserved)) {
                            if (pid >= 0) missing += pid
                            continue
                        }
                        // Repeated/other-PID cooldowns keep their elapsed durations; first malformed MAF retries next row.
                        if (pid >= 0 && !malformed.shouldPoll(pid, wallCycle, opportunity = sequence)) {
                            timing.cooldownSkip(pid)
                            missing += pid
                            if (!optional) criticalOutcomes[pid] = CriticalPollOutcome.COOLDOWN
                            if (optional) scheduler.attempted(pid)
                            continue
                        }
                        if (optional) scheduler.attempted(pid)
                        val commandStarted = SystemClock.elapsedRealtime()
                        try {
                            if (pid == PollTrialScheduler.VOLTAGE) {
                                elm.readVoltage()?.let {
                                    values["adapter_voltage"] = it
                                    lastVoltage = it
                                    lastVoltageAt = SystemClock.elapsedRealtime()
                                }
                            } else {
                                when (val result = timedLivePidPoll(pid, timing) {
                                    if (optional) elm.queryOptional(it) else elm.query(it)
                                }) {
                                    LivePidPollResult.Missing -> {
                                        malformed.recordValid(pid)
                                        missing += pid
                                        if (!optional) criticalOutcomes[pid] = CriticalPollOutcome.MISSING
                                    }
                                    is LivePidPollResult.Values -> {
                                        malformed.recordValid(pid)
                                        values.putAll(result.decoded)
                                        decodedFields[pid] = result.decoded.keys.toSet()
                                        if (!optional) criticalOutcomes[pid] = CriticalPollOutcome.SUCCESS
                                        if (pid == 0x0D) (result.decoded["vehicle_speed"] as? Number)?.toDouble()?.let {
                                            motionJournal?.speed(it)
                                        }
                                    }
                                    is LivePidPollResult.Malformed -> {
                                        missing += pid
                                        if (!optional) criticalOutcomes[pid] = CriticalPollOutcome.MALFORMED
                                        malformed.recordMalformed(pid, wallCycle, opportunity = sequence)
                                        recordParserFailure(result.error)
                                        database.incrementError(driveId)
                                        database.addDiagnostic(driveId, "parser_failure", JSONObject()
                                            .put("category", "live_pid_%02X".format(pid))
                                            .put("message", safeError(result.error)), driveClock.nowUtc())
                                    }
                                }
                            }
                        } catch (error: ElmCommandTimeoutException) {
                            if (!optional || !error.promptRecovered) throw error
                            missing += pid
                            malformed.recordMalformed(pid, wallCycle)
                            recordRecoveredOptionalTimeout(driveId, pid, driveClock)
                        } finally {
                            if (pid == PollTrialScheduler.VOLTAGE) {
                                timing.observeStage(PollCycleStage.AUXILIARY_REQUESTS, commandStarted)
                            }
                            scheduler.observeCommand(boundedElapsedMillis(commandStarted))
                        }
                    }
                } catch (cancelled: CancellationException) {
                    runCatching { partial(values, missing + requested.filter { it !in timing.successfulRowPids() }, decodedFields) }
                    throw cancelled
                } catch (quiesce: ElmQuiesceRequestedException) {
                    partial(values, missing + requested.filter { it !in timing.successfulRowPids() }, decodedFields)
                    quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                    throw quiesce
                } catch (error: Exception) {
                    partial(values, missing + requested.filter { it !in timing.successfulRowPids() }, decodedFields)
                    if (PollTrialPlan.transportFault(error, elm.isSessionTainted)) fallback("transport_fault", error)
                    throw error
                }
                val tailStarted = SystemClock.elapsedRealtime()
                values.putAll(staticValues)
                values.putAll(ElmProtocol.estimates(values))
                val capture = driveClock.capture()
                val prepared = prepareTimedTrialRow(values, decodedFields, timing.takeRowAges(capture.elapsedMillis), missing)
                val ages = prepared.ages
                val retainedOutcomes = criticalOutcomes.mapValues { (pid, outcome) ->
                    if (outcome == CriticalPollOutcome.SUCCESS && pid !in ages) CriticalPollOutcome.UNPROVED else outcome
                }
                val persisted = timing.measureStage(PollCycleStage.PERSISTENCE) { persistSample(SampleRecord(
                    driveId, sequence, capture.timestampUtc, prepared.values,
                    parserQuality = if (prepared.missing.isEmpty()) "ok" else "partial",
                    missingPids = prepared.missing, pidResponseAgeMs = ages,
                )) }
                if (persisted) timing.rowPersisted(ages)
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                // Read-only fault probes must fit the measured command + full post-primary
                // tail reserve. A deferred queue retries after 15s, leaving auxiliary slots
                // available between reservations; an actual probe keeps the 65s cadence.
                var diagnosticMillis = 0L
                val diagnosticAllowed = reserved && persisted && scheduler.mayRun(started)
                if (reserved) diagnosticSchedule.observedReservation(SystemClock.elapsedRealtime(), diagnosticAllowed)
                if (diagnosticAllowed) {
                    val diagnosticStarted = timing.now()
                    try {
                        diagnosticScan.runOne(wallCycle, capture.timestampUtc)
                    } finally {
                        timing.observeStage(PollCycleStage.DIAGNOSTICS, diagnosticStarted)
                        diagnosticMillis = boundedElapsedMillis(diagnosticStarted)
                        scheduler.observeCommand(diagnosticMillis)
                    }
                    if (elm.isSessionTainted || !elm.isConnected) fallback("transport_fault")
                }
                quiesceRequest()?.let { return RecordingExit.Quiesce(it) }
                val running = lifecycle.remainsRecording(SystemClock.elapsedRealtime(),
                    prepared.values["adapter_voltage"] as? Double, prepared.values["engine_rpm"] as? Double)
                if (!running) {
                    timing.finishCycle(completed = persisted)
                    return RecordingExit.EngineStopped
                }
                // Fresh low battery voltage plus absent/stopped RPM is shutdown evidence, not
                // a trial failure during the existing engine-off grace period.
                val shuttingDown = PollTrialPlan.shuttingDown(SystemClock.elapsedRealtime(), lastVoltageAt,
                    lastVoltage, config.voltageOff, (prepared.values["engine_rpm"] as? Number)?.toDouble())
                timing.measureStage(PollCycleStage.STATUS_PUBLISH) { publish("ecu_online", config) }
                timing.finishCycle(completed = persisted)
                emitPollTiming(driveId, timing, "periodic", driveClock)
                // Include quiesce, status and timing writes, rather than learning only
                // the sample's SQLite write. The separately timed fault command is not tail.
                scheduler.observeTail((boundedElapsedMillis(tailStarted) - diagnosticMillis).coerceAtLeast(0))
                // Include synchronous status and diagnostic disk work in the fallback budget.
                if (shuttingDown) health.reset() else health.completed(
                    boundedElapsedMillis(started), retainedOutcomes, SystemClock.elapsedRealtime(),
                )?.let { fallback(it) }
                sequence += 1
                awaitLivePollDeadline(started, SystemClock::elapsedRealtime,
                    { !scope.isActive || quiesceRequest() != null }, PollTrialPlan.TARGET_MILLIS)
            }
            throw CancellationException("logger scope stopped")
        } catch (error: Exception) {
            flushReason = "partial_failure"
            throw error
        } finally {
            timing.finishCycle()
            emitPollTiming(driveId, timing, flushReason, driveClock)
        }
    }

    private fun emitPollTiming(
        driveId: String,
        timing: PollTiming,
        reason: String,
        driveClock: MonotonicUtcClock,
    ) {
        // Diagnostics must never replace a transport/cancellation error or stop collection.
        runCatching {
            timing.drain(reason)?.let { payload ->
                database.addDiagnostic(driveId, "poll_timing", payload, driveClock.nowUtc())
            }
        }
    }

    private fun persistPartialSample(
        driveId: String,
        sequence: Long,
        observedValues: Map<String, Any>,
        missingPids: List<Int>,
        timestampUtc: String,
        pollTiming: PollTiming,
    ) {
        if (observedValues.isEmpty()) return
        val values = LinkedHashMap(observedValues)
        values.putAll(ElmProtocol.estimates(values))
        val rowAges = pollTiming.takeRowAges()
        partialSampleAfterTransportFailure(
            driveId = driveId,
            sequence = sequence,
            timestampUtc = timestampUtc,
            values = values,
            missingPids = missingPids,
        )?.let { sample ->
            if (pollTiming.measureStage(PollCycleStage.PERSISTENCE) { persistSample(sample) }) pollTiming.rowPersisted(rowAges)
        }
    }

    private fun persistSample(sample: SampleRecord): Boolean {
        pipelineMetrics.sampleCreated()
        pipelineMetrics.sampleQueued()
        return try {
            val inserted = database.addSample(sample)
            if (inserted) {
                pipelineMetrics.samplePersisted()
                lastSampleAt = sample.timestampUtc
                if (sample.sequence == 0L) {
                    val metrics = linkedMapOf<String, Number>("sample_count" to 1)
                    metrics.putAll(
                        firstSampleTimingMetrics(
                            connectionStartedElapsedMillis,
                            driveStartedElapsedMillis,
                            SystemClock.elapsedRealtime(),
                        ),
                    )
                    emitEvent(
                        "obd.poll_health",
                        "info",
                        "succeeded",
                        "first_sample_persisted",
                        sample.driveId,
                        metrics,
                    )
                    connectionStartedElapsedMillis = null
                }
            } else {
                pipelineMetrics.sampleDropped()
            }
            inserted
        } catch (error: Exception) {
            pipelineMetrics.databaseWriteFailed()
            throw error
        }
    }

    private fun currentQuiesceRequest(deviceRoot: File): IngestionRequestRead? =
        readIngestionRequest(deviceRoot, cleanupAbsentMarkers = false).takeUnless {
            it is IngestionRequestRead.Absent
        }

    private fun readIngestionRequest(deviceRoot: File, cleanupAbsentMarkers: Boolean = true): IngestionRequestRead =
        IngestionQuiesceFiles.readRequest(deviceRoot, cleanupAbsentMarkers = cleanupAbsentMarkers).also { request ->
            sleepWindowController?.setIngestionRequestActive(request is IngestionRequestRead.Valid)
            observeIngestionRequest(request)
        }

    private fun recordParserFailure(error: ElmProtocolException) {
        pipelineMetrics.parserFailure(
            checksumFailure = error.message?.contains("checksum", ignoreCase = true) == true,
        )
    }

    private fun recordRecoveredOptionalTimeout(driveId: String, pid: Int, driveClock: MonotonicUtcClock) {
        database.incrementError(driveId)
        database.addDiagnostic(
            driveId, "connection_failure", JSONObject()
                .put("category", "optional_pid_%02X_timeout_recovered".format(pid))
                .put("message", "optional PID timed out; adapter prompt resynchronised; reading omitted"),
            driveClock.nowUtc(),
        )
    }

    private fun finalizeActiveDrive(
        driveId: String,
        stopReason: String,
        lastSuccessfulResponseAtUtc: String?,
        noticedAtUtc: String,
        finalisedAtUtc: () -> String,
    ): DriveFinalization {
        val noticedAt = Instant.parse(noticedAtUtc).toString()
        database.addDiagnostic(
            driveId,
            "pipeline_metrics",
            pipelineMetrics.snapshot().toJson(),
            noticedAt,
        )
        database.markFinalising(
            driveId,
            stopReason,
            noticedAt,
            lastSuccessfulResponseAtUtc,
        )
        val finalised = database.finalizeDrive(
            driveId = driveId,
            stopReason = stopReason,
            noticedAtUtc = noticedAt,
            requestedFinishAtUtc = if (stopReason == "engine_stopped") noticedAt else null,
            lastSuccessfulResponseAtUtc = lastSuccessfulResponseAtUtc,
            finalisedAtUtc = finalisedAtUtc(),
        )
        currentDriveId = null
        currentPollPlanVersion = ObdPollPlan.VERSION
        lastDriveId = driveId
        lastDriveFinished = finalised.finishTimeUtc
        lastSampleAt = finalised.lastSampleAtUtc
        val eventMetrics = linkedMapOf<String, Number>("sample_count" to finalised.sampleCount)
        driveStartedElapsedMillis?.let {
            eventMetrics["elapsed_ms"] = boundedElapsedMillis(it, 604_800_000L)
        }
        emitEvent(
            kind = "drive.lifecycle",
            level = if (finalised.status == "complete") "info" else "warning",
            outcome = when (finalised.status) {
                "complete" -> "completed"
                "recovered" -> "recovered"
                else -> "interrupted"
            },
            reasonCode = when (stopReason) {
                "engine_stopped" -> "engine_stopped"
                "ingestion_requested" -> "ingestion_requested"
                "device_restart" -> "device_restart"
                "command_timeout" -> "poll_timeout"
                "connection_lost" -> "connection_lost"
                "process_terminated" -> "uncaught_restart"
                "database_fault" -> "database_fault"
                else -> "unknown"
            },
            driveId = driveId,
            metrics = eventMetrics,
        )
        driveStartedElapsedMillis = null
        return finalised
    }

    private fun exportTerminalDriveForIngestion(finalised: DriveFinalization): ExportedBundle? {
        if (finalised.sampleCount <= 0) {
            database.prepareCompletedExports()
            return null
        }
        return exportWithEvent(finalised.driveId)
    }

    private fun acknowledgementMetadata(
        finalised: DriveFinalization,
        exported: ExportedBundle?,
    ): IngestionAckMetadata = IngestionAckMetadata(
        driveId = finalised.driveId,
        lastSampleAtUtc = finalised.lastSampleAtUtc,
        bundleFilename = exported?.file?.name,
        bundleSha256 = exported?.sha256,
    )

    private fun prepareParkedForIngestion(
        deviceRoot: File,
        config: LoggerConfig,
        request: IngestionRequest,
    ) {
        if (!requestStillActive(deviceRoot, request)) return
        if (IngestionQuiesceFiles.isReadyFor(deviceRoot, request.requestId)) {
            startupRecoveredDrive = null
            emitAcknowledgementOnce(request.requestId, null)
            publish("ingestion_ready", config)
            updateNotification("OBD persisted; ready for ingestion")
            return
        }
        drainPendingExportsStrict()
        database.checkpointForIngestion()
        if (!requestStillActive(deviceRoot, request)) return
        val metadata = startupRecoveredDrive?.let { recoveredDrive ->
            val actualBundle = if (recoveredDrive.sampleCount > 0) {
                // Revalidate/recreate the local immutable file even when a prior server receipt
                // allowed retention to prune it. An ACK must never name a file that is absent.
                exportWithEvent(recoveredDrive.driveId)
            } else {
                null
            }
            acknowledgementMetadata(recoveredDrive, actualBundle)
        } ?: IngestionAckMetadata()
        IngestionQuiesceFiles.publishReady(deviceRoot, request, metadata)
        emitAcknowledgementOnce(request.requestId, metadata.driveId)
        startupRecoveredDrive = null
        publish("ingestion_ready", config)
        updateNotification("OBD persisted; ready for ingestion")
    }

    private suspend fun waitForNextParkedProbe(deviceRoot: File, durationMillis: Long) {
        awaitInterruptibleDelay(
            durationMillis = durationMillis,
            wakeSignals = retryWakeSignals,
            preempted = {
                !scope.isActive ||
                    readIngestionRequest(deviceRoot) !is IngestionRequestRead.Absent
            },
            monotonicMillis = SystemClock::elapsedRealtime,
        )
    }

    private fun clearRetryWakeSignals() {
        while (retryWakeSignals.tryReceive().isSuccess) {
            // Wake broadcasts observed during a proven live session must not zero a later retry.
        }
    }

    private fun requestStillActive(deviceRoot: File, request: IngestionRequest): Boolean =
        (readIngestionRequest(deviceRoot) as? IngestionRequestRead.Valid)
            ?.request?.requestId == request.requestId

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

        fun prepareCycle(cycle: Long): Boolean {
            if (steps.isEmpty && cycle >= nextScanAt) enqueueScan()
            return !steps.isEmpty
        }

        suspend fun runOne(cycle: Long, observedAtUtc: String): Boolean {
            prepareCycle(cycle)
            val step = steps.take(cycle) ?: return false
            try {
                step.operation(observedAtUtc)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                if (error is ElmProtocolException) recordParserFailure(error)
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
            return true
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
            recordRetentionPrune(exporter.enforceRetention())
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

    /** Unlike background draining, quiesce cannot acknowledge success after an export failure. */
    private fun drainPendingExportsStrict() {
        exporter.reconcileMissingExports()
        for (driveId in database.prepareCompletedExports()) exportWithEvent(driveId)
        runCatching { recordRetentionPrune(exporter.enforceRetention()) }
    }

    private fun exportSafely(driveId: String) {
        try {
            exportWithEvent(driveId)
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

    private fun exportWithEvent(driveId: String): ExportedBundle {
        emitEvent("bundle.export", "info", "started", "export_started", driveId)
        return try {
            exporter.export(driveId).also { exported ->
                emitEvent(
                    "bundle.export",
                    "info",
                    "completed",
                    "export_completed",
                    driveId,
                    mapOf("bundle_bytes" to exported.file.length().coerceAtMost(536_870_912L)),
                )
            }
        } catch (error: Exception) {
            emitEvent("bundle.export", "error", "failed", "export_failed", driveId)
            throw error
        }
    }

    private fun recordRetentionPrune(count: Int) {
        if (count <= 0) return
        emitEvent(
            "receipt.verification",
            "info",
            "pruned",
            "retention_pruned",
            metrics = mapOf("receipt_count" to count.coerceAtMost(1_000_000)),
        )
    }

    private fun publish(state: String, config: LoggerConfig, error: String? = null) {
        lastStatusState = state
        lastStatusError = error
        val voltageProbe = pipelineMetrics.voltageProbeSnapshot()
        val voltageState = voltagePublicState(
            voltageProbe,
            Instant.now(),
            VOLTAGE_FRESHNESS_SECONDS,
        )
        val ecuConnected = state == "ecu_online"
        val controlledVoltageOnly = config.voltageOnlyMode || BuildConfig.VOLTAGE_ONLY_AUDIT
        val sleepWindow = sleepWindowController?.snapshot() ?: SleepWindowEvidence()
        val bleOwner = when {
            !config.ownershipTransferred -> "unowned"
            state == "ingestion_ready" -> "unowned"
            state.startsWith("ingestion_") -> "transitioning"
            controlledVoltageOnly -> "dashcam_voltage_only"
            else -> "dashcam_full_obd"
        }
        val status = PublicStatus(
            state = state,
            ownershipEnabled = config.ownershipTransferred,
            currentDriveId = currentDriveId,
            pollPlanVersion = currentPollPlanVersion,
            trialSettings = LoggerPreferences.pollTrialSnapshot(this).json(),
            restartEvidence = bootDiagnostics?.snapshot(),
            temperatureCapture = temperatureCapture?.snapshot()?.json(),
            lastDriveId = lastDriveId,
            lastDriveFinishedAtUtc = lastDriveFinished,
            ingestionRequestId = ingestionRequestId,
            lastSampleAtUtc = lastSampleAt,
            // The signature excludes rolling metrics. Sort distributions only when a
            // transition/heartbeat really requires publication, not on every live row.
            adapterReachable = voltageState.adapterReachable,
            adapterConnected = client?.isConnected == true,
            ecuConnected = ecuConnected,
            engineRunning = ecuConnected && currentDriveId != null,
            vehicleState = if (ecuConnected) "ecu_online" else "parked",
            batteryVoltage = voltageProbe?.parsedVoltage,
            batteryVoltageSource = voltageProbe?.parsedVoltage?.let { "dashcam_elm_atrv" },
            batteryVoltageSampleAtUtc = voltageProbe?.sampleAtUtc,
            batteryVoltageFresh = voltageState.fresh,
            batteryVoltageRawResponse = voltageProbe?.sanitizedRawResponse,
            batteryVoltageQuality = voltageState.quality,
            bleOwner = bleOwner,
            voltageOnlyMode = controlledVoltageOnly,
            wifiConnected = sleepWindow.wifiConnected,
            accStateKnown = sleepWindow.accStateKnown,
            accOn = sleepWindow.accOn,
            ingestionSleepHoldKnown = sleepWindow.ingestionStateKnown,
            ingestionSleepHold = sleepWindow.ingestionRequestActive,
            sleepWindowPolicy = sleepWindow.policy,
            sleepWindowTargetSeconds = sleepWindow.targetSeconds,
            sleepWindowObservedSeconds = sleepWindow.observedSeconds,
            sleepWindowVerified = sleepWindow.verified,
            sleepWindowError = sleepWindow.error,
            sleepDeadlineEvidence = sleepWindow.deadline,
            movementState = clipIndexer?.state ?: "starting",
            movementClipCount = clipIndexer?.clipCount ?: 0,
            movementGpsState = passiveMotion?.state ?: "disabled",
            transferPolicyEnabled = clipIndexer?.transferPolicy?.enabled ?: false,
            transferPolicyState = clipIndexer?.transferPolicy?.state ?: "disabled",
            transferPolicySkipCount = clipIndexer?.transferPolicy?.skipCount ?: 0,
            transferPolicyUploadCount = clipIndexer?.transferPolicy?.uploadCount ?: 0,
            transferPolicyDeferCount = clipIndexer?.transferPolicy?.deferCount ?: 0,
            lastError = error,
            lastErrorAtUtc = error?.let { Instant.now().toString() },
        )
        scheduleEventProjectionRetry()
        val signature = durableStatusSignature(status)
        if (!statusWriteGate.shouldWrite(
                signature,
                SystemClock.elapsedRealtime(),
                heartbeatOverrideMillis = status.sleepDeadlineEvidence?.windowSeconds
                    ?.let { DEADLINE_STATUS_HEARTBEAT_MS },
            )) return
        runCatching {
            StatusPublisher.publish(this, status.copy(metrics = pipelineMetrics.snapshot()))
        }.onFailure {
            statusWriteGate.writeFailed(signature)
        }
    }

    /**
     * Signal a fail-soft projection retry without putting TF-card IO on the OBD poll worker.
     * Persistent failures remain single-flight and rate-limited; the private ring stays durable.
     */
    private fun scheduleEventProjectionRetry() {
        if (
            !::eventJournal.isInitialized ||
            !eventJournal.hasPendingProjection() ||
            !eventProjectionRetryQueued.compareAndSet(false, true)
        ) {
            return
        }
        eventScope.launch {
            try {
                val nowMillis = SystemClock.elapsedRealtime()
                if (lastEventProjectionRetryAtMillis > 0L) {
                    val elapsedMillis =
                        (nowMillis - lastEventProjectionRetryAtMillis).coerceAtLeast(0L)
                    delay((EVENT_PROJECTION_RETRY_INTERVAL_MILLIS - elapsedMillis).coerceAtLeast(0L))
                }
                if (eventJournal.hasPendingProjection()) eventJournal.publishSnapshot()
                lastEventProjectionRetryAtMillis = SystemClock.elapsedRealtime()
            } finally {
                eventProjectionRetryQueued.set(false)
            }
        }
    }

    private fun emitEvent(
        kind: String,
        level: String,
        outcome: String,
        reasonCode: String? = null,
        driveId: String? = null,
        metrics: Map<String, Number> = emptyMap(),
    ) {
        if (!::eventEmitter.isInitialized) return
        eventEmitter.emit(
            kind = kind,
            level = level,
            outcome = outcome,
            reasonCode = reasonCode,
            driveId = driveId,
            metrics = metrics,
        )
    }

    private fun observeIngestionRequest(request: IngestionRequestRead) {
        when (request) {
            is IngestionRequestRead.Valid -> if (
                !ingestionEventStateKnown || lastIngestionEventRequestId != request.request.requestId
            ) {
                ingestionEventStateKnown = true
                lastIngestionEventRequestId = request.request.requestId
                emitEvent("ingest.handoff", "info", "requested", "request_observed")
            }
            IngestionRequestRead.Absent -> {
                if (ingestionEventStateKnown && lastIngestionEventRequestId != null) {
                    emitEvent("ingest.handoff", "info", "resumed", "resume_observed")
                }
                ingestionEventStateKnown = true
                lastIngestionEventRequestId = null
                lastAcknowledgedEventRequestId = null
            }
            is IngestionRequestRead.Invalid -> Unit
        }
    }

    private fun emitAcknowledgementOnce(requestId: String, driveId: String?) {
        if (lastAcknowledgedEventRequestId == requestId) return
        lastAcknowledgedEventRequestId = requestId
        emitEvent(
            "ingest.handoff",
            "info",
            "acknowledged",
            "quiesce_acknowledged",
            driveId,
        )
    }

    private fun recordSleepWindowObservation(value: SleepWindowControllerObservation) {
        when (value) {
            is SleepWindowControllerObservation.AccRead -> motionJournal?.acc(value.on)
            SleepWindowControllerObservation.DeadlineUpdated -> scheduleSleepStatusPublish()
            is SleepWindowControllerObservation.Wifi -> emitEvent(
                kind = "network.wifi",
                level = "info",
                outcome = if (value.connected) "available" else "lost",
                reasonCode = if (value.connected) "wifi_available" else "wifi_lost",
            )
            is SleepWindowControllerObservation.Acc -> {
                if (value.on) retryWakeSignals.trySend(LoggerWakeReason.ACC_ON)
                emitEvent(
                    kind = "app.service",
                    level = "info",
                    outcome = "observed",
                    reasonCode = if (value.on) "acc_on" else "acc_off",
                )
                if (!value.on) {
                    dispatchIngestWebhook()
                }
            }
            is SleepWindowControllerObservation.Attempt -> {
                val evidence = value.evidence
                val reason = when (evidence.error) {
                    "sleep countdown update was refused" -> "property_refused"
                    "sleep countdown readback was unavailable" -> "readback_unavailable"
                    "sleep countdown readback did not match the requested value" ->
                        "readback_mismatch"
                    else -> when {
                        evidence.targetSeconds == null -> "ingestion_state_unknown"
                        evidence.ingestionRequestActive -> "backup_active"
                        evidence.wifiConnected -> "wifi_connected"
                        else -> "wifi_disconnected"
                    }
                }
                val metrics = linkedMapOf<String, Number>("attempt" to value.attempt)
                evidence.targetSeconds?.let { metrics["sleep_target_s"] = it }
                evidence.observedSeconds?.let { metrics["sleep_observed_s"] = it }
                value.retryDelayMillis?.let { metrics["retry_delay_ms"] = it }
                emitEvent(
                    kind = "power.sleep_window",
                    level = if (evidence.error == null) "info" else "warning",
                    outcome = when {
                        evidence.error != null && value.retryDelayMillis != null -> "retrying"
                        evidence.error != null -> "failed"
                        evidence.verified -> "verified"
                        else -> "skipped"
                    },
                    reasonCode = reason,
                    metrics = metrics,
                )
                scheduleSleepStatusPublish()
            }
        }
    }

    private fun scheduleSleepStatusPublish() {
        if (sleepStatusPublishJob?.isActive == true || !startupAllowed) return
        sleepStatusPublishJob = scope.launch {
            delay(250)
            val config = runCatching { LoggerPreferences.load(this@ObdLoggerService) }.getOrNull()
                ?: return@launch
            publish(lastStatusState, config, lastStatusError)
        }
    }

    private fun boundedElapsedMillis(startedAt: Long, maximum: Long = 3_600_000L): Long =
        (SystemClock.elapsedRealtime() - startedAt).coerceIn(0L, maximum)

    private fun safeError(error: Exception): String {
        return boundedRedactedError(error.message, fallback = error.javaClass.simpleName)
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

    private var lastWebhookDispatchAtMillis = 0L

    private fun dispatchIngestWebhook() {
        val config = LoggerPreferences.load(this)
        if (!config.webhookEnabled) return
        val configurationError = webhookConfigurationError(config)
        if (configurationError != null) {
            emitEvent("ingest.webhook", "warning", "failed", configurationError)
            return
        }
        val now = SystemClock.elapsedRealtime()
        if (now - lastWebhookDispatchAtMillis < 30_000L) {
            return
        }
        lastWebhookDispatchAtMillis = now
        scope.launch {
            // The unit's radios are the first thing the server quiets once it sees the car,
            // and the hotspot that carries this request may still be settling right after
            // ignition-off. A transient failure is retried a few times with short backoff;
            // anything the server actively rejected is not, because it will not change.
            var attempt = 0
            var result = sendWebhookPost(
                url = config.webhookUrl,
                apiKey = config.webhookApiKey,
                vehicleId = config.vehicleId,
            )
            while (!result.success && webhookRetryDelayMillis(result, attempt) != null) {
                val wait = webhookRetryDelayMillis(result, attempt) ?: break
                emitEvent(
                    kind = "ingest.webhook",
                    level = "info",
                    outcome = "retrying",
                    reasonCode = result.reasonCode,
                )
                delay(wait)
                attempt += 1
                result = sendWebhookPost(
                    url = config.webhookUrl,
                    apiKey = config.webhookApiKey,
                    vehicleId = config.vehicleId,
                )
            }
            emitEvent(
                kind = "ingest.webhook",
                level = if (result.success) "info" else "warning",
                outcome = if (result.success) "dispatched" else "failed",
                reasonCode = result.reasonCode,
            )
        }
    }

    private fun sendWebhookPost(url: String, apiKey: String, vehicleId: String): WebhookResult {
        var connection: java.net.HttpURLConnection? = null
        return try {
            val endpoint = java.net.URL(url.trim())
            connection = endpoint.openConnection() as java.net.HttpURLConnection
            configureWebhookConnection(connection, apiKey)
            val payload = JSONObject()
                .put("trigger", "obd_app_ignition_off")
                .put("vehicle_id", vehicleId)
                .put("timestamp", Instant.now().toString())
                .toString()
            connection.outputStream.use { os ->
                os.write(payload.toByteArray(Charsets.UTF_8))
                os.flush()
            }
            classifyWebhookResponse(connection.responseCode)
        } catch (_: java.net.SocketTimeoutException) {
            WebhookResult(false, "webhook_timeout")
        } catch (_: java.io.IOException) {
            WebhookResult(false, "webhook_network_error")
        } catch (_: SecurityException) {
            WebhookResult(false, "webhook_security_error")
        } catch (_: IllegalArgumentException) {
            WebhookResult(false, "invalid_webhook_url")
        } catch (_: Exception) {
            WebhookResult(false, "webhook_failed")
        } finally {
            connection?.disconnect()
        }
    }

    companion object {
        private const val EVENT_PROJECTION_RETRY_INTERVAL_MILLIS = 30_000L
        private const val CHANNEL = "obd_logger"
        private const val NOTIFICATION_ID = 1107
        private const val VOLTAGE_FRESHNESS_SECONDS = 75L
        const val ACTION_RELOAD_CONFIGURATION =
            "com.dashcamstats.obdlogger.action.RELOAD_CONFIGURATION"
    }
}

internal fun configureWebhookConnection(connection: java.net.HttpURLConnection, apiKey: String) {
    connection.requestMethod = "POST"
    connection.connectTimeout = 5000
    connection.readTimeout = 5000
    connection.instanceFollowRedirects = false
    connection.doOutput = true
    connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
    if (apiKey.isNotBlank()) {
        connection.setRequestProperty("X-API-Key", apiKey.trim())
    }
}

internal data class WebhookResult(val success: Boolean, val reasonCode: String)

/** Reason codes worth another attempt: the network, not the server's verdict, said no. */
internal val WEBHOOK_TRANSIENT_REASONS = setOf(
    "webhook_timeout",
    "webhook_network_error",
    "webhook_server_error",
)

/** Backoff between webhook attempts; the list length bounds the retries. */
internal val WEBHOOK_RETRY_DELAYS_MILLIS = listOf(3_000L, 10_000L, 20_000L)

/**
 * How long to wait before retrying after [result] on zero-based [attempt], or null when the
 * failure is final or the retry budget is spent.
 */
internal fun webhookRetryDelayMillis(result: WebhookResult, attempt: Int): Long? {
    if (result.success) return null
    if (result.reasonCode !in WEBHOOK_TRANSIENT_REASONS) return null
    return WEBHOOK_RETRY_DELAYS_MILLIS.getOrNull(attempt)
}

internal fun classifyWebhookResponse(code: Int): WebhookResult = when (code) {
    in 200..299 -> WebhookResult(true, "webhook_ok")
    401, 403 -> WebhookResult(false, "webhook_auth_rejected")
    in 400..499 -> WebhookResult(false, "webhook_client_error")
    in 500..599 -> WebhookResult(false, "webhook_server_error")
    else -> WebhookResult(false, "webhook_unexpected_response")
}
