package com.dashcamstats.obdlogger

import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.coroutines.withTimeoutOrNull
import java.time.Instant

enum class ServiceStartupDecision { START, STOP_DISABLED, STOP_PERMISSION_REQUIRED }

enum class ServiceWorkerDecision { START, RESTART_FOR_CONFIGURATION, KEEP_RUNNING }

internal fun serviceWorkerDecision(
    workerActive: Boolean,
    configurationReloadRequested: Boolean,
): ServiceWorkerDecision = when {
    workerActive && configurationReloadRequested -> ServiceWorkerDecision.RESTART_FOR_CONFIGURATION
    workerActive -> ServiceWorkerDecision.KEEP_RUNNING
    else -> ServiceWorkerDecision.START
}

internal const val EXTRA_STARTUP_RECOVERY_REASON =
    "com.dashcamstats.obdlogger.extra.STARTUP_RECOVERY_REASON"

/** Only an actual BOOT_COMPLETED launch may classify a stale recording as a device restart. */
internal fun startupRecoveryReason(intentReason: String?): String =
    if (intentReason == "device_restart") "device_restart" else "process_terminated"

/**
 * Per-connection UTC anchor advanced only by Android's monotonic elapsed-realtime clock. Once a
 * drive starts, NTP/manual wall-clock changes therefore cannot make later samples regress.
 */
internal class MonotonicUtcClock(
    private val anchorUtc: Instant,
    private val anchorElapsedMillis: Long,
    private val elapsedRealtimeMillis: () -> Long,
) {
    private var greatestElapsedMillis = anchorElapsedMillis

    init {
        require(anchorElapsedMillis >= 0)
    }

    @Synchronized
    fun nowUtc(): String {
        greatestElapsedMillis = maxOf(greatestElapsedMillis, elapsedRealtimeMillis())
        val delta = (greatestElapsedMillis - anchorElapsedMillis).coerceAtLeast(0)
        return anchorUtc.plusMillis(delta).toString()
    }
}

data class DriveTerminalPolicy(
    val status: String,
    val cleanEnd: Boolean,
    val useLastValidSampleAsEnd: Boolean,
)

/** One mapping shared by live finalisation, restart recovery, bundles and JVM tests. */
internal fun driveTerminalPolicy(stopReason: String): DriveTerminalPolicy = when (stopReason) {
    "engine_stopped" -> DriveTerminalPolicy("complete", true, false)
    "device_restart" -> DriveTerminalPolicy("recovered", false, true)
    // Connection, ingestion, administrative and process/worker faults retain all evidence but
    // are never presented as a clean drive.
    else -> DriveTerminalPolicy("interrupted", false, true)
}

object ServiceStartupGate {
    fun decide(canRun: Boolean, hasPermissions: Boolean): ServiceStartupDecision = when {
        !hasPermissions -> ServiceStartupDecision.STOP_PERMISSION_REQUIRED
        !canRun -> ServiceStartupDecision.STOP_DISABLED
        else -> ServiceStartupDecision.START
    }
}

/**
 * Prevent database construction from drifting ahead of foreground promotion. Android gives a
 * service started with startForegroundService only a short deadline to call startForeground;
 * migration backup, WAL checkpointing and integrity checks must therefore remain behind this
 * explicit gate.
 */
internal class ForegroundFirstStartupGate {
    @Volatile
    private var foregroundStarted = false

    fun markForegroundStarted() {
        foregroundStarted = true
    }

    fun <T> afterForeground(block: () -> T): T {
        check(foregroundStarted) { "database initialization requires foreground promotion" }
        return block()
    }
}

class StatusWriteGate(private val heartbeatMillis: Long = 60_000) {
    private var lastSignature: String? = null
    private var lastWriteAt: Long? = null

    fun shouldWrite(signature: String, nowMillis: Long): Boolean {
        val previousAt = lastWriteAt
        if (
            signature == lastSignature && previousAt != null &&
            nowMillis - previousAt < heartbeatMillis
        ) {
            return false
        }
        lastSignature = signature
        lastWriteAt = nowMillis
        return true
    }

    fun writeFailed(signature: String) {
        if (signature == lastSignature) {
            lastSignature = null
            lastWriteAt = null
        }
    }
}

/** Normal parked probes are not reconnects; only a failed connection arms the next attempt. */
internal class ReconnectAttemptTracker {
    private var pending = false

