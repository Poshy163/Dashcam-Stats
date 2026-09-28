package com.dashcamstats.obdlogger

import kotlinx.coroutines.CancellationException
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.ceil

private const val POLL_TIMING_COUNT_LIMIT = 1_000_000L
private const val POLL_TIMING_TIME_LIMIT = 1_000_000_000_000L
private const val POLL_TIMING_OBSERVATION_LIMIT = 86_400_000L

/** Fixed-size recent distribution; max and total cover every counted observation. */
private class PollTimingDistribution {
    var count = 0L
        private set
    private val recent = ArrayDeque<Long>()
    private var maximum = 0L
    private var total = 0L

    fun observe(value: Long) {
        if (count >= POLL_TIMING_COUNT_LIMIT) return
        val bounded = value.coerceIn(0, POLL_TIMING_OBSERVATION_LIMIT)
        count += 1
        if (recent.size == 256) recent.removeFirst()
        recent.addLast(bounded)
        maximum = maxOf(maximum, bounded)
        total = (total + bounded).coerceAtMost(POLL_TIMING_TIME_LIMIT)
    }

    fun json(): JSONObject {
        val ordered = recent.sorted()
        val median = when {
            ordered.isEmpty() -> JSONObject.NULL
            ordered.size % 2 == 1 -> ordered[ordered.size / 2].toDouble()
            else -> (ordered[ordered.size / 2 - 1] + ordered[ordered.size / 2]) / 2.0
        }
        return JSONObject()
            .put("count", count)
            .put("retained", ordered.size)
            .put("median_ms", median)
            .put("p95_ms", if (ordered.isEmpty()) JSONObject.NULL else ordered[ceil(ordered.size * 0.95).toInt() - 1])
            .put("max_ms", maximum)
            .put("total_ms", total)
    }
}

internal enum class PollOutcome(val field: String) {
    SUCCESS("successes"), MISSING("missing"), MALFORMED("malformed"),
    TIMEOUT("timeouts"), TRANSPORT_ERROR("transport_errors"), CANCELLED("cancelled"),
}

/**
 * Drive-scoped, single-owner aggregates. Retains no response, value, wall clock or identity.
 * A request includes existing pacing, mutex wait and parsing. Age starts at decoded query
 * completion, not the unknown instant the ECU measured its sensor.
 */
