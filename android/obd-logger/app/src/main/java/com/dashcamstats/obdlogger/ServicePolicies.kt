package com.dashcamstats.obdlogger

enum class ServiceStartupDecision { START, STOP_DISABLED, STOP_PERMISSION_REQUIRED }

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

/** Suppresses a malformed advertised PID until the next fresh connection. */
internal class LivePidMalformedTracker {
    private val malformed = mutableSetOf<Int>()

    fun isMalformed(pid: Int): Boolean = pid in malformed

    fun markMalformed(pid: Int): Boolean = malformed.add(pid)
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
        if (sequence % 3L == 0L) addAll(medium)
        if (sequence % 12L == 0L) addAll(slow)
    }.filter(supported::contains)
}
