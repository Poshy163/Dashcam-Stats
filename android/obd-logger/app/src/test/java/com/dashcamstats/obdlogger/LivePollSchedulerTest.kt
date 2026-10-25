package com.dashcamstats.obdlogger

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class LivePollSchedulerTest {
    private val supported = (1..0x21).toSet()
    private val critical = listOf(0x04, 0x0C, 0x0D)

    @Test
    fun measuredSerialCostsDeferOptionalWorkAndKeepAllCriticalValuesFresh() {
        for (commandMillis in listOf(610L, 850L)) {
            var clock = 0L
            val scheduler = LivePollScheduler { clock }
            val seen = mutableMapOf<Int, MutableList<Long>>()
            var deferred = 0
            repeat(120) { index ->
                val sequence = index.toLong()
                val started = clock
                val due = ObdPollPlan.requestedPids(sequence, supported)
                val cycle = scheduler.beginCycle(sequence, due, started)
                // A new sample never starts with the previous row's sensor values.
                val values = linkedMapOf<Int, Long>()
                val missing = mutableListOf<Int>()
                val commands = mutableListOf<Int?>()
                while (true) {
                    when (val step = cycle.next()) {
                        LivePollStep.Complete -> break
                        LivePollStep.ReadVoltage -> {
                            commands += null
                            clock += commandMillis
                            scheduler.observeCommand(commandMillis)
                        }
                        is LivePollStep.QueryPid -> {
                            cycle.attempted(step.pid)
                            commands += step.pid
                            clock += commandMillis
                            scheduler.observeCommand(commandMillis)
                            values[step.pid] = clock
                            seen.getOrPut(step.pid) { mutableListOf() }.add(sequence)
                        }
                        is LivePollStep.Deferred -> { missing += step.pid; deferred += 1 }
                        is LivePollStep.Interrupted -> error("unexpected interruption")
                    }
                }
                clock += 150L
                scheduler.observeTail(150L)

                assertEquals(critical + listOf(null), commands.take(4))
                assertTrue(values.keys.containsAll(critical))
                assertTrue(values.values.all { it in started..clock })
                assertTrue(missing.none(values::containsKey))
                assertEquals(due.toSet(), values.keys + missing)
                assertTrue(commands.filterNotNull().all(due::contains)) // no catch-up PID
                assertEquals(commands.size, commands.distinct().size)
                assertTrue("$commandMillis ms request overran at $sequence", clock - started <= 5_000L)
                assertTrue(commands.size <= due.size + 1) // never adds ECU requests to gain cadence
                clock = started + 5_000L
            }

            assertTrue(deferred > 0)
            val optional = (0L..11L).flatMap { ObdPollPlan.requestedPids(it, supported) }.toSet() - critical.toSet()
            // At850ms only one optional command fits. Fuel gets that slot when due;
            // coolant/intake/slow21 share its nominal phase and remain honestly missing.
            val admitted = if (commandMillis == 850L) optional - setOf(0x05, 0x0F, 0x21) else optional
            assertEquals(admitted, seen.keys - critical.toSet())
            assertEquals((0L..119L).filter { it % 3 == 1L }, seen.getValue(0x10))
            admitted.forEach { pid ->
                val attempts = seen.getValue(pid)
                assertTrue("PID $pid starved at $commandMillis ms", attempts.size >= 3)
                assertTrue("PID $pid unfairly delayed", attempts.zipWithNext().all { (a, b) -> b - a <= 24 })
            }
        }
    }

    @Test
    fun commandAndPersistenceReservesAreRecheckedAgainstTheActualRemainingTime() {
        var clock = 0L
        val scheduler = LivePollScheduler { clock }
        repeat(4) { scheduler.observeCommand(610L) }
        scheduler.observeTail(800L)
        val cycle = scheduler.beginCycle(0, ObdPollPlan.requestedPids(0, supported), 0L)
        repeat(3) { assertTrue(cycle.next() is LivePollStep.QueryPid) }
        assertEquals(LivePollStep.ReadVoltage, cycle.next())
        clock = 3_000L
        assertTrue(cycle.next() is LivePollStep.QueryPid) // 2,000 ms fits 788 + 900
        clock += 610L
        assertTrue(cycle.next() is LivePollStep.Deferred) // no assumption that the first fit means all fit
        assertTrue(cycle.next() is LivePollStep.Deferred)
    }

    @Test
    fun slowRowPersistenceStillAllowsFairOptionalProgressWhenOneCommandFits() {
        for (tailMillis in listOf(1_000L, 1_500L)) {
            var clock = 0L
            val scheduler = LivePollScheduler { clock }
            val attempts = mutableMapOf<Int, Int>()
            repeat(120) { index ->
                val started = clock
                val due = ObdPollPlan.requestedPids(index.toLong(), supported)
                val cycle = scheduler.beginCycle(index.toLong(), due, started)
                var commands = 0
                while (true) {
                    when (val step = cycle.next()) {
                        LivePollStep.Complete -> break
                        LivePollStep.ReadVoltage -> { commands += 1; clock += 610L; scheduler.observeCommand(610L) }
                        is LivePollStep.QueryPid -> {
                            cycle.attempted(step.pid)
                            commands += 1
                            attempts[step.pid] = (attempts[step.pid] ?: 0) + 1
                            clock += 610L
                            scheduler.observeCommand(610L)
                        }
                        is LivePollStep.Deferred -> Unit
                        is LivePollStep.Interrupted -> error("unexpected interruption")
                    }
                }
                clock += tailMillis
                scheduler.observeTail(tailMillis)
                assertTrue("slow row tail overran at $index", clock - started <= 5_000L)
                assertTrue(commands <= if (tailMillis == 1_500L) 5 else 6)
                clock = started + 5_000L
            }
            val optional = (0L..11L).flatMap { ObdPollPlan.requestedPids(it, supported) }.toSet() - critical.toSet()
            val admitted = if (tailMillis == 1_500L) optional - setOf(0x05, 0x0F, 0x21) else optional
            assertTrue(admitted.all { (attempts[it] ?: 0) >= 3 })
            assertEquals(40, attempts[0x10])
            if (tailMillis == 1_500L) assertTrue((optional - admitted).all { (attempts[it] ?: 0) == 0 })
        }
    }

    @Test
    fun bothHighReservesExpireAfterSixteenNewObservationsRatherThanStarvingTheDrive() {
        val clock = 2_440L
        val scheduler = LivePollScheduler { clock }
        scheduler.observeCommand(10_000L)
        scheduler.observeTail(10_000L)
        fun firstOptional(): LivePollStep {
            val cycle = scheduler.beginCycle(0, ObdPollPlan.requestedPids(0, supported), 0)
            repeat(4) { cycle.next() }
            return cycle.next()
        }
        assertTrue(firstOptional() is LivePollStep.Deferred)
        repeat(16) { scheduler.observeCommand(610L) }
        assertTrue(scheduler.requiredCommandMillis() < 1_000L)
        assertTrue(firstOptional() is LivePollStep.Deferred) // slow row reserve still protects persistence
        repeat(16) { scheduler.observeTail(150L) }
        assertTrue(firstOptional() is LivePollStep.QueryPid)
    }

    @Test
    fun transientOutlierCannotPermanentlyStarveOptionalPids() {
        var clock = 0L
        val scheduler = LivePollScheduler { clock }
        scheduler.observeCommand(2_900L)
        assertTrue(scheduler.requiredCommandMillis() > 3_000L)
        var optionalAttempts = 0
        repeat(6) { index ->
            val started = clock
            val cycle = scheduler.beginCycle(index.toLong(), ObdPollPlan.requestedPids(index.toLong(), supported), started)
            while (true) {
                when (val step = cycle.next()) {
                    LivePollStep.Complete -> break
                    LivePollStep.ReadVoltage -> { clock += 610L; scheduler.observeCommand(610L) }
                    is LivePollStep.QueryPid -> {
                        cycle.attempted(step.pid)
                        if (step.pid !in critical) optionalAttempts += 1
                        clock += 610L
                        scheduler.observeCommand(610L)
                    }
                    is LivePollStep.Deferred -> Unit
                    is LivePollStep.Interrupted -> error("unexpected interruption")
                }
            }
            clock = started + 5_000L
        }
        assertTrue(optionalAttempts > 0)
        assertTrue(scheduler.requiredCommandMillis() < 1_000L)
    }

    @Test
    fun reservedDiagnosticsFitTheWholeCycleAndKeepEveryDueMafOpportunity() {
        var clock = 0L
        val live = LivePollScheduler { clock }
        val sparse = SparseDiagnosticScheduler()
        val budget = SparseDiagnosticBudgetTracker().apply { observeCommand(2_900L) }
        val queue = OneStepPerCycleQueue<String>()
        val scan = listOf("readiness", "confirmed_dtcs", "pending_dtcs", "permanent_dtcs", "dtc_scan_complete")
        scan.forEach(queue::add)
        val completed = mutableListOf<String>()
        val mafCycles = mutableListOf<Long>()
        val scanCycles = mutableListOf<Long>()
        repeat(100) { index ->
            val sequence = index.toLong()
            val started = clock
            val reserved = sparse.reserveCycle(sequence, !queue.isEmpty)
            val due = ObdPollPlan.requestedPids(sequence, supported, reserved)
            val cycle = live.beginCycle(sequence, due, started)
            while (true) {
                when (val step = cycle.next()) {
                    LivePollStep.Complete -> break
                    LivePollStep.ReadVoltage -> { clock += 650L; live.observeCommand(650L); budget.observeCommand(650L) }
                    is LivePollStep.QueryPid -> {
                        cycle.attempted(step.pid)
                        if (step.pid == 0x10) mafCycles += sequence
                        clock += 650L
                        live.observeCommand(650L)
                        budget.observeCommand(650L)
                    }
                    is LivePollStep.Deferred -> Unit
                    is LivePollStep.Interrupted -> error("unexpected interruption")
                }
            }
            clock += 150L // already-spent parsing and durable primary-row work
            if (ObdPollPlan.mayRunSparseDiagnostic(clock - started, budget.requiredBudgetMillis(),
                    live.remainingTailMillis(150L))) {
                queue.take(sequence)?.let {
                    completed += it
                    scanCycles += sequence
                    sparse.observeAttempt(sequence)
                    assertTrue(sequence % 3 != 1L) // MAF wins this slot when both cannot fit.
                    clock += 650L
                    budget.observeCommand(650L)
                }
            }
            clock += 100L // status/handoff/timing writes still have their reserve
            live.observeTail(250L)
            assertTrue("reserved work overran at $sequence", clock - started <= 5_000L)
            clock = started + 5_000L
        }
        assertEquals(scan, completed)
        assertEquals(listOf(3L, 29L, 42L, 68L, 81L), scanCycles)
        assertEquals((0L..99L).filter { it % 3 == 1L }, mafCycles)
    }

    @Test
    fun tailBudgetCountsOnlyOutstandingWorkAndKeepsAPostSampleFloor() {
        val live = LivePollScheduler { 0L }
        assertEquals(1_350L, live.remainingTailMillis(150L)) // unknown initial aggregate is conservative
        live.observeTail(850L)
        assertEquals(750L, live.remainingTailMillis(200L)) // full learned aggregate plus100, less actual spent
        assertEquals(100L, live.remainingTailMillis(2_000L)) // no fabricated zero-cost completion
        assertEquals(950L, live.remainingTailMillis(-100L))
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(2_750, 1_550, live.remainingTailMillis(250L)))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(2_751, 1_550, live.remainingTailMillis(250L)))
    }

    @Test
    fun quiescePreemptsBeforeTheNextCommandAndPreservesUnattemptedMissingPids() {
        val scheduler = LivePollScheduler { 0L }
        val requested = ObdPollPlan.requestedPids(0, supported)
        val cycle = scheduler.beginCycle(0, requested, 0)
        assertEquals(LivePollStep.QueryPid(critical.first()), cycle.next())
        val interrupted = cycle.next(preempted = true) as LivePollStep.Interrupted
        assertEquals((requested - critical.first()).toSet(), interrupted.remainingPids.toSet())
        assertTrue(cycle.next(preempted = true) is LivePollStep.Interrupted)
    }

    @Test
    fun waitingKeepsTheFiveSecondTargetButQuiesceInterruptsWithinOneCheck() = runTest {
        awaitLivePollDeadline(0, { testScheduler.currentTime }, { false })
        assertEquals(5_000L, testScheduler.currentTime)
        val secondStart = testScheduler.currentTime
        awaitLivePollDeadline(secondStart, { testScheduler.currentTime }, { testScheduler.currentTime >= secondStart + 100 })
        assertEquals(secondStart + 250L, testScheduler.currentTime)
        var checked = false
        awaitLivePollDeadline(secondStart, { testScheduler.currentTime }, { checked = true; true })
        assertTrue(checked)
        assertFalse(testScheduler.currentTime >= secondStart + 5_000L)
    }
}
