package com.dashcamstats.obdlogger

import java.time.Duration
import java.time.Instant

/** Bounded zero-order integration of the MAF-derived estimate, never an ECU fuel counter. */
internal class EstimatedFuelAccumulator {
    var litres = 0.0
        private set
    var hasEvidence = false
        private set
    private var previousAt: Instant? = null
    private var previousSequence: Long? = null
    private var previousDriveId: String? = null
    private var rateAt: Instant? = null
    private var rate: Double? = null

    fun observe(
        driveId: String,
        sequence: Long,
        capturedAt: Instant,
        estimatedRate: Double?,
        invalid: Boolean = false,
    ) {
        val gap = previousAt?.let { Duration.between(it, capturedAt).toMillis() / 1000.0 } ?: 0.0
        val unusable = invalid || (estimatedRate != null && (!estimatedRate.isFinite() || estimatedRate < 0))
        val contiguous = gap > 0 && gap <= MAX_ROW_GAP_SECONDS && previousDriveId == driveId &&
            previousSequence?.let { sequence == it + 1 } == true
        if (unusable || !contiguous) {
            rate = null
            rateAt = null
        } else if (rate != null && rateAt != null) {
            val ageAtStart = Duration.between(rateAt, previousAt).toMillis() / 1000.0
            val covered = minOf(gap, (MAX_HOLD_SECONDS - ageAtStart).coerceAtLeast(0.0))
            if (covered > 0) {
                hasEvidence = true
                litres += rate!! * covered / 3600
            }
        }
        if (estimatedRate != null && !unusable) {
            rate = estimatedRate
            rateAt = capturedAt
        }
        previousAt = capturedAt
        previousSequence = sequence
        previousDriveId = driveId
    }

    companion object {
        // Match the server: 5s rows and 15s medium PID cadence, with 1.5x tolerance.
        const val MAX_ROW_GAP_SECONDS = 7.5
        const val MAX_HOLD_SECONDS = 22.5
    }
}
