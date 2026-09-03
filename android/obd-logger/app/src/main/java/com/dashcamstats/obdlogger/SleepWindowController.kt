package com.dashcamstats.obdlogger

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.provider.Settings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.TimeUnit

internal const val ACTIVE_SLEEP_WINDOW_SECONDS = 1200
internal const val IDLE_SLEEP_WINDOW_SECONDS = 300
private const val SLEEP_COUNTDOWN_PROPERTY = "persist.sys.sleep.countdown.time"
private const val ACC_STATUS_SETTING = "acc_status"
private const val PROPERTY_PROCESS_TIMEOUT_SECONDS = 3L
private const val ACC_POLL_INTERVAL_MILLIS = 5_000L
private val SLEEP_WINDOW_RETRY_DELAYS_MILLIS = listOf(1_000L, 5_000L, 15_000L)

internal enum class SleepWindowEvent {
    STARTED,
    WIFI_BECAME_PRESENT,
    WIFI_LOST,
    INGESTION_STARTED,
    INGESTION_ABSENCE_OBSERVED,
    INGESTION_ENDED,
    ACC_BECAME_ON,
    ACC_BECAME_OFF,
}

internal data class SleepWindowCommand(
    val policy: String,
    val targetSeconds: Int?,
)

/**
 * App-owned v2 waits through the initial Wi-Fi/request handshake, then narrows the sleep window
 * only after a previously active request disappears and ACC is definitely off. The server removes
 * the request after radio restoration, so that transition is the safe local completion boundary.
 */
internal fun sleepWindowCommand(
    event: SleepWindowEvent,
    wifiConnected: Boolean,
    ingestionStateKnown: Boolean,
    ingestionRequestActive: Boolean,
    ingestionCompleted: Boolean = false,
    accStateKnown: Boolean = false,
    accOn: Boolean = false,
    activeSeconds: Int = ACTIVE_SLEEP_WINDOW_SECONDS,
    idleSeconds: Int = IDLE_SLEEP_WINDOW_SECONDS,
): SleepWindowCommand = when (event) {
    SleepWindowEvent.STARTED -> if (wifiConnected || ingestionRequestActive) {
        SleepWindowCommand("managed_active", activeSeconds)
    } else if (!ingestionStateKnown) {
        SleepWindowCommand("awaiting_ingestion_state", null)
    } else {
        SleepWindowCommand("managed_idle", idleSeconds)
    }
    SleepWindowEvent.WIFI_BECAME_PRESENT,
    SleepWindowEvent.INGESTION_STARTED,
    -> SleepWindowCommand("managed_active", activeSeconds)
    SleepWindowEvent.WIFI_LOST -> if (ingestionRequestActive) {
        SleepWindowCommand("managed_active", activeSeconds)
    } else if (!ingestionStateKnown) {
        SleepWindowCommand("awaiting_ingestion_state", null)
    } else {
        SleepWindowCommand("managed_idle", idleSeconds)
    }
    SleepWindowEvent.INGESTION_ABSENCE_OBSERVED -> if (wifiConnected) {
        SleepWindowCommand("managed_active", activeSeconds)
    } else if (!ingestionStateKnown) {
        SleepWindowCommand("awaiting_ingestion_state", null)
    } else {
        SleepWindowCommand("managed_idle", idleSeconds)
    }
    SleepWindowEvent.INGESTION_ENDED -> when {
        !wifiConnected -> SleepWindowCommand("managed_idle", idleSeconds)
        accStateKnown && !accOn -> SleepWindowCommand("managed_idle", idleSeconds)
        else -> SleepWindowCommand("managed_active", activeSeconds)
    }
    SleepWindowEvent.ACC_BECAME_ON -> when {
        wifiConnected || ingestionRequestActive ->
            SleepWindowCommand("managed_active", activeSeconds)
        !ingestionStateKnown -> SleepWindowCommand("awaiting_ingestion_state", null)
        else -> SleepWindowCommand("managed_idle", idleSeconds)
    }
    SleepWindowEvent.ACC_BECAME_OFF -> when {
        ingestionRequestActive -> SleepWindowCommand("managed_active", activeSeconds)
        !wifiConnected -> if (ingestionStateKnown) {
            SleepWindowCommand("managed_idle", idleSeconds)
        } else {
            SleepWindowCommand("awaiting_ingestion_state", null)
        }
        ingestionCompleted -> SleepWindowCommand("managed_idle", idleSeconds)
        else -> SleepWindowCommand("managed_active", activeSeconds)
    }
}

internal data class SleepWindowEvidence(
    val wifiConnected: Boolean = false,
    val accStateKnown: Boolean = false,
    val accOn: Boolean = false,
    val ingestionStateKnown: Boolean = false,
    val ingestionRequestActive: Boolean = false,
    val policy: String = "uninitialized",
    val targetSeconds: Int? = null,
    val observedSeconds: Int? = null,
    val verified: Boolean = false,
    val error: String? = null,
)

