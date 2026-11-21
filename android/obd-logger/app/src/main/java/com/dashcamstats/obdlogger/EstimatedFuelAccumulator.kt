package com.dashcamstats.obdlogger

import java.time.Duration
import java.time.Instant
import kotlin.math.roundToLong

/** Bounded zero-order integration of the MAF-derived estimate, never an ECU fuel counter. */
internal class EstimatedFuelAccumulator(
    private val nominalCycleSeconds: Double = 5.0,
    private val nominalMafSeconds: Double = 15.0,
    private val requireTiming: Boolean = false,
) {
    var litres = 0.0
        private set
    var hasEvidence = false
        private set
    private var previousAt: Instant? = null
    private var previousSequence: Long? = null
    private var previousDriveId: String? = null
    private var rateAt: Instant? = null
    private var rate: Double? = null
    private var previousEffectiveAt: Instant? = null
    private var previousTimed: Boolean? = null

    init {
        require(nominalCycleSeconds.isFinite() && nominalCycleSeconds > 0)
        require(nominalMafSeconds.isFinite() && nominalMafSeconds > 0)
    }

    fun observe(
        driveId: String,
        sequence: Long,
        capturedAt: Instant,
        estimatedRate: Double?,
        invalid: Boolean = false,
        responseAgeMillis: Long? = null,
        timed: Boolean = false,
    ): RateInterval? {
        var interval: RateInterval? = null
        val gap = previousAt?.let { Duration.between(it, capturedAt).toMillis() / 1000.0 } ?: 0.0
        var unusable = invalid || (requireTiming && !timed) ||
            (estimatedRate != null && (!estimatedRate.isFinite() || estimatedRate < 0))
        var contiguous = gap > 0 && gap <= nominalCycleSeconds * 1.5 && previousDriveId == driveId &&
            previousSequence?.let { sequence == it + 1 } == true
        if (timed || requireTiming) {
            var effectiveAt = if (estimatedRate != null) {
                responseAgeMillis?.takeIf { it in 0..60_000L }?.let(capturedAt::minusMillis)
            } else capturedAt
            unusable = unusable || effectiveAt == null
            if (previousTimed == false) contiguous = false
            val effectiveGap = if (effectiveAt != null && previousEffectiveAt != null) {
                Duration.between(previousEffectiveAt, effectiveAt).toMillis() / 1000.0
            } else 0.0
            if (previousEffectiveAt != null && effectiveGap <= 0) {
                unusable = true
                effectiveAt = capturedAt
            }
            if (unusable || !contiguous || effectiveGap <= 0) {
                rate = null
                rateAt = null
            } else if (previousEffectiveAt != null && effectiveAt != null) {
                interval = integrateHeldRate(previousEffectiveAt!!, effectiveAt, nominalMafSeconds * 1.5)
            }
            if (estimatedRate != null && !unusable) {
                rate = estimatedRate
                rateAt = effectiveAt
            }
            previousEffectiveAt = effectiveAt
            previousTimed = true
            previousAt = capturedAt
            previousSequence = sequence
            previousDriveId = driveId
            return interval
        }
        if (previousTimed == true) contiguous = false
        if (unusable || !contiguous) {
            rate = null
            rateAt = null
        } else if (previousAt != null) {
            interval = integrateHeldRate(previousAt!!, capturedAt, nominalCycleSeconds * 3 * 1.5)
        }
        if (estimatedRate != null && !unusable) {
            rate = estimatedRate
            rateAt = capturedAt
        }
        previousAt = capturedAt
        previousSequence = sequence
        previousDriveId = driveId
        previousEffectiveAt = capturedAt
        previousTimed = false
        return interval
    }

    private fun integrateHeldRate(start: Instant, end: Instant, maxHoldSeconds: Double): RateInterval? {
        val heldRate = rate ?: return null
        val expires = rateAt?.plusNanos((maxHoldSeconds * 1e9).roundToLong()) ?: return null
        val until = minOf(end, expires)
        if (start >= until) return null
        val interval = RateInterval(start, until, heldRate, heldRate)
        hasEvidence = true
        litres += interval.integral(start, until)
        return interval
    }

    companion object {
        // Match the server: 5s rows and 15s medium PID cadence, with 1.5x tolerance.
        const val MAX_ROW_GAP_SECONDS = 7.5
        const val MAX_HOLD_SECONDS = 22.5
    }
}
