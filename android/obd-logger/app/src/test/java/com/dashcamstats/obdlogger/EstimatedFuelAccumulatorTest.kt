package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

class EstimatedFuelAccumulatorTest {
    private val start = Instant.parse("2026-09-28T00:00:00Z")

    private fun EstimatedFuelAccumulator.add(
        seconds: Long,
        rate: Double? = null,
        sequence: Long = seconds / 5,
        drive: String = "one",
        invalid: Boolean = false,
    ) = observe(drive, sequence, start.plusSeconds(seconds), rate, invalid)

    @Test
    fun sparseMafReadingsCoverAllObservedIntervals() {
        val fuel = EstimatedFuelAccumulator()
        for (t in 0L..30L step 5) fuel.add(t, if (t % 15 == 0L) 6.0 else null)
        assertEquals(6.0 * 30 / 3600, fuel.litres, 1e-10)
        assertTrue(fuel.hasEvidence)
    }

    @Test
    fun newRateOnlyAppliesAfterItsObservation() {
        val fuel = EstimatedFuelAccumulator()
        for (t in 0L..30L step 5) fuel.add(t, when (t) { 0L -> 6.0; 15L -> 12.0; else -> null })
        assertEquals((6.0 * 15 + 12.0 * 15) / 3600, fuel.litres, 1e-10)
    }

    @Test
    fun estimateExpiresAtMediumCadenceToleranceAndDoesNotExtrapolate() {
        val fuel = EstimatedFuelAccumulator()
        fuel.add(0, 6.0)
        assertFalse(fuel.hasEvidence)
        for (t in 5L..45L step 5) fuel.add(t)
        assertEquals(6.0 * 22.5 / 3600, fuel.litres, 1e-10)
    }

    @Test
    fun invalidAndMissingRowsNeverBridgeTheOldEstimate() {
        for (case in 0..8) {
            val fuel = EstimatedFuelAccumulator()
            fuel.add(0, 6.0)
            fuel.add(5)
            fuel.add(10)
            when (case) {
                0 -> fuel.add(15, invalid = true)
                1 -> fuel.add(15, Double.NaN)
                2 -> fuel.add(15, Double.POSITIVE_INFINITY)
                3 -> fuel.add(15, -1.0)
                4 -> fuel.add(15, sequence = 4)
                5 -> fuel.add(15, drive = "two")
                6 -> fuel.add(20, sequence = 3)
                7 -> fuel.add(10, sequence = 3)
                8 -> fuel.add(5, sequence = 3)
            }
            fuel.add(25)
            assertEquals("case $case", 6.0 * 10 / 3600, fuel.litres, 1e-10)
        }
    }

    @Test
    fun contradictoryPresentRateDoesNotOverrideExplicitFailure() {
        val fuel = EstimatedFuelAccumulator()
        fuel.add(0, 6.0)
        fuel.add(5, 6.0, invalid = true)
        fuel.add(10)
        assertFalse(fuel.hasEvidence)
    }

    @Test
    fun freshRateAfterGapRestartsWithoutBackfilling() {
        val fuel = EstimatedFuelAccumulator()
        fuel.add(0, 6.0)
        fuel.add(10, 12.0, sequence = 1)
        fuel.add(15, sequence = 2)
        assertEquals(12.0 * 5 / 3600, fuel.litres, 1e-10)
    }

    @Test
    fun zeroRateIsEvidenceAndInstancesDoNotShareState() {
        val fuel = EstimatedFuelAccumulator()
        fuel.add(0, 0.0)
        fuel.add(5)
        assertTrue(fuel.hasEvidence)
        assertEquals(0.0, fuel.litres, 0.0)
        val nextDrive = EstimatedFuelAccumulator()
        nextDrive.add(0)
        nextDrive.add(5)
        assertFalse(nextDrive.hasEvidence)
    }
}
