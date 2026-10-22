package com.dashcamstats.obdlogger

import org.json.JSONObject
import java.time.Duration
import java.time.Instant

internal const val PID_RESPONSE_AGES_KEY = "pid_response_age_ms"

/** Explicit but incomplete timing is unknown; only legacy rows fall back to row UTC. */
internal fun obdResponseAgeMillis(quality: JSONObject, pid: Int): Long? {
    val raw = quality.optJSONObject(PID_RESPONSE_AGES_KEY)?.opt("%02X".format(pid))
    val age = when (raw) {
        is Int -> raw.toLong()
        is Long -> raw
        else -> return null
    }
    return age.takeIf { it in 0..60_000L }
}

internal fun obdResponseTime(rowAt: Instant, quality: JSONObject, pid: Int, requireTiming: Boolean = false): Instant? =
    if (!quality.has(PID_RESPONSE_AGES_KEY)) if (requireTiming) null else rowAt
    else obdResponseAgeMillis(quality, pid)?.let(rowAt::minusMillis)

/** Shared by the production exporter and calculation fixtures; no fabricated simultaneous reads. */
internal class TimedMotionAccumulator(
    private val nominalCycleSeconds: Double = 5.0,
    private val requireTiming: Boolean = false,
) {
    var distanceKm = 0.0
        private set
    var distanceIntervals = 0
        private set
    var idleSeconds = 0.0
        private set
    var idleEvidence = false
        private set

    private data class Observation(
        val drive: String,
        val sequence: Long,
        val rowAt: Instant,
        val speedAt: Instant?,
        val speed: Double?,
        val rpm: Double?,
        val timed: Boolean,
        val live: Boolean,
    )

    private var previous: Observation? = null

    fun observe(
        drive: String,
        sequence: Long,
        rowAt: Instant,
        speed: Double?,
        rpm: Double?,
        status: String,
        quality: JSONObject,
    ) {
        val timed = quality.has(PID_RESPONSE_AGES_KEY) || requireTiming
        val live = status == "live" && quality.optString("transport", "ok") == "ok"
        val missing = quality.optJSONArray("missing_pids")
        fun missingPid(pid: Int): Boolean = missing != null && (0 until missing.length()).any {
            missing.optInt(it, -1) == pid
        }
        var speedAt = if (speed != null || !timed) obdResponseTime(rowAt, quality, 0x0D, requireTiming) else null
        val rpmAt = if (rpm != null) obdResponseTime(rowAt, quality, 0x0C, requireTiming) else null
        var observedRpm = rpm
        if (timed && (!live || missingPid(0x0D))) speedAt = null
        if (timed && (!live || rpmAt == null || missingPid(0x0C))) observedRpm = null
        previous?.let { first ->
            val rowGap = Duration.between(first.rowAt, rowAt).toMillis() / 1000.0
            val contiguous = (!timed && !first.timed) || (
                timed && first.timed && live && first.live && drive == first.drive &&
                    sequence == first.sequence + 1 && rowGap > 0 && rowGap <= nominalCycleSeconds * 1.5
                )
            val gap = if (speedAt != null && first.speedAt != null) {
                Duration.between(first.speedAt, speedAt).toMillis() / 1000.0
            } else 0.0
            val maximumGap = nominalCycleSeconds * (if (timed) 1.5 else 3.0)
            if (contiguous && gap > 0 && gap <= maximumGap) {
                if (first.speed != null && speed != null) {
                    distanceKm += (first.speed + speed) / 2 * gap / 3600
                    distanceIntervals += 1
                }
                if (first.rpm != null && first.speed != null) {
                    idleEvidence = true
                    if (first.rpm > 300 && first.speed < 1) idleSeconds += gap
                }
            }
        }
        previous = Observation(drive, sequence, rowAt, speedAt, speed, observedRpm, timed, live)
    }
}