    fun nextAttemptIsReconnect(): Boolean = pending.also { pending = false }

    fun connectionFailed() {
        pending = true
    }
}

internal enum class ParkedObservationBand { BELOW_START, ENGINE_CANDIDATE }

/** Thousands of identical parked probes collapse to one transition event. */
internal class ParkedObservationEventGate {
    private var previous: ParkedObservationBand? = null

    fun changed(next: ParkedObservationBand): Boolean {
        if (next == previous) return false
        previous = next
        return true
    }
}

internal fun bleConnectionFailureReason(error: Exception): String =
    if (error is ElmConnectionTimeoutException) "gatt_timeout" else "gatt_error"

internal fun firstSampleTimingMetrics(
    connectionStartedAtMillis: Long?,
    driveStartedAtMillis: Long?,
    observedAtMillis: Long,
): Map<String, Long> = buildMap {
    connectionStartedAtMillis?.let {
        put("first_sample_ms", (observedAtMillis - it).coerceIn(0L, 3_600_000L))
    }
    driveStartedAtMillis?.let {
        put("elapsed_ms", (observedAtMillis - it).coerceIn(0L, 3_600_000L))
    }
}

/** Bounded retry delay with jitter so two clients do not remain phase-locked after a conflict. */
internal class BoundedExponentialBackoff(
    private val baseDelayMillis: Long = 2_000,
    private val maximumDelayMillis: Long = 30_000,
    private val jitterFraction: Double = 0.2,
    private val randomUnit: () -> Double = { kotlin.random.Random.nextDouble() },
) {
    private var failures = 0

    init {
        require(baseDelayMillis in 1..maximumDelayMillis)
        require(jitterFraction in 0.0..0.5)
    }

    fun reset() {
        failures = 0
    }

    fun nextDelayMillis(): Long {
        failures = (failures + 1).coerceAtMost(31)
        var delay = baseDelayMillis
        repeat(failures - 1) {
            delay = (delay * 2).coerceAtMost(maximumDelayMillis)
        }
        val unit = randomUnit().coerceIn(0.0, 1.0)
        val factor = (1.0 - jitterFraction) + (2.0 * jitterFraction * unit)
        return (delay * factor).toLong().coerceIn(1L, maximumDelayMillis)
    }
}

/**
 * One service-lifetime retry policy whose escalation ends as soon as the adapter/ECU path proves
 * useful again. A live drive can span several reconnects, so waiting for runOneConnection() to
 * return before resetting would incorrectly carry old failures forward for the rest of the drive.
 */
internal class ConnectionRetryController(
    private val backoff: BoundedExponentialBackoff = BoundedExponentialBackoff(),
) {
    private var consecutiveFailures = 0

    fun failed(): ConnectionFailureDecision {
        consecutiveFailures = (consecutiveFailures + 1).coerceAtMost(1_000_000)
        return ConnectionFailureDecision(
            delayMillis = backoff.nextDelayMillis(),
            consecutiveFailures = consecutiveFailures,
        )
    }

    fun progressConfirmed() {
        backoff.reset()
        consecutiveFailures = 0
    }

    fun externalWakeObserved() = backoff.reset()
}

internal data class ConnectionFailureDecision(
    val delayMillis: Long,
    val consecutiveFailures: Int,
)

internal enum class LoggerWakeReason {
    BLUETOOTH_ON,
    SCREEN_ON,
    USER_PRESENT,
    POWER_CONNECTED,
    ACC_ON,
}

internal fun LoggerWakeReason.eventReasonCode(): String = when (this) {
    LoggerWakeReason.BLUETOOTH_ON -> "bluetooth_on"
    LoggerWakeReason.SCREEN_ON -> "screen_on"
    LoggerWakeReason.USER_PRESENT -> "user_present"
    LoggerWakeReason.POWER_CONNECTED -> "power_connected"
    LoggerWakeReason.ACC_ON -> "acc_on"
}

internal sealed interface InterruptibleWaitResult {
    data object Elapsed : InterruptibleWaitResult
    data object Preempted : InterruptibleWaitResult
    data class Woken(val reason: LoggerWakeReason) : InterruptibleWaitResult
}