internal interface SleepWindowPropertyAccessor {
    fun readSeconds(): Int?

    fun writeSeconds(seconds: Int): Boolean
}

internal interface AccStateAccessor {
    fun readAccOn(): Boolean?
}

internal sealed interface SleepWindowControllerObservation {
    data class Wifi(val connected: Boolean) : SleepWindowControllerObservation

    data class Acc(val on: Boolean) : SleepWindowControllerObservation

    data class Attempt(
        val evidence: SleepWindowEvidence,
        val attempt: Int,
        val retryDelayMillis: Long?,
    ) : SleepWindowControllerObservation
}

/**
 * One bounded system-property transaction. Failure is evidence, never a logger failure.
 *
 * This unit's ordinary ADB shell can write the vendor countdown because SELinux is permissive.
 * The production APK still verifies its own app-domain access on every change rather than assuming
 * that the same vendor policy applies to both UIDs.
 */
internal class AndroidSleepWindowPropertyAccessor : SleepWindowPropertyAccessor {
    override fun readSeconds(): Int? {
        val result = runProcess(listOf("/system/bin/getprop", SLEEP_COUNTDOWN_PROPERTY))
        if (result.exitCode != 0) return null
        return result.output.trim().toIntOrNull()?.takeIf { it in 1..3_600 }
    }

    override fun writeSeconds(seconds: Int): Boolean {
        require(seconds in 1..3_600)
        return runProcess(
            listOf("/system/bin/setprop", SLEEP_COUNTDOWN_PROPERTY, seconds.toString()),
        ).exitCode == 0
    }

