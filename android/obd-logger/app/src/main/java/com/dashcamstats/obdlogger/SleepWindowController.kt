package com.dashcamstats.obdlogger

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.TimeUnit

internal const val ACTIVE_SLEEP_WINDOW_SECONDS = 900
internal const val IDLE_SLEEP_WINDOW_SECONDS = 300
private const val SLEEP_COUNTDOWN_PROPERTY = "persist.sys.sleep.countdown.time"
private const val PROPERTY_PROCESS_TIMEOUT_SECONDS = 3L
private val SLEEP_WINDOW_RETRY_DELAYS_MILLIS = listOf(1_000L, 5_000L, 15_000L)

internal enum class SleepWindowEvent {
    STARTED,
    WIFI_BECAME_PRESENT,
    WIFI_LOST,
    INGESTION_STARTED,
    INGESTION_ABSENCE_OBSERVED,
    INGESTION_ENDED,
}

internal data class SleepWindowCommand(
    val policy: String,
    val targetSeconds: Int?,
)

/**
 * Request removal while Wi-Fi remains up is deliberately server-owned. The backup controller
 * removes that request only after radio restoration, then writes its configured idle countdown.
 * Reasserting 900 here would race that successful cleanup and hold the unit awake unnecessarily.
 */
internal fun sleepWindowCommand(
    event: SleepWindowEvent,
    wifiConnected: Boolean,
    ingestionStateKnown: Boolean,
    ingestionRequestActive: Boolean,
): SleepWindowCommand = when (event) {
    SleepWindowEvent.STARTED -> if (wifiConnected || ingestionRequestActive) {
        SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS)
    } else if (!ingestionStateKnown) {
        SleepWindowCommand("awaiting_ingestion_state", null)
    } else {
        SleepWindowCommand("managed_idle", IDLE_SLEEP_WINDOW_SECONDS)
    }
    SleepWindowEvent.WIFI_BECAME_PRESENT,
    SleepWindowEvent.INGESTION_STARTED,
    -> SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS)
    SleepWindowEvent.WIFI_LOST -> if (ingestionRequestActive) {
        SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS)
    } else if (!ingestionStateKnown) {
        SleepWindowCommand("awaiting_ingestion_state", null)
    } else {
        SleepWindowCommand("managed_idle", IDLE_SLEEP_WINDOW_SECONDS)
    }
    SleepWindowEvent.INGESTION_ABSENCE_OBSERVED,
    SleepWindowEvent.INGESTION_ENDED -> if (wifiConnected) {
        SleepWindowCommand("server_owned", null)
    } else {
        SleepWindowCommand("managed_idle", IDLE_SLEEP_WINDOW_SECONDS)
    }
}

internal data class SleepWindowEvidence(
    val wifiConnected: Boolean = false,
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

/** Pure, synchronously testable policy application used only from the controller's IO worker. */
internal class SleepWindowReconciler(
    private val property: SleepWindowPropertyAccessor,
) {
    fun reconcile(
        wifiConnected: Boolean,
        ingestionStateKnown: Boolean,
        ingestionRequestActive: Boolean,
        command: SleepWindowCommand,
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
        waitBeforeRetry: suspend (Long) -> Boolean = {
            delay(it)
            true
        },
        publish: (SleepWindowEvidence) -> Unit = {},
    ): SleepWindowEvidence {
        var latest = SleepWindowEvidence(
            wifiConnected = wifiConnected,
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
            )
            currentCoroutineContext().ensureActive()

            val finished = command.targetSeconds == null || latest.verified
            val exhausted = attempt == retryDelaysMillis.size
            // Keep the established public error vocabulary. The backend accepts these exact
            // bounded strings, while the fixed write count proves that retries cannot spin.
            publish(latest)
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
    context: Context,
    private val scope: CoroutineScope,
    property: SleepWindowPropertyAccessor = AndroidSleepWindowPropertyAccessor(),
) {
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private val reconciler = SleepWindowReconciler(property)
    private val retryer = SleepWindowRetryer(reconciler)
    private val changes = Channel<SleepWindowEvent>(Channel.UNLIMITED)
    private val stateLock = Any()
    private var worker: Job? = null
    private var callbackRegistered = false
    private var currentDefaultNetwork: Network? = null
    private var wifiConnected = false
    private var ingestionStateKnown = false
    private var ingestionRequestActive = false

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
                    !wasWifiConnected && wifiConnected -> SleepWindowEvent.WIFI_BECAME_PRESENT
                    wasWifiConnected && !wifiConnected -> SleepWindowEvent.WIFI_LOST
                    else -> null
                }
            }
            event?.let(changes::trySend)
        }

        override fun onLost(network: Network) {
            val changed = synchronized(stateLock) {
                if (network != currentDefaultNetwork) {
                    false
                } else {
                    currentDefaultNetwork = null
                    wifiConnected = false
                    true
                }
            }
            if (changed) changes.trySend(SleepWindowEvent.WIFI_LOST)
        }
    }

    fun start() {
        if (worker != null) return
        worker = scope.launch {
            var pendingEvent: SleepWindowEvent? = null
            while (true) {
                val event = pendingEvent ?: changes.receiveCatching().getOrNull() ?: break
                pendingEvent = null
                val state = synchronized(stateLock) {
                    ControllerState(wifiConnected, ingestionStateKnown, ingestionRequestActive)
                }
                retryer.reconcile(
                    state.wifiConnected,
                    state.ingestionStateKnown,
                    state.ingestionRequestActive,
                    sleepWindowCommand(
                        event,
                        state.wifiConnected,
                        state.ingestionStateKnown,
                        state.ingestionRequestActive,
                    ),
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
                    publish = { evidence = it },
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
    }

    fun setIngestionRequestActive(active: Boolean) {
        val event = synchronized(stateLock) {
            val wasKnown = ingestionStateKnown
            val wasActive = ingestionRequestActive
            ingestionStateKnown = true
            when {
                !wasKnown -> {
                    ingestionRequestActive = active
                    if (active) {
                        SleepWindowEvent.INGESTION_STARTED
                    } else {
                        SleepWindowEvent.INGESTION_ABSENCE_OBSERVED
                    }
                }
                wasActive != active -> {
                    ingestionRequestActive = active
                    if (active) SleepWindowEvent.INGESTION_STARTED else SleepWindowEvent.INGESTION_ENDED
                }
                else -> null
            }
        }
        event?.let(changes::trySend)
    }

    private data class ControllerState(
        val wifiConnected: Boolean,
        val ingestionStateKnown: Boolean,
        val ingestionRequestActive: Boolean,
    )

    fun snapshot(): SleepWindowEvidence = evidence

    fun close() {
        if (callbackRegistered) {
            runCatching { connectivity.unregisterNetworkCallback(networkCallback) }
            callbackRegistered = false
        }
        changes.close()
        worker?.cancel()
        worker = null
    }
}