/**
 * Wait without hiding a newly-created ingestion lease or a head-unit/radio wake transition behind
 * a retry delay. The monotonic clock keeps suspend time in the deadline on Android and makes the
 * policy deterministic in JVM tests.
 */
internal suspend fun awaitInterruptibleDelay(
    durationMillis: Long,
    wakeSignals: ReceiveChannel<LoggerWakeReason>,
    preempted: () -> Boolean,
    monotonicMillis: () -> Long,
    preemptionPollMillis: Long = 250,
): InterruptibleWaitResult {
    require(durationMillis >= 0)
    require(preemptionPollMillis >= 1)
    val startedAt = monotonicMillis()
    while (true) {
        if (preempted()) return InterruptibleWaitResult.Preempted
        val elapsed = (monotonicMillis() - startedAt).coerceAtLeast(0)
        val remaining = durationMillis - elapsed
        if (remaining <= 0) return InterruptibleWaitResult.Elapsed
        val waitMillis = minOf(preemptionPollMillis, remaining)
        val received = withTimeoutOrNull(waitMillis) {
            wakeSignals.receiveCatching()
        }
        if (received != null) {
            val wakeReason = received.getOrNull() ?: return InterruptibleWaitResult.Elapsed
            return InterruptibleWaitResult.Woken(wakeReason)
        }
    }
}

/** Null or low adapter voltage keeps the logger entirely outside the ECU protocol path. */
internal fun parkedVoltageWarrantsInitialization(voltage: Double?, voltageOn: Double): Boolean =
    voltage?.isFinite() == true && voltage >= voltageOn

/** Audit mode is an absolute fence: voltage can never authorise adapter reset or ECU probing. */
internal fun parkedProbeMayInitialize(
    voltageOnlyMode: Boolean,
    voltage: Double?,
    voltageOn: Double,
): Boolean = !voltageOnlyMode && parkedVoltageWarrantsInitialization(voltage, voltageOn)

internal data class VoltagePublicState(
    val fresh: Boolean,
    val quality: String,
    val adapterReachable: Boolean,
)

/** Convert one probe into public freshness semantics without extending its lifetime on restart. */
internal fun voltagePublicState(
    probe: VoltageProbeSnapshot?,
    nowUtc: Instant,
    freshnessSeconds: Long,
): VoltagePublicState {
    require(freshnessSeconds >= 1)
    val ageSeconds = probe?.let {
        runCatching { nowUtc.epochSecond - Instant.parse(it.sampleAtUtc).epochSecond }.getOrNull()
    }
    val fresh = probe?.result == "valid" &&
        ageSeconds != null && ageSeconds in -5..freshnessSeconds
    val quality = when {
        probe == null -> "unavailable"
        probe.result != "valid" -> probe.result
        fresh -> "valid"
        else -> "stale"
    }
    return VoltagePublicState(
        fresh = fresh,
        quality = quality,
        adapterReachable = probe?.result == "valid" && fresh,
    )
}

/**
 * Only fields whose change warrants an immediate durable status write belong in this signature.
 * Sample time and pipeline counters remain present in status.json, but are snapshots refreshed on
 * the bounded heartbeat instead of forcing an fsync and removable-storage rename every cycle.
 */
internal fun durableStatusSignature(status: PublicStatus): String = listOf(
    status.state,
    status.ownershipEnabled.toString(),
    status.adapterReachable.toString(),
    status.adapterConnected.toString(),
    status.ecuConnected.toString(),
    status.engineRunning.toString(),
    status.vehicleState,
    status.batteryVoltageSource.orEmpty(),
    status.batteryVoltageFresh.toString(),
    status.batteryVoltageQuality,
    status.bleOwner,
    status.headUnitState,
    status.voltageOnlyMode.toString(),
    status.wifiConnected.toString(),
    status.accStateKnown.toString(),
    status.accOn.toString(),
    status.ingestionSleepHoldKnown.toString(),
    status.ingestionSleepHold.toString(),
    status.sleepWindowPolicy,
    status.sleepWindowTargetSeconds?.toString().orEmpty(),
    status.sleepWindowObservedSeconds?.toString().orEmpty(),
    status.sleepWindowVerified.toString(),
    status.sleepWindowError.orEmpty(),
    status.currentDriveId.orEmpty(),
    status.lastDriveId.orEmpty(),
    status.lastDriveFinishedAtUtc.orEmpty(),
    status.ingestionRequestId.orEmpty(),
    status.lastError.orEmpty(),
).joinToString("\u0000")

