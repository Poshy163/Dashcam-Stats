package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SparseDiagnosticSchedulingTest {
    private val supported = (1..0x21).toSet()
    private val fast = listOf(0x04, 0x0C, 0x0D)

    @Test
    fun capturedBaselineReservationSlotsNoLongerRemoveSevenMafOpportunities() {
        // Anonymous sequence slots from the 259-row 25 October baseline drive. Its
        // first opportunistic diagnostic made the recurring reservations start at13.
        val scheduler = SparseDiagnosticScheduler().apply { observeAttempt(0L) }
        val reservedMaf = mutableListOf<Long>()
        val requestedMaf = mutableListOf<Long>()
        val successfulMaf = mutableListOf<Long>()
        repeat(259) { index ->
            val sequence = index.toLong()
            val reserved = scheduler.reserveCycle(sequence, workPending = true)
            val requested = ObdPollPlan.requestedPids(sequence, supported, reserved)
            assertTrue(requested.containsAll(fast))
            if (reserved) {
                assertEquals(fast + if (sequence % 3 == 1L) listOf(0x10) else emptyList(), requested)
                if (0x10 in requested) reservedMaf += sequence
            }
            if (0x10 in requested) {
                requestedMaf += sequence
                // The independently observed malformed reply stays missing. The next
                // nominal opportunity retries normally; no same-cycle command is added.
                if (sequence != 136L) successfulMaf += sequence
            }
        }
        assertEquals(listOf(13L, 52L, 91L, 130L, 169L, 208L, 247L), reservedMaf)
        assertEquals((0L..258L).filter { it % 3 == 1L }, requestedMaf)
        assertEquals(86, requestedMaf.size)
        assertEquals(85, successfulMaf.size) // formerly78: seven omissions plus one rejected reply
        assertEquals(listOf(136L), requestedMaf - successfulMaf.toSet())
        assertEquals(1, successfulMaf.zipWithNext().count { (a, b) -> b - a == 6L })
        assertTrue(successfulMaf.zipWithNext().all { (a, b) -> b - a in setOf(3L, 6L) })
    }

    @Test
    fun unsupportedMafIsNeverAddedAndOtherRotatingPhasesStayUnchanged() {
        val withoutMaf = supported - 0x10
        repeat(40) { index ->
            val sequence = index.toLong()
            assertFalse(0x10 in ObdPollPlan.requestedPids(sequence, withoutMaf, true))
            assertEquals(fast, ObdPollPlan.requestedPids(sequence, withoutMaf, true))
        }
        assertEquals(listOf(0x04, 0x0C, 0x0D, 0x0E, 0x03, 0x07, 0x15),
            ObdPollPlan.requestedPids(0, supported))
        assertEquals(3, ObdPollPlan.expectedIntervalCycles(0x10))
        assertEquals("medium", ObdPollPlan.tier(0x10))
    }

    @Test
    fun aReservationCannotBypassTheCommandAndOutstandingTailBudgets() {
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(3_350, 1_550, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(3_351, 1_550, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(0, 6_000, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(5_000, 1, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(-1, 1_550, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_000, 1_550, -1))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_000, 1_550, 0))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_000, Long.MAX_VALUE, 100))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_000, 1_550, Long.MAX_VALUE))
        val queue = OneStepPerCycleQueue<String>().apply { add("readiness") }
        if (ObdPollPlan.mayRunSparseDiagnostic(0, 6_000, 100)) queue.take(3)
        assertEquals("readiness", queue.take(16)) // deferral did not dequeue or invent completion
        assertNull(queue.take(16))
    }

    @Test
    fun slowPrimaryWorkDefersScanWithoutReservingEveryFollowingCycle() {
        val scheduler = SparseDiagnosticScheduler()
        assertTrue(scheduler.reserveCycle(3, workPending = true))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(5_001, 1_550, 100))
        for (cycle in 4L..15L) assertFalse(scheduler.reserveCycle(cycle, workPending = true))
        assertTrue(scheduler.reserveCycle(16, workPending = true))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(5_000, 6_000, 100))
    }

    @Test
    fun commandOutlierExpiresOnlyAfterSixteenGenuineNewObservations() {
        val budget = SparseDiagnosticBudgetTracker()
        assertEquals(6_000L, budget.requiredBudgetMillis())
        budget.observeCommand(2_900)
        repeat(15) { budget.observeCommand(650) }
        repeat(200) { assertEquals(6_000L, budget.requiredBudgetMillis()) }
        budget.observeCommand(650)
        assertEquals(1_550L, budget.requiredBudgetMillis())
        budget.observeCommand(Long.MAX_VALUE)
        assertEquals(6_000L, budget.requiredBudgetMillis()) // overflow is conservative
    }

    @Test
    fun slow850MillisBusRetainsDueMafReservationsAndHonestlyDefersDiagnostics() {
        var clock = 0L
        val live = LivePollScheduler { clock }
        val sparse = SparseDiagnosticScheduler()
        val budget = SparseDiagnosticBudgetTracker()
        val queue = OneStepPerCycleQueue<String>().apply { add("readiness") }
        val reservedMaf = mutableListOf<Long>()
        val attemptedReservedMaf = mutableListOf<Long>()
        val mafCycles = mutableListOf<Long>()
        repeat(100) { index ->
            val sequence = index.toLong()
            val started = clock
            val reserved = sparse.reserveCycle(sequence, !queue.isEmpty)
            val requested = ObdPollPlan.requestedPids(sequence, supported, reserved)
            if (reserved && 0x10 in requested) reservedMaf += sequence
            val cycle = live.beginCycle(sequence, requested, started)
            while (true) {
                when (val step = cycle.next()) {
                    LivePollStep.Complete -> break
                    LivePollStep.ReadVoltage -> { clock += 850; live.observeCommand(850); budget.observeCommand(850) }
                    is LivePollStep.QueryPid -> {
                        cycle.attempted(step.pid)
                        if (reserved && step.pid == 0x10) attemptedReservedMaf += sequence
                        if (step.pid == 0x10) mafCycles += sequence
                        clock += 850; live.observeCommand(850); budget.observeCommand(850)
                    }
                    is LivePollStep.Deferred -> Unit
                    is LivePollStep.Interrupted -> error("unexpected interruption")
                }
            }
            clock += 150
            assertFalse(ObdPollPlan.mayRunSparseDiagnostic(clock - started, budget.requiredBudgetMillis(),
                live.remainingTailMillis(150)))
            clock += 100
            live.observeTail(250)
            assertTrue(clock - started <= 5_000)
            clock = started + 5_000
        }
        assertTrue(reservedMaf.isNotEmpty())
        assertEquals(reservedMaf, attemptedReservedMaf)
        assertEquals((0L..99L).filter { it % 3 == 1L }, mafCycles)
        assertEquals("readiness", queue.take(101))
    }

    @Test
    fun unexpectedSlowDiagnosticRemainsAnOverrunAndRaisesTheFollowingBudget() {
        val budget = SparseDiagnosticBudgetTracker()
        repeat(16) { budget.observeCommand(650L) }
        val primaryMillis = 2_750L
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(primaryMillis, budget.requiredBudgetMillis(), 350L))
        val actualDiagnosticMillis = 2_700L
        assertTrue(primaryMillis + actualDiagnosticMillis + 100L > ObdPollPlan.TARGET_CYCLE_MILLIS)
        budget.observeCommand(actualDiagnosticMillis)
        assertEquals(5_650L, budget.requiredBudgetMillis())
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(primaryMillis, budget.requiredBudgetMillis(), 350L))
        repeat(16) { budget.observeCommand(650L) }
        assertEquals(1_550L, budget.requiredBudgetMillis())
    }

    @Test
    fun slowMultiFrameDiagnosticRaisesItsOwnBudgetWithoutStarvingSingleFrameMaf() {
        var clock = 0L
        val live = LivePollScheduler { clock }
        val diagnostics = SparseDiagnosticBudgetTracker()
        repeat(16) { live.observeCommand(650); diagnostics.observeCommand(650) }
        live.observeTail(250)
        // A large fault-code reply is not evidence that the separate single-frame
        // live PID became slow. Real service observations keep these reserves separate.
        diagnostics.observeCommand(2_700)
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_750, diagnostics.requiredBudgetMillis(), 350))
        val cycle = live.beginCycle(1, ObdPollPlan.requestedPids(1, supported, true), clock)
        repeat(3) {
            assertTrue(cycle.next() is LivePollStep.QueryPid)
            clock += 650
        }
        assertEquals(LivePollStep.ReadVoltage, cycle.next())
        clock += 650
        assertEquals(LivePollStep.QueryPid(0x10), cycle.next())
        assertTrue(clock + 650 + 250 <= ObdPollPlan.TARGET_CYCLE_MILLIS)
    }

    @Test
    fun opportunisticProgressPostponesReservationAndIdleQueueDoesNotReserve() {
        val scheduler = SparseDiagnosticScheduler()
        assertFalse(scheduler.reserveCycle(0, workPending = true))
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(2_000, 1_550, 350))
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