    private fun runProcess(arguments: List<String>): ProcessResult = try {
        val process = ProcessBuilder(arguments)
            .redirectErrorStream(true)
            .start()
        if (!process.waitFor(PROPERTY_PROCESS_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            process.waitFor(PROPERTY_PROCESS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            ProcessResult(null, "")
        } else {
            // getprop/setprop output is tiny. Keep a hard bound anyway so vendor diagnostics can
            // never become status data or unbounded memory.
            ProcessResult(process.exitValue(), process.inputStream.bufferedReader().readText().take(64))
        }
    } catch (_: Exception) {
        ProcessResult(null, "")
    }

    private data class ProcessResult(val exitCode: Int?, val output: String)
}

/** Read-only vendor ACC evidence. Unknown output never becomes an off decision. */
internal class AndroidAccStateAccessor(private val context: Context) : AccStateAccessor {
    override fun readAccOn(): Boolean? = runCatching {
        Settings.Global.getString(context.contentResolver, ACC_STATUS_SETTING)
            ?.let(::parseAccState)
    }.getOrNull()
}

internal fun parseAccState(value: String): Boolean? = when (value.trim().lowercase()) {
    "1", "on", "true" -> true
    "0", "off", "false" -> false
    else -> null
}

/** Pure, synchronously testable policy application used only from the controller's IO worker. */
internal class SleepWindowReconciler(
    private val property: SleepWindowPropertyAccessor,
) {
    fun reconcile(
        wifiConnected: Boolean,
        ingestionStateKnown: Boolean,
        ingestionRequestActive: Boolean,
        command: SleepWindowCommand,
        accStateKnown: Boolean = false,
        accOn: Boolean = false,
    ): SleepWindowEvidence {
        val target = command.targetSeconds
        var observed = runCatching(property::readSeconds).getOrNull()
        var writeAccepted = true
        if (target != null && observed != target) {
            writeAccepted = runCatching { property.writeSeconds(target) }.getOrDefault(false)
            observed = runCatching(property::readSeconds).getOrNull()
        }
        val verified = target != null && observed == target
        val error = when {
            verified -> null
            target == null && observed != null -> null
            !writeAccepted -> "sleep countdown update was refused"
            observed == null -> "sleep countdown readback was unavailable"
            else -> "sleep countdown readback did not match the requested value"
        }
        return SleepWindowEvidence(
            wifiConnected = wifiConnected,
            accStateKnown = accStateKnown,
            accOn = accOn,
            ingestionStateKnown = ingestionStateKnown,
            ingestionRequestActive = ingestionRequestActive,
            policy = command.policy,
            targetSeconds = target,
            observedSeconds = observed,
            verified = verified,
            error = error,
        )
    }
}

/**
 * Retries only managed targets and stops after a small, fixed backoff schedule.
 *
 * Cancellation is checked immediately before and after each blocking property transaction. The
 * caller can also decline the next retry when a newer ordered state event arrives, without ever
 * running competing setprop processes.
 */
internal class SleepWindowRetryer(
    private val reconciler: SleepWindowReconciler,
    private val retryDelaysMillis: List<Long> = SLEEP_WINDOW_RETRY_DELAYS_MILLIS,
) {
    init {
        require(retryDelaysMillis.all { it >= 0L })
    }

    suspend fun reconcile(
        wifiConnected: Boolean,
        ingestionStateKnown: Boolean,
        ingestionRequestActive: Boolean,
        command: SleepWindowCommand,
        accStateKnown: Boolean = false,
        accOn: Boolean = false,
        waitBeforeRetry: suspend (Long) -> Boolean = {
            delay(it)
            true
        },
        publish: (SleepWindowEvidence, Int, Long?) -> Unit = { _, _, _ -> },
    ): SleepWindowEvidence {
        var latest = SleepWindowEvidence(
            wifiConnected = wifiConnected,
            accStateKnown = accStateKnown,
            accOn = accOn,
            ingestionStateKnown = ingestionStateKnown,
            ingestionRequestActive = ingestionRequestActive,
            policy = command.policy,
            targetSeconds = command.targetSeconds,
        )

        for (attempt in 0..retryDelaysMillis.size) {
            if (attempt > 0 && !waitBeforeRetry(retryDelaysMillis[attempt - 1])) return latest
            currentCoroutineContext().ensureActive()
            latest = reconciler.reconcile(
                wifiConnected,
                ingestionStateKnown,
                ingestionRequestActive,
                command,
                accStateKnown,
                accOn,
            )
            currentCoroutineContext().ensureActive()

            val finished = command.targetSeconds == null || latest.verified
            val exhausted = attempt == retryDelaysMillis.size
            // Keep the established public error vocabulary. The backend accepts these exact
            // bounded strings, while the fixed write count proves that retries cannot spin.
            publish(
                latest,
                attempt + 1,
                if (!finished && !exhausted) retryDelaysMillis[attempt] else null,
            )
            if (finished || exhausted) return latest
        }
        return latest
    }
}

/**
 * Keeps the vendor ignition-off countdown aligned without ever blocking BLE or SQLite work.
 *
 * Network callbacks and ingestion-state changes enqueue only real state transitions. They stay
 * ordered so the first known-absent ingestion observation cannot replace a preceding Wi-Fi arrival
 * before its 900-second write. A single IO coroutine performs every property transaction, so the
 * events still cannot launch competing setprop processes or delay the OBD command stream.
 */
internal class AdaptiveSleepWindowController(
    private val context: Context,
    private val scope: CoroutineScope,
    property: SleepWindowPropertyAccessor = AndroidSleepWindowPropertyAccessor(),
    private val accState: AccStateAccessor = AndroidAccStateAccessor(context),
    private val observation: (SleepWindowControllerObservation) -> Unit = {},
) {
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private val reconciler = SleepWindowReconciler(property)
    private val retryer = SleepWindowRetryer(reconciler)
    private val changes = Channel<SleepWindowEvent>(Channel.UNLIMITED)
    private val stateLock = Any()
    private var worker: Job? = null
    private var accPoller: Job? = null
    private var callbackRegistered = false
    private var currentDefaultNetwork: Network? = null
    private var wifiConnected = false
    private var accStateKnown = false
    private var accOn = false
    private var ingestionStateKnown = false
    private var ingestionRequestActive = false
    private var ingestionCompleted = false

    @Volatile
    private var evidence = SleepWindowEvidence()

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            synchronized(stateLock) {
                currentDefaultNetwork = network
            }
            // Android O+ follows this with ordered capabilities. Retain the previous safe state
            // until those capabilities arrive instead of briefly shortening the countdown.
        }

        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            val event = synchronized(stateLock) {
                val wasWifiConnected = wifiConnected
                currentDefaultNetwork = network
                wifiConnected = capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                when {
                    !wasWifiConnected && wifiConnected -> {
                        ingestionCompleted = false
                        SleepWindowEvent.WIFI_BECAME_PRESENT
                    }
                    wasWifiConnected && !wifiConnected -> {
                        ingestionCompleted = false
                        SleepWindowEvent.WIFI_LOST
                    }
                    else -> null
                }
            }
            event?.let {
                reportObservation(
                    SleepWindowControllerObservation.Wifi(
                        it == SleepWindowEvent.WIFI_BECAME_PRESENT,
                    ),
                )
                changes.trySend(it)
            }
        }