internal class PollTiming(
    driveOriginMillis: Long? = null,
    private val monotonicMillis: () -> Long,
) {
    private class PidTiming {
        val outcomes = PollOutcome.entries.associateWith { 0L }.toMutableMap()
        var skips = 0L
        val requests = PollTimingDistribution()
        val ages = PollTimingDistribution()
        fun json(pid: Int): JSONObject = JSONObject()
            .put("pid", pid)
            .put("attempts", requests.count)
            .also { body -> outcomes.forEach { (outcome, count) -> body.put(outcome.field, count) } }
            .put("cooldown_skips", skips)
            .put("request_ms", requests.json())
            .put("value_age_at_row_ms", ages.json())
    }

    private var highWater = monotonicMillis().coerceAtLeast(0)
    private val origin = (driveOriginMillis ?: highWater).coerceIn(0, highWater)
    private var windowStart = highWater
    private var windowIndex = 0L
    private var cyclesStarted = 0L
    private var cyclesCompleted = 0L
    private var overruns = 0L
    private var activeCycle: Long? = null
    private var previousCycle: Long? = null
    private var work = PollTimingDistribution()
    private var intervals = PollTimingDistribution()
    private val pids = linkedMapOf<Int, PidTiming>()
    private val rowCompletions = linkedMapOf<Int, Long>()

    fun now(): Long {
        highWater = maxOf(highWater, monotonicMillis().coerceAtLeast(0))
        return highWater
    }

    fun startCycle() {
        check(activeCycle == null)
        val at = now()
        rowCompletions.clear()
        if (cyclesStarted >= POLL_TIMING_COUNT_LIMIT) return
        previousCycle?.let { intervals.observe(at - it) }
        previousCycle = at
        activeCycle = at
        cyclesStarted += 1
    }

    fun finishCycle(completed: Boolean = false) {
        val start = activeCycle ?: return
        val duration = now() - start
        work.observe(duration)
        if (completed) cyclesCompleted += 1
        if (duration > ObdPollPlan.TARGET_CYCLE_MILLIS) overruns += 1
        activeCycle = null
    }

    fun recordRequest(pid: Int, startedAt: Long, outcome: PollOutcome) {
        val stats = stats(pid) ?: return
        val completedAt = now()
        if (stats.requests.count >= POLL_TIMING_COUNT_LIMIT) return
        stats.requests.observe(completedAt - startedAt)
        stats.outcomes[outcome] = stats.outcomes.getValue(outcome) + 1
        if (outcome == PollOutcome.SUCCESS) rowCompletions[pid] = completedAt
    }

    fun cooldownSkip(pid: Int) {
        stats(pid)?.let { it.skips = (it.skips + 1).coerceAtMost(POLL_TIMING_COUNT_LIMIT) }
    }

    /** Call adjacent to the UTC row timestamp, before the database write. Consumed once. */
    fun takeRowAges(): Map<Int, Long> {
        val at = now()
        return rowCompletions.mapValues { at - it.value }.also { rowCompletions.clear() }
    }

    /** Failed/duplicate database writes deliberately contribute no row-age observations. */
    fun rowPersisted(ages: Map<Int, Long>) {
        ages.forEach { (pid, age) ->
            pids[pid]?.let { stats ->
                if (stats.ages.count < stats.outcomes.getValue(PollOutcome.SUCCESS)) stats.ages.observe(age)
            }
        }
    }

    fun drain(reason: String): JSONObject? {
        require(reason in setOf("periodic", "drive_end", "partial_failure"))
        check(activeCycle == null)
        val at = now()
        // Keep diagnostics.json comfortably below its bundle limit even on very long drives.
        // After 64 periodic windows, measurements continue in the bounded final aggregate.
        if (reason == "periodic" && windowIndex >= 64) return null
        if (reason == "periodic" && at - windowStart < 60_000) return null
        if (cyclesStarted == 0L && pids.isEmpty()) return null
        val body = JSONObject()
            .put("schema_version", 1)
            .put("window_index", windowIndex)
            .put("window_start_elapsed_ms", (windowStart - origin).coerceAtMost(POLL_TIMING_TIME_LIMIT))
            .put("window_duration_ms", (at - windowStart).coerceAtMost(POLL_TIMING_TIME_LIMIT))
            .put("flush_reason", reason)
            .put("target_cycle_ms", ObdPollPlan.TARGET_CYCLE_MILLIS)
            .put("cycles_started", cyclesStarted)
            .put("cycles_completed", cyclesCompleted)
            .put("overrun_count", overruns)
            .put("cycle_work_ms", work.json())
            .put("cycle_start_interval_ms", intervals.json())
            .put("pids", JSONArray(pids.toSortedMap().map { (pid, stats) -> stats.json(pid) }))
        windowIndex = (windowIndex + 1).coerceAtMost(POLL_TIMING_COUNT_LIMIT)
        windowStart = at
        cyclesStarted = 0
        cyclesCompleted = 0
        overruns = 0
        work = PollTimingDistribution()
        intervals = PollTimingDistribution()
        pids.clear()
        rowCompletions.clear()
        return body
    }

    private fun stats(pid: Int): PidTiming? = if (pid in KNOWN_PIDS) pids.getOrPut(pid) { PidTiming() } else null

    companion object {
        private val KNOWN_PIDS = setOf(0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13, 0x14, 0x15, 0x1C, 0x20, 0x21)
    }
}

/** Shared by the actual recording loop and fixtures, including thrown/cancelled requests. */
internal suspend fun timedLivePidPoll(
    pid: Int,
    timing: PollTiming,
    query: suspend (Int) -> Map<String, Any>,
): LivePidPollResult {
    val startedAt = timing.now()
    val result = try {
        pollLivePid(pid, query)
    } catch (error: CancellationException) {
        timing.recordRequest(pid, startedAt, PollOutcome.CANCELLED)
        throw error
    } catch (error: ElmCommandTimeoutException) {
        timing.recordRequest(pid, startedAt, PollOutcome.TIMEOUT)
        throw error
    } catch (error: Exception) {
        timing.recordRequest(pid, startedAt, PollOutcome.TRANSPORT_ERROR)
        throw error
    }
    timing.recordRequest(
        pid,
        startedAt,
        when (result) {
            is LivePidPollResult.Values -> PollOutcome.SUCCESS
            LivePidPollResult.Missing -> PollOutcome.MISSING
            is LivePidPollResult.Malformed -> PollOutcome.MALFORMED
        },
    )
    return result
}
