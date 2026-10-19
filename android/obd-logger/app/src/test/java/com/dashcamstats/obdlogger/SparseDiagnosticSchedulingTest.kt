package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SparseDiagnosticSchedulingTest {
    @Test
    fun busyFiveSecondPollStillCompletesReadinessAndAllDtcModes() {
        val budget = SparseDiagnosticBudgetTracker()
        // The real failure mode: one slow command permanently raises the conservative
        // reserve above the entire target. Even an empty normal cycle cannot admit it.
        budget.observeCommand(2_900)
        assertEquals(6_000L, budget.requiredBudgetMillis())
        val scheduler = SparseDiagnosticScheduler()
        val queue = OneStepPerCycleQueue<String>()
        val scan = listOf("readiness", "confirmed_dtcs", "pending_dtcs", "permanent_dtcs", "dtc_scan_complete")
        scan.forEach(queue::add)
        val supported = (1..0x21).toSet()
        val completed = mutableListOf<String>()
        val scanCycles = mutableListOf<Long>()
        var oldPolicyAttempts = 0
        var deferredCommands = 0
        var longestCycleMillis = 0L

        repeat(252) { index ->
            val cycle = index.toLong()
            val ordinary = ObdPollPlan.requestedPids(cycle, supported)
            val reserved = scheduler.reserveCycle(cycle, !queue.isEmpty)
            val requested = ObdPollPlan.requestedPids(cycle, supported, reserved)
            // Primary telemetry is polled and persisted before the diagnostic queue.
            assertTrue(requested.containsAll(listOf(0x04, 0x0C, 0x0D)))
            if (reserved) assertEquals(listOf(0x04, 0x0C, 0x0D), requested)
            else assertEquals(ordinary, requested)
            deferredCommands += ordinary.size - requested.size
            val primaryPersisted = true
            val elapsedMillis = requested.size * 700L + 50L
            if (ObdPollPlan.mayRunSparseDiagnostic(ordinary.size * 700L + 50L, budget.requiredBudgetMillis())) {
                oldPolicyAttempts++
            }
            var cycleMillis = elapsedMillis
            if (primaryPersisted && ObdPollPlan.mayRunSparseDiagnostic(elapsedMillis, budget.requiredBudgetMillis(), reserved)) {
                queue.take(cycle)?.let { step ->
                    completed += step
                    scanCycles += cycle
                    scheduler.observeAttempt(cycle)
                    cycleMillis += 6_000L // fake slow, but bounded, diagnostic reply
                    assertNull(queue.take(cycle))
                }
            }
            longestCycleMillis = maxOf(longestCycleMillis, cycleMillis)
        }

        assertEquals(0, oldPolicyAttempts)
        assertEquals(scan, completed)
        assertEquals(listOf(3L, 16L, 29L, 42L, 55L), scanCycles)
        assertTrue(scanCycles.zipWithNext().all { (a, b) -> b - a >= 13 })
        assertTrue(deferredCommands > scan.size) // fewer total ECU requests than before
        assertEquals(8_150L, longestCycleMillis)
    }

    @Test
    fun slowPrimaryWorkDefersScanWithoutReservingEveryFollowingCycle() {
        val scheduler = SparseDiagnosticScheduler()
        assertTrue(scheduler.reserveCycle(3, workPending = true))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(5_001, 6_000, diagnosticReserved = true))
        for (cycle in 4L..15L) assertFalse(scheduler.reserveCycle(cycle, workPending = true))
        assertTrue(scheduler.reserveCycle(16, workPending = true))
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(5_000, 6_000, diagnosticReserved = true))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_000, 6_001, diagnosticReserved = true))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(-1, 2_000, diagnosticReserved = true))
    }

    @Test
    fun opportunisticProgressPostponesReservationAndIdleQueueDoesNotReserve() {
        val scheduler = SparseDiagnosticScheduler()
        assertFalse(scheduler.reserveCycle(0, workPending = true))
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(2_000, 2_000))
        scheduler.observeAttempt(2)
        for (cycle in 3L..14L) assertFalse(scheduler.reserveCycle(cycle, workPending = true))
        assertFalse(scheduler.reserveCycle(15, workPending = false))
        assertTrue(scheduler.reserveCycle(16, workPending = true))
    }

    @Test
    fun reservationRotatesAcrossEveryMediumAndSlowPhase() {
        val scheduler = SparseDiagnosticScheduler()
        val phases = (0L..160L).filter { scheduler.reserveCycle(it, workPending = true) }
        assertEquals((0L..2L).toSet(), phases.map { it % 3 }.toSet())
        assertEquals((0L..11L).toSet(), phases.map { it % 12 }.toSet())
    }
}
