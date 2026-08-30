package com.dashcamstats.obdlogger

import java.time.Instant

enum class ServiceStartupDecision { START, STOP_DISABLED, STOP_PERMISSION_REQUIRED }

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

class StatusWriteGate(private val heartbeatMillis: Long = 300_000) {
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

/**
 * Only fields whose change warrants an immediate durable status write belong in this signature.
 * Sample time and pipeline counters remain present in status.json, but are snapshots refreshed on
 * the bounded heartbeat instead of forcing an fsync and removable-storage rename every cycle.
 */
internal fun durableStatusSignature(status: PublicStatus): String = listOf(
    status.state,
    status.ownershipEnabled.toString(),
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
    private val fast = listOf(0x04, 0x0C, 0x0D, 0x0E, 0x10, 0x11)
    private val medium = listOf(0x03, 0x05, 0x06, 0x07, 0x0F, 0x14, 0x15)
    private val slow = listOf(0x13, 0x1C, 0x21)

    fun requestedPids(sequence: Long, supported: Set<Int>): List<Int> = buildList {
        addAll(fast)
        // Preserve the 3-cycle cadence for every medium PID without making one cycle issue all
        // seven commands. The slow PIDs retain their 12-cycle cadence and use separated phases.
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
}