class OneStepPerCycleQueue<T> {
    private val pending = ArrayDeque<T>()
    private var lastCycle: Long? = null

    val isEmpty: Boolean get() = pending.isEmpty()

    fun add(value: T) {
        pending.addLast(value)
    }

    fun take(cycle: Long): T? {
        if (lastCycle == cycle) return null
        lastCycle = cycle
        return pending.removeFirstOrNull()
    }
}

class DtcScanCompletionTracker {
    private val successfulModes = mutableSetOf<Int>()

    fun markSuccessful(mode: Int) {
        require(mode in REQUIRED_MODES)
        successfulModes += mode
    }

    val isComplete: Boolean get() = successfulModes == REQUIRED_MODES

    companion object {
        val REQUIRED_MODES: Set<Int> = setOf(0x03, 0x07, 0x0A)
    }
}

/** Classify sparse diagnostic failures without mistaking a BLE/ELM outage for JSON parsing. */
fun diagnosticFailureKind(error: Throwable): String = when (error) {
    is ElmProtocolException -> "parser_failure"
    else -> "connection_failure"
}

/** Preserve the direct-BLE status vocabulary in the exported diagnostic evidence. */
fun diagnosticProbeStatus(error: Throwable): String = when (error) {
    is ElmCommandRejectedException -> "rejected"
    is ElmProtocolException -> "malformed"
    else -> "transport_error"
}

class DiagnosticScanFailureTracker {
    private var failure: String? = null

    fun reset() {
        failure = null
    }

    fun failed(status: String) {
        failure = status
    }

    fun finalStatus(successStatus: String): String = failure ?: successStatus
}

internal sealed interface LivePidPollResult {
    data class Values(val decoded: Map<String, Any>) : LivePidPollResult
    data object Missing : LivePidPollResult
    data class Malformed(val error: ElmProtocolException) : LivePidPollResult
}

/**
 * A malformed, prompt-complete optional PID is a missing value, not a lost connection.
 * Deliberately catch only parser validation failures: BLE errors, ELM prompt timeouts and
 * cancellations must escape so the caller aborts the drive and reconnects.
 */
internal suspend fun pollLivePid(
    pid: Int,
    query: suspend (Int) -> Map<String, Any>,
): LivePidPollResult = try {
    query(pid).takeIf { it.isNotEmpty() }
        ?.let { LivePidPollResult.Values(it) }
        ?: LivePidPollResult.Missing
} catch (error: ElmProtocolException) {
    LivePidPollResult.Malformed(error)
}

data class MalformedPidDecision(
    val consecutiveFailures: Int,
    val retryAtCycle: Long,
)

/**
 * Per-PID circuit breaker. A transient malformed response is retried after a short cooldown;
 * repeated strict-parser failures back off exponentially to a bounded one-minute cadence.
 */
internal class LivePidMalformedTracker(
    private val baseCooldownCycles: Long = 2,
    private val maximumCooldownCycles: Long = 12,
) {
    private data class State(val failures: Int, val retryAtCycle: Long)
    private val states = mutableMapOf<Int, State>()

    init {
        require(baseCooldownCycles >= 1)
        require(maximumCooldownCycles >= baseCooldownCycles)
    }

    fun shouldPoll(pid: Int, cycle: Long): Boolean =
        states[pid]?.let { cycle >= it.retryAtCycle } ?: true

    fun recordMalformed(pid: Int, cycle: Long): MalformedPidDecision {
        val failures = ((states[pid]?.failures ?: 0) + 1).coerceAtMost(30)
        val shift = (failures - 1).coerceAtMost(20)
        val cooldown = (baseCooldownCycles shl shift).coerceAtMost(maximumCooldownCycles)
        val retryAt = if (cycle > Long.MAX_VALUE - cooldown) Long.MAX_VALUE else cycle + cooldown
        states[pid] = State(failures, retryAt)
        return MalformedPidDecision(failures, retryAt)
    }

    fun recordValid(pid: Int) {
        states.remove(pid)
    }
}

