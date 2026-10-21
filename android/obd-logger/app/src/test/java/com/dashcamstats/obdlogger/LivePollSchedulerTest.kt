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
                scheduler.observePrimaryTail(150L)

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
            assertEquals(optional, seen.keys - critical.toSet())
            optional.forEach { pid ->
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
        scheduler.observePrimaryTail(800L)
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
                scheduler.observePrimaryTail(tailMillis)
                assertTrue("slow row tail overran at $index", clock - started <= 5_000L)
                assertTrue(commands <= if (tailMillis == 1_500L) 5 else 6)
                clock = started + 5_000L
            }
            val optional = (0L..11L).flatMap { ObdPollPlan.requestedPids(it, supported) }.toSet() - critical.toSet()
            assertTrue(optional.all { (attempts[it] ?: 0) >= 3 })
        }
    }

    @Test
    fun bothHighReservesExpireAfterSixteenNewObservationsRatherThanStarvingTheDrive() {
        val clock = 2_440L
        val scheduler = LivePollScheduler { clock }
        scheduler.observeCommand(10_000L)
        scheduler.observePrimaryTail(10_000L)
        fun firstOptional(): LivePollStep {
            val cycle = scheduler.beginCycle(0, ObdPollPlan.requestedPids(0, supported), 0)
            repeat(4) { cycle.next() }
            return cycle.next()
        }
        assertTrue(firstOptional() is LivePollStep.Deferred)
        repeat(16) { scheduler.observeCommand(610L) }
        assertTrue(scheduler.requiredCommandMillis() < 1_000L)
        assertTrue(firstOptional() is LivePollStep.Deferred) // slow row reserve still protects persistence
        repeat(16) { scheduler.observePrimaryTail(150L) }
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
    fun sparseScanStillFinishesAndItsSlowReservedCyclesRemainHonestOverruns() {
        var clock = 0L
        val live = LivePollScheduler { clock }
        val sparse = SparseDiagnosticScheduler()
        val budget = SparseDiagnosticBudgetTracker().apply { observeCommand(2_900L) }
        val queue = OneStepPerCycleQueue<String>()
        val scan = listOf("readiness", "confirmed_dtcs", "pending_dtcs", "permanent_dtcs", "dtc_scan_complete")
        scan.forEach(queue::add)
        val completed = mutableListOf<String>()
        val scanCycles = mutableListOf<Long>()
        var overruns = 0
        repeat(60) { index ->
            val sequence = index.toLong()
            val started = clock
            val reserved = sparse.reserveCycle(sequence, !queue.isEmpty)
            val cycle = live.beginCycle(sequence, ObdPollPlan.requestedPids(sequence, supported, reserved), started)
            var attempts = 0
            while (true) {
                when (val step = cycle.next()) {
                    LivePollStep.Complete -> break
                    LivePollStep.ReadVoltage -> { clock += 610L; live.observeCommand(610L) }
                    is LivePollStep.QueryPid -> {
                        attempts += 1
                        cycle.attempted(step.pid)
                        clock += 610L
                        live.observeCommand(610L)
                    }
                    is LivePollStep.Deferred -> Unit
                    is LivePollStep.Interrupted -> error("unexpected interruption")
                }
            }
            clock += 150L // the primary row is already persisted before diagnostics
            live.observePrimaryTail(150L)
            if (ObdPollPlan.mayRunSparseDiagnostic(clock - started, budget.requiredBudgetMillis(), reserved)) {
                queue.take(sequence)?.let {
                    completed += it
                    scanCycles += sequence
                    sparse.observeAttempt(sequence)
                    assertEquals(3, attempts)
                    clock += 6_000L
                }
            }
            if (clock - started > 5_000L) overruns += 1
            clock = maxOf(clock, started + 5_000L)
        }
        assertEquals(scan, completed)
        assertEquals(listOf(3L, 16L, 29L, 42L, 55L), scanCycles)
        assertEquals(scan.size, overruns)
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
