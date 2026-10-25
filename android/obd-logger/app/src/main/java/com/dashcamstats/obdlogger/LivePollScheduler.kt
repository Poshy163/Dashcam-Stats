package com.dashcamstats.obdlogger

import kotlinx.coroutines.delay

internal sealed interface LivePollStep {
    data class QueryPid(val pid: Int) : LivePollStep
    data object ReadVoltage : LivePollStep
    data class Deferred(val pid: Int) : LivePollStep
    data class Interrupted(val remainingPids: List<Int>) : LivePollStep
    data object Complete : LivePollStep
}

/**
 * Admission, not a faster bus or a longer target. Critical PIDs and ATRV precede optional work.
 * Due MAF has first claim on the budget after critical PIDs and voltage; other optional
 * work remains fair by last attempt. Every optional command must fit its measured
 * pacing-inclusive reserve plus the full non-command tail.
 * Only this cycle's nominally due PIDs are eligible; a deferral never creates a catch-up burst.
 */
internal class LivePollScheduler(private val monotonicMillis: () -> Long) {
    private val recentCommands = ArrayDeque<Long>()
    private val recentTails = ArrayDeque<Long>()
    private val lastAttempts = mutableMapOf<Int, Long>()
    private var highWater = monotonicMillis().coerceAtLeast(0)

    fun beginCycle(sequence: Long, requestedPids: List<Int>, startedAtMillis: Long): Cycle {
        val critical = requestedPids.filter { ObdPollPlan.tier(it) == "fast" }
        val optional = requestedPids.filter { ObdPollPlan.tier(it) != "fast" }
            .sortedWith(compareBy<Int> { if (it == 0x10) 0 else 1 }
                .thenBy { lastAttempts[it] ?: Long.MIN_VALUE })
        return Cycle(sequence, startedAtMillis, critical + listOf(null) + optional)
    }

    fun observeAttempt(pid: Int, sequence: Long) {
        if (ObdPollPlan.tier(pid) in setOf("medium", "slow")) lastAttempts[pid] = sequence
    }

    /** Whole request duration, including existing inter-command pacing and mutex waits. */
    fun observeCommand(durationMillis: Long) = retain(recentCommands, durationMillis)

    /** Full non-command tail, including persistence, handoff, status and timing writes. */
    fun observeTail(durationMillis: Long) = retain(recentTails, durationMillis)

    fun requiredCommandMillis(): Long = if (recentCommands.isEmpty()) INITIAL_COMMAND_MILLIS
        else conservativeReserve(recentCommands.max(), MINIMUM_COMMAND_MILLIS)

    // Tail is already the observed aggregate of parsing/estimation, persistence and
    // post-sample handoff/status/timing work; diagnostics are separately timed commands.
    // Avoid multiplying both independent reserves until a fitting optional request starves.
    private fun requiredTailMillis(): Long = if (recentTails.isEmpty()) INITIAL_TAIL_MILLIS
        else maxOf(MINIMUM_TAIL_MILLIS, recentTails.max() + 100L)

    /** Do not reserve already-completed parsing/persistence twice when admitting diagnostics. */
    fun remainingTailMillis(completedTailMillis: Long): Long =
        (requiredTailMillis() - completedTailMillis.coerceAtLeast(0L)).coerceAtLeast(MINIMUM_POST_SAMPLE_MILLIS)

    private fun retain(window: ArrayDeque<Long>, durationMillis: Long) {
        if (window.size == HISTORY_SIZE) window.removeFirst()
        window.addLast(durationMillis.coerceIn(0, MAXIMUM_OBSERVATION_MILLIS))
    }

    private fun conservativeReserve(durationMillis: Long, minimum: Long): Long =
        maxOf(minimum, (durationMillis * 5 + 3) / 4 + 25)

    private fun now(): Long {
        highWater = maxOf(highWater, monotonicMillis().coerceAtLeast(0))
        return highWater
    }

    inner class Cycle internal constructor(
        private val sequence: Long,
        private val startedAtMillis: Long,
        private val commands: List<Int?>,
    ) {
        private var nextIndex = 0

        val remainingPids: List<Int>
            get() = commands.drop(nextIndex).filterNotNull()

        fun next(preempted: Boolean = false): LivePollStep {
            // Also checked after the last command, before the caller persists a normal row.
            if (preempted) return LivePollStep.Interrupted(remainingPids)
            if (nextIndex == commands.size) return LivePollStep.Complete
            val pid = commands[nextIndex++] ?: return LivePollStep.ReadVoltage
            if (ObdPollPlan.tier(pid) != "fast") {
                val elapsed = (now() - startedAtMillis).coerceAtLeast(0)
                val remaining = ObdPollPlan.TARGET_CYCLE_MILLIS - elapsed
                if (remaining < requiredCommandMillis() + requiredTailMillis()) {
                    return LivePollStep.Deferred(pid)
                }
            }
            return LivePollStep.QueryPid(pid)
        }

        fun attempted(pid: Int) = observeAttempt(pid, sequence)
    }

    companion object {
        private const val HISTORY_SIZE = 16
        private const val INITIAL_COMMAND_MILLIS = 1_000L
        private const val INITIAL_TAIL_MILLIS = 1_500L
        private const val MINIMUM_COMMAND_MILLIS = 750L
        private const val MINIMUM_TAIL_MILLIS = 500L
        private const val MINIMUM_POST_SAMPLE_MILLIS = 100L
        private const val MAXIMUM_OBSERVATION_MILLIS = 60_000L
    }
}

/** No early wake can increase bus duty; a quiesce request interrupts the idle wait promptly. */
internal suspend fun awaitLivePollDeadline(
    cycleStartedMillis: Long,
    monotonicMillis: () -> Long,
    preempted: () -> Boolean,
    targetMillis: Long = ObdPollPlan.TARGET_CYCLE_MILLIS,
) {
    var latest = cycleStartedMillis
    while (!preempted()) {
        latest = maxOf(latest, monotonicMillis())
        val remaining = targetMillis - (latest - cycleStartedMillis)
        if (remaining <= 0) return
        delay(minOf(250L, remaining))
    }
}