        override fun onLost(network: Network) {
            val changed = synchronized(stateLock) {
                if (network != currentDefaultNetwork) {
                    false
                } else {
                    currentDefaultNetwork = null
                    wifiConnected = false
                    ingestionCompleted = false
                    true
                }
            }
            if (changed) {
                reportObservation(SleepWindowControllerObservation.Wifi(false))
                changes.trySend(SleepWindowEvent.WIFI_LOST)
            }
        }
    }

    fun start() {
        if (worker != null) return
        worker = scope.launch {
            var pendingEvent: SleepWindowEvent? = null
            while (true) {
                val event = pendingEvent ?: changes.receiveCatching().getOrNull() ?: break
                pendingEvent = null
                if (
                    event == SleepWindowEvent.STARTED ||
                    event == SleepWindowEvent.WIFI_BECAME_PRESENT ||
                    event == SleepWindowEvent.INGESTION_ENDED
                ) {
                    refreshAccState()
                }
                val state = synchronized(stateLock) {
                    ControllerState(
                        wifiConnected,
                        accStateKnown,
                        accOn,
                        ingestionStateKnown,
                        ingestionRequestActive,
                        ingestionCompleted,
                    )
                }
                val config = LoggerPreferences.load(context)
                retryer.reconcile(
                    state.wifiConnected,
                    state.ingestionStateKnown,
                    state.ingestionRequestActive,
                    sleepWindowCommand(
                        event,
                        state.wifiConnected,
                        state.ingestionStateKnown,
                        state.ingestionRequestActive,
                        state.ingestionCompleted,
                        state.accStateKnown,
                        state.accOn,
                        activeSeconds = config.backupAwakeSeconds,
                        idleSeconds = config.idleAwakeSeconds,
                    ),
                    state.accStateKnown,
                    state.accOn,
                    waitBeforeRetry = { delayMillis ->
                        val next = withTimeoutOrNull(delayMillis) {
                            changes.receiveCatching()
                        }
                        when {
                            next == null -> true
                            next.isSuccess -> {
                                pendingEvent = next.getOrNull()
                                false
                            }
                            else -> false
                        }
                    },
                    publish = { nextEvidence, attempt, retryDelayMillis ->
                        evidence = nextEvidence
                        reportObservation(
                            SleepWindowControllerObservation.Attempt(
                                nextEvidence,
                                attempt,
                                retryDelayMillis,
                            ),
                        )
                    },
                )
            }
        }

        runCatching {
            val network = connectivity.activeNetwork
            val capabilities = network?.let(connectivity::getNetworkCapabilities)
            synchronized(stateLock) {
                currentDefaultNetwork = network
                wifiConnected = capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
            }
        }
        runCatching {
            connectivity.registerDefaultNetworkCallback(networkCallback)
            callbackRegistered = true
        }
        changes.trySend(SleepWindowEvent.STARTED)
        accPoller = scope.launch {
            while (true) {
                refreshAccState()
                delay(ACC_POLL_INTERVAL_MILLIS)
            }
        }
    }

    fun setIngestionRequestActive(active: Boolean) {
        val event = synchronized(stateLock) {
            val wasKnown = ingestionStateKnown
            val wasActive = ingestionRequestActive
            ingestionStateKnown = true
            when {
                !wasKnown -> {
                    ingestionRequestActive = active
                    if (active) ingestionCompleted = false
                    if (active) {
                        SleepWindowEvent.INGESTION_STARTED
                    } else {
                        SleepWindowEvent.INGESTION_ABSENCE_OBSERVED
                    }
                }
                wasActive != active -> {
                    ingestionRequestActive = active
                    ingestionCompleted = !active
                    if (active) SleepWindowEvent.INGESTION_STARTED else SleepWindowEvent.INGESTION_ENDED
                }
                else -> null
            }
        }
        event?.let(changes::trySend)
    }

    private data class ControllerState(
        val wifiConnected: Boolean,
        val accStateKnown: Boolean,
        val accOn: Boolean,
        val ingestionStateKnown: Boolean,
        val ingestionRequestActive: Boolean,
        val ingestionCompleted: Boolean,
    )

    private fun refreshAccState() {
        val observed = runCatching(accState::readAccOn).getOrNull() ?: return
        val event = synchronized(stateLock) {
            val changed = !accStateKnown || accOn != observed
            accStateKnown = true
            accOn = observed
            if (!changed) null else if (observed) {
                SleepWindowEvent.ACC_BECAME_ON
            } else {
                SleepWindowEvent.ACC_BECAME_OFF
            }
        }
        event?.let {
            reportObservation(
                SleepWindowControllerObservation.Acc(it == SleepWindowEvent.ACC_BECAME_ON),
            )
            changes.trySend(it)
        }
    }

    fun snapshot(): SleepWindowEvidence = evidence

    private fun reportObservation(value: SleepWindowControllerObservation) {
        runCatching { observation(value) }
    }

    fun close() {
        if (callbackRegistered) {
            runCatching { connectivity.unregisterNetworkCallback(networkCallback) }
            callbackRegistered = false
        }
        changes.close()
        accPoller?.cancel()
        accPoller = null
        worker?.cancel()
        worker = null
    }
}