/** Build the one durable partial row allowed before a fatal live transport failure. */
internal fun partialSampleAfterTransportFailure(
    driveId: String,
    sequence: Long,
    timestampUtc: String,
    values: Map<String, Any>,
    missingPids: List<Int>,
): SampleRecord? = values.takeIf { it.isNotEmpty() }?.let {
    SampleRecord(
        driveId = driveId,
        sequence = sequence,
        timestampUtc = timestampUtc,
        values = LinkedHashMap(it),
        transportQuality = "failed_after_partial",
        parserQuality = "partial",
        missingPids = missingPids.distinct(),
    )
}

data class StorageVolumeState(val removable: Boolean, val mounted: Boolean)

object RemovableStoragePolicy {
    fun selectIndex(volumes: List<StorageVolumeState>): Int? =
        volumes.indexOfFirst { it.removable && it.mounted }.takeIf { it >= 0 }
}

object ObdPollPlan {
    const val VERSION = 3
    const val TARGET_CYCLE_MILLIS = 5_000L

    // ISO 9141 has to serialize every request.  Six fast PIDs plus the rotating work
    // consistently overran the five-second target on the real adapter, producing
    // artificial 8-second gaps in speed and RPM.  Keep the driving-critical values in
    // every cycle and distribute all other live values across the same three-cycle tier.
    private val fast = listOf(0x04, 0x0C, 0x0D)
    private val medium = listOf(0x0E, 0x10, 0x11, 0x03, 0x05, 0x06, 0x07, 0x0F, 0x14, 0x15)
    private val slow = listOf(0x13, 0x1C, 0x21)

    fun requestedPids(sequence: Long, supported: Set<Int>): List<Int> = buildList {
        addAll(fast)
        // Preserve the 3-cycle cadence for every non-critical live PID without letting one
        // cycle issue the whole schedule. The slow PIDs retain their 12-cycle cadence and use
        // separated phases.
        addAll(medium.filterIndexed { index, _ -> index % 3 == (sequence % 3).toInt() })
        addAll(slow.filterIndexed { index, _ -> index * 4 == (sequence % 12).toInt() })
    }.filter(supported::contains)

    fun tier(pid: Int): String? = when (pid) {
        in fast -> "fast"
        in medium -> "medium"
        in slow -> "slow"
        else -> null
    }

    fun expectedIntervalCycles(pid: Int): Int? = when (pid) {
        in fast -> 1
        in medium -> 3
        in slow -> 12
        else -> null
    }

    /**
     * Optional diagnostic work may only consume the command budget learned from this live ELM
     * session. This is deliberately not a fixed guess: ISO 9141 response time changes between
     * adapters and a slow command disables sparse work for the rest of the drive.
     */
    fun mayRunSparseDiagnostic(
        liveCycleElapsedMillis: Long,
        requiredCommandBudgetMillis: Long,
    ): Boolean =
        requiredCommandBudgetMillis in 1..TARGET_CYCLE_MILLIS &&
            liveCycleElapsedMillis <= TARGET_CYCLE_MILLIS - requiredCommandBudgetMillis
}

/**
 * Conservative, connection-local budget for one optional ELM command. Live and diagnostic
 * command timings feed the same high-water mark. A two-times multiplier plus a fixed margin
 * accounts for larger multi-frame diagnostic replies without cancelling an in-flight command.
 * Once a command is slow enough, optional work remains deferred until reconnect.
 */
internal class SparseDiagnosticBudgetTracker(
    private val minimumBudgetMillis: Long = 2_000L,
    private val maximumBudgetMillis: Long = 6_000L,
    private val multiplier: Long = 2L,
    private val marginMillis: Long = 250L,
) {
    private var observedWorstMillis = 0L

    init {
        require(minimumBudgetMillis >= 1)
        require(maximumBudgetMillis >= minimumBudgetMillis)
        require(multiplier >= 1)
        require(marginMillis >= 0)
    }

    fun observeCommand(durationMillis: Long) {
        observedWorstMillis = maxOf(observedWorstMillis, durationMillis.coerceAtLeast(1L))
    }

    fun requiredBudgetMillis(): Long {
        if (observedWorstMillis == 0L) return maximumBudgetMillis
        val scaled = if (observedWorstMillis > (Long.MAX_VALUE - marginMillis) / multiplier) {
            Long.MAX_VALUE
        } else {
            observedWorstMillis * multiplier + marginMillis
        }
        return scaled.coerceIn(minimumBudgetMillis, maximumBudgetMillis)
    }
}
