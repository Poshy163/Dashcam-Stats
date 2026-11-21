package com.dashcamstats.obdlogger

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DriveStartupReadsTest {
    @Test fun firstBaselineRowEndsAtTheMotionRepliesAndRetainsTheOtherDuePidsAsMissing() {
        val supported = setOf(0x04, 0x0C, 0x0D, 0x0E)
        val due = ObdPollPlan.requestedPids(0, supported)
        val motion = DriveStartupReads.firstBaselinePids(due)
        val scheduler = LivePollScheduler { 0L }
        val first = scheduler.beginCycle(0, motion, 0, includeVoltage = false)
        assertEquals(LivePollStep.QueryPid(0x0D), first.next())
        assertEquals(LivePollStep.QueryPid(0x0C), first.next())
        assertEquals(LivePollStep.Complete, first.next())
        assertEquals(setOf(0x04, 0x0E), (due - motion.toSet()).toSet())
        assertEquals(listOf(0x0C), DriveStartupReads.firstBaselinePids(listOf(0x04, 0x0C)))
        assertTrue(DriveStartupReads.firstBaselinePids(listOf(0x04)).isEmpty())
        val next = scheduler.beginCycle(1, ObdPollPlan.requestedPids(1, supported), 5_000)
        assertEquals(LivePollStep.QueryPid(0x04), next.next())
    }

    @Test fun trialKeepsFirstRowsMafAndItsOwnResponseTimeAndDefersRatherThanReschedulingOptionalPids() {
        var now = 0L
        val scheduler = PollTrialScheduler { now }
        val supported = setOf(0x04, 0x0C, 0x0D, 0x10)
        now = 1_800
        val commands = scheduler.commands(supported)
        assertEquals(listOf(0x0D, 0x0C, 0x10), commands.take(3))
        val due = commands.drop(3)
        assertTrue(DriveStartupReads.firstTrialAuxiliary(due, criticalMeasured = true).isEmpty())
        assertEquals(listOf(PollTrialScheduler.VOLTAGE), DriveStartupReads.firstTrialAuxiliary(due, criticalMeasured = false))
        val row = prepareTimedTrialRow(
            mapOf("vehicle_speed" to 40.0, "engine_rpm" to 1500.0, "mass_air_flow" to 8.0),
            mapOf(0x0D to setOf("vehicle_speed"), 0x0C to setOf("engine_rpm"), 0x10 to setOf("mass_air_flow")),
            mapOf(0x0D to 1200L, 0x0C to 600L, 0x10 to 0L), due.filter { it >= 0 },
        )
        assertEquals(listOf(0x04), row.missing)
        assertEquals(1200L, row.ages[0x0D])
        assertTrue("estimated_fuel_rate" in row.values)
        assertFalse("adapter_voltage" in row.values)
        scheduler.attempted(PollTrialScheduler.VOLTAGE)
        now = 3_000
        assertEquals(listOf(0x04), scheduler.duePids(supported))
    }

    @Test fun oneOffMetadataUsesSeparateSlotsAndValuesAppearOnlyAfterAReply() = runTest {
        val commands = mutableListOf<String>()
        val startup = DriveStartupReads(setOf(0x13, 0x1C), { commands.add("protocol") }) { pid ->
            commands += pid.toString(16)
            mapOf("pid-$pid" to pid)
        }
        assertTrue(startup.values.isEmpty())
        for (sequence in 0L..5L) {
            assertEquals(sequence % 2 == 0L, startup.reserve(sequence))
            if (startup.reserve(sequence)) startup.readOne()
        }
        assertEquals(listOf("protocol", "13", "1c"), commands)
        assertEquals(setOf("pid-19", "pid-28"), startup.values.keys)
        assertFalse(startup.reserve(6))
    }
}
