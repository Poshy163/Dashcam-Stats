package com.dashcamstats.obdlogger

/** A different immutable drive plan; the existing five-second plan remains version 7. */
internal object PollTrialPlan {
    const val VERSION = 8
    const val TARGET_MILLIS = 3_000L
    val critical = listOf(0x0D, 0x0C, 0x10)
    val optional = listOf(0x04, 0x0E, 0x11, 0x03, 0x05, 0x06, 0x07, 0x0F, 0x14, 0x15, 0x21, 0x01)
    val fallbackReasons = setOf("required_pids_unavailable", "sustained_overrun", "critical_readings_missing", "transport_fault")

    fun fallbackText(reason: String): String = when (reason) {
        "required_pids_unavailable" -> "The vehicle did not advertise all required readings; baseline selected."
        "sustained_overrun" -> "Readings repeatedly exceeded three seconds; baseline selected for the next connection."
        "critical_readings_missing" -> "Speed, RPM or airflow readings were repeatedly missing; baseline selected for the next connection."
        "transport_fault" -> "The adapter connection became uncertain; baseline selected for a fresh connection."
        else -> "Baseline selected."
    }

    fun cooldownCycle(elapsedMillis: Long): Long = elapsedMillis.coerceAtLeast(0) / ObdPollPlan.TARGET_CYCLE_MILLIS

    fun shuttingDown(now: Long, voltageAt: Long?, voltage: Double?, voltageOff: Double, rpm: Double?): Boolean =
        voltageAt != null && now - voltageAt in 0..10_000L && voltage != null && voltage < voltageOff &&
            (rpm == null || rpm <= 300)

    fun transportFault(error: Exception, sessionTainted: Boolean): Boolean =
        error !is kotlinx.coroutines.CancellationException && error !is ElmQuiesceRequestedException &&
            (sessionTainted || (error is ElmException && error !is ElmCommandRejectedException))

    fun select(enabled: Boolean, supported: Set<Int>): Int =
        if (enabled && supported.containsAll(critical)) VERSION else ObdPollPlan.VERSION
}

internal class PollTrialFallbackException(val reason: String, cause: Throwable? = null) :
    Exception("poll trial fallback: $reason", cause) {
    init { require(reason in PollTrialPlan.fallbackReasons) }
}

/** A failed preference commit cannot permit another trial connection in this service. */
internal class PollTrialFallbackLatch {
    private var pendingReason: String? = null
    fun disable(reason: String, persist: (String) -> Unit) {
        require(reason in PollTrialPlan.fallbackReasons)
        pendingReason = reason
        beforeConnection(persist)
    }
    fun beforeConnection(persist: (String) -> Unit) {
        pendingReason?.let { reason ->
            persist(reason) // Leave the latch set if persistence fails.
            pendingReason = null
        }
    }
}

internal data class TimedTrialRow(
    val values: Map<String, Any>,
    val ages: Map<Int, Long>,
    val missing: List<Int>,
)

/** A suspended worker cannot publish a successful value without its bounded response proof. */
internal fun prepareTimedTrialRow(
    observed: Map<String, Any>,
    decodedFields: Map<Int, Set<String>>,
    responseAges: Map<Int, Long>,
    missing: List<Int>,
): TimedTrialRow {
    val validAges = responseAges.filter { (pid, age) -> pid in decodedFields && age in 0..60_000 }
    val unproved = decodedFields.keys - validAges.keys
    val values = LinkedHashMap(observed)
    unproved.forEach { pid -> decodedFields.getValue(pid).forEach(values::remove) }
    values.remove("estimated_fuel_rate")
    values.remove("estimated_fuel_consumption")
    values.putAll(ElmProtocol.estimates(values))
    return TimedTrialRow(values, validAges, (missing + unproved).distinct())
}

/** Only completed, still-running cycles count. A normal engine stop is checked first. */
internal class PollTrialHealth {
    private var overruns = 0
    private var missing = 0
    fun reset() { overruns = 0; missing = 0 }
    fun completed(workMillis: Long, successfulPids: Set<Int>): String? {
        overruns = if (workMillis > PollTrialPlan.TARGET_MILLIS) overruns + 1 else 0
        missing = if (!successfulPids.containsAll(PollTrialPlan.critical)) missing + 1 else 0
        return when {
            overruns >= 3 -> "sustained_overrun"
            missing >= 3 -> "critical_readings_missing"
            else -> null
        }
    }
}

/**
 * Due times use elapsed time, not row numbers. Overdue auxiliary work is served oldest first;
 * each attempt moves its deadline forward from now, so a slow cycle cannot create a burst.
 * The separate trial reserve admits one ~600ms request after the measured ~1.86s fast trio,
 * while retaining a measured persistence tail. No optional request can delay the fast trio.
 */
internal class PollTrialScheduler(private val monotonicMillis: () -> Long) {
    private val due = linkedMapOf<Int, Long>()
    private val commands = ArrayDeque<Long>()
    private val tails = ArrayDeque<Long>()
    private var highWater = monotonicMillis().coerceAtLeast(0)
    private val origin = highWater

    init {
        due[VOLTAGE] = origin
        PollTrialPlan.optional.forEachIndexed { index, pid ->
            due[pid] = origin + if (pid == 0x04) 0 else index * PollTrialPlan.TARGET_MILLIS
        }
    }

    fun duePids(supported: Set<Int>): List<Int> = due.entries
        .filter { it.key == VOLTAGE || it.key in supported }
        .filter { it.value <= now() }
        .sortedWith(compareBy<Map.Entry<Int, Long>> { it.value }.thenBy { it.key })
        .map { it.key }

    /** Due voltage is mandatory and precedes all optional ECU probes. */
    fun commands(supported: Set<Int>): List<Int> {
        val auxiliary = duePids(supported)
        return PollTrialPlan.critical + auxiliary.filter { it == VOLTAGE } + auxiliary.filter { it != VOLTAGE }
    }

    fun mayRunCommand(pid: Int, startedAt: Long, diagnosticReserved: Boolean): Boolean =
        pid in PollTrialPlan.critical || pid == VOLTAGE || (!diagnosticReserved && mayRun(startedAt))

    fun mayRun(startedAt: Long): Boolean =
        PollTrialPlan.TARGET_MILLIS - (now() - startedAt) >= commandReserve() + tailReserve()

    fun attempted(pid: Int) {
        require(pid in due)
        due[pid] = now() + when (pid) { VOLTAGE -> 5_000L; 0x04 -> 15_000L; else -> 60_000L }
    }

    fun observeCommand(millis: Long) = retain(commands, millis)
    fun observeTail(millis: Long) = retain(tails, millis)
    fun commandReserve(): Long = if (commands.isEmpty()) 900L else
        maxOf(650L, (commands.max() * 11 + 9) / 10 + 25)
    fun tailReserve(): Long = if (tails.isEmpty()) 200L else maxOf(150L, tails.max() + 50)

    private fun retain(history: ArrayDeque<Long>, millis: Long) {
        if (history.size == 16) history.removeFirst()
        history.addLast(millis.coerceIn(0, 60_000))
    }
    private fun now(): Long {
        highWater = maxOf(highWater, monotonicMillis().coerceAtLeast(0))
        return highWater
    }
    companion object { const val VOLTAGE = -1 }
}
