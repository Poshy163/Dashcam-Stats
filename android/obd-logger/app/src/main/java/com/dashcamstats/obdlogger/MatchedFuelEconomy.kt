package com.dashcamstats.obdlogger

import java.time.Duration
import java.time.Instant
import java.util.ArrayDeque

/** A linearly changing speed (km/h), or a held fuel estimate (L/h). */
internal data class RateInterval(val start: Instant, val end: Instant, val first: Double, val last: Double) {
    fun integral(from: Instant, until: Instant): Double {
        val length = Duration.between(from, until).toNanos()
        val midpoint = Duration.between(start, from).toNanos() + length / 2.0
        val rate = first + (last - first) * midpoint / Duration.between(start, end).toNanos()
        return rate * length / 3_600_000_000_000.0
    }
}

/** One drive's fuel and speed replies arrive at different times, sometimes across sample rows. */
internal class MatchedFuelEconomy(startedAt: Instant) {
    private val speeds = ArrayDeque<RateInterval>()
    private val fuels = ArrayDeque<RateInterval>()
    private var lastMatchEnd = startedAt
    private var previousSequence: Long? = null
    private var previousTimed = false
    private var distanceKm = 0.0
    private var litres = 0.0

    val consumption: Double? get() = if (distanceKm > 0) 100 * litres / distanceKm else null

    fun observe(sequence: Long, capturedAt: Instant, timed: Boolean, speed: RateInterval?, fuel: RateInterval?) {
        val continuous = previousSequence?.let { sequence == it + 1 } == true && previousTimed == timed
        previousSequence = sequence
        previousTimed = timed
        if (!continuous) {
            speeds.clear()
            fuels.clear()
            return
        }
        // Replies can be 60 seconds old and their preceding speed interval can be 15 seconds long.
        val cutoff = capturedAt.minusSeconds(75)
        while (speeds.isNotEmpty() && speeds.first.end <= cutoff) speeds.removeFirst()
        while (fuels.isNotEmpty() && fuels.first.end <= cutoff) fuels.removeFirst()
        if (speed != null) speeds.addLast(speed)
        if (fuel != null) fuels.addLast(fuel)
        while (speeds.isNotEmpty() && fuels.isNotEmpty()) {
            val moving = speeds.first
            val burning = fuels.first
            val start = maxOf(lastMatchEnd, moving.start, burning.start)
            val end = minOf(moving.end, burning.end)
            if (start < end) {
                distanceKm += moving.integral(start, end)
                litres += burning.integral(start, end)
                lastMatchEnd = end
            }
            if (moving.end <= burning.end) speeds.removeFirst()
            if (burning.end <= moving.end) fuels.removeFirst()
        }
    }
}
