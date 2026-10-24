package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import java.time.Instant
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
class PollTrialTest {
    private fun criticalOutcomes(vararg changes: Pair<Int, CriticalPollOutcome>): Map<Int, CriticalPollOutcome> =
        PollTrialPlan.critical.associateWith { CriticalPollOutcome.SUCCESS } + changes

    @Test fun baselineIsDefaultAndMissingRequiredPidCannotSelectTrial() {
        assertEquals(7, PollTrialPlan.select(false, PollTrialPlan.critical.toSet()))
        PollTrialPlan.critical.forEach { absent ->
            assertEquals(7, PollTrialPlan.select(true, PollTrialPlan.critical.toSet() - absent))
        }
        assertEquals(8, PollTrialPlan.select(true, PollTrialPlan.critical.toSet()))
        assertEquals(listOf(0x0D, 0x0C, 0x10), PollTrialPlan.critical)
        assertEquals(5_000L, ObdPollPlan.TARGET_CYCLE_MILLIS)
        assertEquals(7, ObdPollPlan.VERSION)
    }

    @Test fun productionBoundarySelectionDisablesUnsupportedTrialAndNeverChangesActivePlan() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val prefs = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        try {
            assertEquals(7, LoggerPreferences.pollPlanForNewDrive(context, PollTrialPlan.critical.toSet()))
            LoggerPreferences.setPollTrial(context, true)
            val activePlan = LoggerPreferences.pollPlanForNewDrive(context, PollTrialPlan.critical.toSet())
            assertEquals(8, activePlan)
            LoggerPreferences.setPollTrial(context, false)
            assertEquals(8, activePlan)
            assertEquals(7, LoggerPreferences.pollPlanForNewDrive(context, PollTrialPlan.critical.toSet()))
            LoggerPreferences.setPollTrial(context, true)
            assertEquals(7, LoggerPreferences.pollPlanForNewDrive(context, setOf(0x0C, 0x0D)))
            assertEquals("required_pids_unavailable", LoggerPreferences.load(context, false).pollTrialFallbackReason)
            assertFalse(LoggerPreferences.load(context, false).pollTrialEnabled)
            assertEquals(7, LoggerPreferences.pollPlanForNewDrive(context, PollTrialPlan.critical.toSet()))
        } finally { prefs.edit().clear().commit() }
    }

    @Test fun uncertainTransportTriggersFallbackButDeliberateStopsNeverDo() {
        assertTrue(PollTrialPlan.transportFault(ElmCommandTimeoutException("bounded fixture"), false))
        assertTrue(PollTrialPlan.transportFault(ElmException("bounded fixture"), false))
        assertTrue(PollTrialPlan.transportFault(ElmProtocolException("bounded fixture"), true))
        assertFalse(PollTrialPlan.transportFault(kotlinx.coroutines.CancellationException("stop"), true))
        assertFalse(PollTrialPlan.transportFault(ElmQuiesceRequestedException(), true))
        assertFalse(PollTrialPlan.transportFault(ElmProtocolException("malformed PID only"), false))
        assertFalse(PollTrialPlan.transportFault(ElmCommandRejectedException("unsupported"), false))
    }

    @Test fun failedFallbackCommitBlocksConnectionUntilDisabledPreferenceIsDurable() {
        val latch = PollTrialFallbackLatch()
        assertThrows(IllegalStateException::class.java) {
            latch.disable("transport_fault") { error("simulated disk failure") }
        }
        var connections = 0
        assertThrows(IllegalStateException::class.java) {
            latch.beforeConnection { error("still unavailable") }
            connections++
        }
        assertEquals(0, connections)
        var stored: String? = null
        latch.beforeConnection { stored = it }
        connections++
        assertEquals("transport_fault", stored)
        assertEquals(1, connections)
        latch.beforeConnection { error("no pending write after successful commit") }
    }

    @Test fun trialDeadlineKeepsThreeSecondTargetAndQuiesceInterruptsIdle() = runTest {
        awaitLivePollDeadline(0, { testScheduler.currentTime }, { false }, PollTrialPlan.TARGET_MILLIS)
        assertEquals(3_000L, testScheduler.currentTime)
        awaitLivePollDeadline(3_000, { testScheduler.currentTime }, { testScheduler.currentTime >= 3_250 }, PollTrialPlan.TARGET_MILLIS)
        assertEquals(3_250L, testScheduler.currentTime)
    }

    @Test fun completedHealthRequiresThreeConsecutiveFailuresAndResets() {
        val success = criticalOutcomes()
        val health = PollTrialHealth()
        assertNull(health.completed(3_501, success, 0))
        assertNull(health.completed(3_501, success, 3_501))
        assertNull(health.completed(3_000, success, 7_001))
        repeat(2) { assertNull(health.completed(3_501, success, 10_502L + it * 3_501)) }
        assertEquals("sustained_overrun", health.completed(3_501, success, 17_504))
        health.reset()
        val missingMaf = criticalOutcomes(0x10 to CriticalPollOutcome.MISSING)
        repeat(2) { assertNull(health.completed(1_900, missingMaf, it * 3_000L)) }
        assertNull(health.completed(1_900, success, 6_000))
        val missingSpeed = criticalOutcomes(0x0D to CriticalPollOutcome.MISSING)
        repeat(2) { assertNull(health.completed(1_900, missingSpeed, 9_000L + it * 3_000)) }
        assertEquals("critical_readings_missing", health.completed(1_900, missingSpeed, 15_000))
        health.reset() // The normal shutdown grace period does not contribute trial failures.
        assertNull(health.completed(1_900, missingSpeed, 18_000))
    }

    @Test fun measuredThreeSecondTargetMissesStayVisibleWithoutDisablingHealthyTrial() {
        var at = 0L
        val health = PollTrialHealth()
        val timing = PollTiming(targetCycleMillis = PollTrialPlan.TARGET_MILLIS) { at }
        // The latest 320-row drive had no transport timeout. Its final 3.058s work
        // cycle included a 672ms sparse diagnostic and used to latch the trial off.
        repeat(320) {
            timing.startCycle()
            timing.measureStage(PollCycleStage.CRITICAL_REQUESTS) { at += 1_906 }
            timing.measureStage(PollCycleStage.QUIESCE_CHECKS) { at += 408 }
            timing.measureStage(PollCycleStage.DIAGNOSTICS) { at += 672 }
            at += 72
            timing.finishCycle(completed = true)
            assertNull(health.completed(3_058, criticalOutcomes(), at))
        }
        val body = timing.drain("drive_end")!!
        assertEquals(3_000L, body.getLong("target_cycle_ms"))
        assertEquals(320L, body.getLong("overrun_count"))
        assertEquals(3_058L, body.getJSONObject("cycle_work_ms").getLong("max_ms"))
    }

    @Test fun jitterAllowanceNeverExemptsMissingReadingsOrRepeatedSlowTrio() {
        val health = PollTrialHealth()
        val missingMaf = criticalOutcomes(0x10 to CriticalPollOutcome.MISSING)
        repeat(2) { assertNull(health.completed(3_058, missingMaf, it * 3_058L)) }
        assertEquals("critical_readings_missing", health.completed(3_058, missingMaf, 6_116))
        health.reset()
        // Three genuine 1.2s ECU reads already exceed the health ceiling, even
        // with no optional/diagnostic request and all response values present.
        repeat(2) { assertNull(health.completed(3_700, criticalOutcomes(), it * 3_700L)) }
        assertEquals("sustained_overrun", health.completed(3_700, criticalOutcomes(), 7_400))
    }

    @Test fun healthMarginBoundaryResetsOverloadStreakWithoutChangingTarget() {
        val health = PollTrialHealth()
        repeat(2) { assertNull(health.completed(3_501, criticalOutcomes(), it * 3_501L)) }
        assertNull(health.completed(3_500, criticalOutcomes(), 10_502))
        repeat(2) { assertNull(health.completed(3_501, criticalOutcomes(), 14_003L + it * 3_501L)) }
        assertEquals("sustained_overrun", health.completed(3_501, criticalOutcomes(), 21_005))
        assertEquals(3_000L, PollTrialPlan.TARGET_MILLIS)
        assertEquals(3_500L, PollTrialPlan.HEALTH_WORK_LIMIT_MILLIS)
    }

    @Test fun absentQuiesceChecksCollapseOnlyForOneHundredMillisAndNewSessionsReadAgain() {
        var at = 0L
        var reads = 0
        var request: IngestionRequestRead? = null
        val read = { reads++; request }
        val reader = TrialQuiesceReader({ at }, read)
        assertNull(reader.read())
        at = 99
        assertNull(reader.read())
        assertEquals(1, reads)
        request = IngestionRequestRead.Invalid("bounded fixture")
        at = 100
        assertSame(request, reader.read())
        assertSame(request, reader.read()) // Invalid/active requests never use the cache.
        assertEquals(3, reads)
        request = null
        assertNull(reader.read())
        assertNull(TrialQuiesceReader({ at }, read).read()) // New connection/session.
        assertEquals(5, reads)
        at = 99 // Monotonic regression must force a fresh check as well.
        assertNull(reader.read())
        assertEquals(6, reads)
    }

    @Test fun activeQuiesceAndReadFailuresAreNeverCached() {
        var at = 0L
        var reads = 0
        var fail = false
        val active = IngestionRequestRead.Valid(IngestionRequest("fixture", "2026-10-24T00:00:00Z", "2026-10-24T00:01:00Z"))
        var request: IngestionRequestRead? = null
        val reader = TrialQuiesceReader({ at }) {
            reads++
            if (fail) error("read failure")
            request
        }
        assertNull(reader.read())
        at = 100
        fail = true
        assertThrows(IllegalStateException::class.java) { reader.read() }
        fail = false
        request = active
        assertSame(active, reader.read())
        assertSame(active, reader.read())
        assertEquals(4, reads)
    }

    @Test fun slowQuiesceMetadataDoesNotExtendAbsentProofLifetime() {
        var at = 0L
        var reads = 0
        val reader = TrialQuiesceReader({ at }) { reads++; at += 100; null }
        assertNull(reader.read())
        assertEquals(100L, at)
        assertNull(reader.read()) // No additional cached lifetime after the slow read.
        assertEquals(2, reads)
    }

    @Test fun diagnosticAdmissionUsesFullTailAndDeferredReservationsDoNotStarveAuxiliaryWork() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        repeat(3) { scheduler.observeCommand(636) }
        scheduler.observeTail(40)
        val schedule = PollTrialDiagnosticSchedule(at)
        at = 15_000
        assertTrue(schedule.reserve(at, true))
        // Primary + quiesce/persistence consumed 2.386s before the old 672ms
        // probe began. It cannot fit, even before learning a more expensive tail.
        at += 2_386
        assertFalse(scheduler.mayRun(15_000))
        schedule.observedReservation(at, attempted = false)
        assertFalse(schedule.reserve(18_000, true))
        at = 19_900
        assertTrue(scheduler.mayRunCommand(0x04, 18_000, diagnosticReserved = false))
        assertFalse(schedule.reserve(32_385, true))
        assertTrue(schedule.reserve(32_386, true))
        schedule.observedReservation(32_386, attempted = true)
        assertFalse(schedule.reserve(97_385, true))
        assertTrue(schedule.reserve(97_386, true))
        assertFalse(schedule.reserve(97_386, false))
        // The actual complete non-diagnostic tail, rather than SQLite alone,
        // must reserve room for status/quiesce/timing writes before another probe.
        at = 2_100
        val fullTailScheduler = PollTrialScheduler { at }
        fullTailScheduler.observeCommand(636)
        fullTailScheduler.observeTail(40)
        assertTrue(fullTailScheduler.mayRun(0))
        fullTailScheduler.observeTail(300)
        assertFalse(fullTailScheduler.mayRun(0))
    }

    @Test fun observedFiftySevenSecondMafFailureGetsItsScheduledPlainRetry() = runTest {
        // Retained row end times from the 22 October drive: 14 valid MAF rows,
        // one malformed reply, then two cooldown slots ended the old trial at 57.311s.
        val rowEnds = listOf(3_586L, 7_434, 10_290, 13_375, 16_696, 19_972, 24_078,
            27_876, 31_847, 34_520, 38_237, 41_785, 44_397, 48_004, 50_624, 54_066, 57_311)
        val health = PollTrialHealth()
        val tracker = LivePidMalformedTracker()
        val reader = ElmLivePidReader()
        val requests = mutableListOf<String>()
        val missingRows = mutableListOf<Int>()
        for ((index, endedAt) in rowEnds.withIndex()) {
            val outcome = when {
                index < 14 -> CriticalPollOutcome.SUCCESS
                index == 14 -> {
                    val malformed = runCatching {
                        reader.queryPayload(0x10, 0x10) { command ->
                            requests += command
                            mafReply(checksumDelta = 1)
                        }
                    }.exceptionOrNull()
                    assertTrue(malformed is ElmProtocolException)
                    tracker.recordMalformed(0x10, PollTrialPlan.cooldownCycle(47_624))
                    CriticalPollOutcome.MALFORMED
                }
                else -> {
                    val startedAt = rowEnds[index - 1]
                    assertFalse(tracker.shouldPoll(0x10, PollTrialPlan.cooldownCycle(startedAt)))
                    CriticalPollOutcome.COOLDOWN
                }
            }
            if (outcome != CriticalPollOutcome.SUCCESS) missingRows += index
            // Isolate missing health from the independent sustained-work deadline.
            assertNull(health.completed(2_934, criticalOutcomes(0x10 to outcome), endedAt))
        }
        assertEquals(listOf(14, 15, 16), missingRows) // No carried-forward MAF or fuel reading.
        assertEquals(listOf("01101"), requests) // Cooldowns performed no hidden retry.
        assertTrue(tracker.shouldPoll(0x10, PollTrialPlan.cooldownCycle(rowEnds.last())))
        val recovered = reader.queryPayload(0x10, 0x10) { command ->
            requests += command
            mafReply()
        }!!
        assertEquals(mapOf("mass_air_flow" to 3.0), ElmProtocol.decode(0x10, recovered))
        tracker.recordValid(0x10)
        assertNull(health.completed(2_700, criticalOutcomes(), 60_011))
        assertEquals(listOf("01101", "0110"), requests)
    }

    @Test fun cooldownCannotSuppressAnotherCriticalFailureOrResetItsStreak() {
        val health = PollTrialHealth()
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 0))
        val otherFailure = criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN, 0x0D to CriticalPollOutcome.MISSING)
        assertNull(health.completed(2_500, otherFailure, 3_000))
        assertEquals("critical_readings_missing", health.completed(2_500, otherFailure, 6_000))
    }

    @Test fun malformedCooldownHasAnAbsoluteBoundAndAnotherMalformedReplyCannotExtendIt() {
        val health = PollTrialHealth()
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 1_000))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN), 10_999))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 11_000))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN), 15_999))
        assertEquals("critical_readings_missing", health.completed(2_500,
            criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN), 16_000))
    }

    @Test fun actualMissingRetryAndAbsentTimingStillCountAsFailedAttempts() {
        val health = PollTrialHealth()
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 0))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN), 6_000))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MISSING), 10_000))
        assertEquals("critical_readings_missing", health.completed(2_500,
            criticalOutcomes(0x10 to CriticalPollOutcome.UNPROVED), 13_000))
        health.reset()
        repeat(2) { assertNull(health.completed(2_500, criticalOutcomes() - 0x0C, it * 3_000L)) }
        assertEquals("critical_readings_missing", health.completed(2_500, criticalOutcomes() - 0x0C, 6_000))
    }

    @Test fun unjustifiedCooldownIsNotARecoveryExemptionAndSlowCyclesStillFallback() {
        val health = PollTrialHealth()
        val cooldown = criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN)
        repeat(2) { assertNull(health.completed(2_500, cooldown, it * 3_000L)) }
        assertEquals("critical_readings_missing", health.completed(2_500, cooldown, 6_000))
        health.reset()
        assertNull(health.completed(3_501, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 0))
        assertNull(health.completed(3_501, cooldown, 3_501))
        assertEquals("sustained_overrun", health.completed(3_501, cooldown, 7_002))
    }

    @Test fun successfulRecoveryAndShutdownResetClearTheMissingDeadline() {
        val health = PollTrialHealth()
        val malformed = criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED)
        assertNull(health.completed(2_500, malformed, 0))
        assertNull(health.completed(2_500, criticalOutcomes(), 12_000))
        assertNull(health.completed(2_500, malformed, 16_000))
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.COOLDOWN), 30_999))
        health.reset()
        assertNull(health.completed(2_500, malformed, 32_000))
    }

    @Test fun lateSuccessfulReplyCannotRetroactivelyExtendRecovery() {
        val health = PollTrialHealth()
        assertNull(health.completed(2_500, criticalOutcomes(0x10 to CriticalPollOutcome.MALFORMED), 1_000))
        assertEquals("critical_readings_missing", health.completed(2_500, criticalOutcomes(), 16_000))
    }

    private fun mafReply(checksumDelta: Int = 0): String {
        val body = listOf(0x48, 0x6B, 0x10, 0x41, 0x10, 0x01, 0x2C)
        return (body + ((body.sum() + checksumDelta) and 0xFF)).joinToString(" ") { "%02X".format(it) } + "\r>"
    }

    @Test fun wallClockDeadlinesDoNotCatchUpAndDeferredWorkKeepsFairPriority() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        val supported = PollTrialPlan.optional.toSet()
        assertEquals(listOf(PollTrialScheduler.VOLTAGE, 0x04), scheduler.duePids(supported))
        scheduler.attempted(PollTrialScheduler.VOLTAGE)
        scheduler.attempted(0x04)
        at = 4_999
        assertFalse(PollTrialScheduler.VOLTAGE in scheduler.duePids(supported))
        at = 5_000
        assertTrue(PollTrialScheduler.VOLTAGE in scheduler.duePids(supported))
        at = 100_000
        val overdue = scheduler.duePids(supported)
        assertTrue(overdue.indexOf(0x0E) < overdue.indexOf(0x04))
        scheduler.attempted(0x0E)
        assertFalse(0x0E in scheduler.duePids(supported))
        at = 159_999
        assertFalse(0x0E in scheduler.duePids(supported))
        at = 160_000
        assertTrue(0x0E in scheduler.duePids(supported))
        assertFalse(0x15 in scheduler.duePids(supported - 0x15))
    }

    @Test fun measuredNormalTrioCanAdmitAuxButSlowCommandOrTailDefersIt() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        repeat(3) { scheduler.observeCommand(630) }
        scheduler.observeTail(40)
        at = 1_890
        assertTrue(scheduler.mayRun(0))
        at += 630
        assertFalse(scheduler.mayRun(0))
        scheduler.observeCommand(1_400)
        at = 1_890
        assertFalse(scheduler.mayRun(0))
        val slowTail = PollTrialScheduler { at }
        slowTail.observeCommand(630)
        slowTail.observeTail(800)
        assertFalse(slowTail.mayRun(0))
    }

    @Test fun productionAdmissionAlwaysServicesDueVoltageBeforeAuxEvenWithExhaustedBudget() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        val supported = PollTrialPlan.optional.toSet()
        val commands = scheduler.commands(supported)
        assertEquals(PollTrialPlan.critical + listOf(PollTrialScheduler.VOLTAGE, 0x04), commands)
        at = 2_900
        assertTrue(scheduler.mayRunCommand(PollTrialScheduler.VOLTAGE, 0, false))
        assertFalse(scheduler.mayRunCommand(0x04, 0, false))
        scheduler.attempted(PollTrialScheduler.VOLTAGE)
        at = 8_900
        assertEquals(PollTrialScheduler.VOLTAGE, scheduler.commands(supported)[3])
        // Diagnostic reservations replace only optional probes, retaining all critical reads.
        assertTrue(scheduler.mayRunCommand(0x10, 0, true))
        assertTrue(scheduler.mayRunCommand(PollTrialScheduler.VOLTAGE, 0, true))
        assertFalse(scheduler.mayRunCommand(0x04, 8_900, true))
    }

    @Test fun selectingAuxAfterTrioServicesFiveSecondVoltageDeadlineOnSixSecondCycle() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        at = 1_860 // The first trio has completed; the first ATRV can start now.
        scheduler.attempted(PollTrialScheduler.VOLTAGE)
        at = 6_000 // The third cycle starts before the voltage deadline.
        assertFalse(PollTrialScheduler.VOLTAGE in scheduler.duePids(emptySet()))
        at += 1_860 // Select auxiliary work after the trio, as the production loop does.
        assertEquals(PollTrialScheduler.VOLTAGE, scheduler.commands(emptySet())[3])
        scheduler.attempted(PollTrialScheduler.VOLTAGE)
        at = 13_860
        assertEquals(PollTrialScheduler.VOLTAGE, scheduler.commands(emptySet())[3])
    }

    @Test fun malformedCriticalCooldownUsesBaselineWallClockNotFasterRows() {
        val malformed = LivePidMalformedTracker()
        malformed.recordMalformed(0x10, PollTrialPlan.cooldownCycle(0))
        assertFalse(malformed.shouldPoll(0x10, PollTrialPlan.cooldownCycle(3_000)))
        assertFalse(malformed.shouldPoll(0x10, PollTrialPlan.cooldownCycle(9_999)))
        assertTrue(malformed.shouldPoll(0x10, PollTrialPlan.cooldownCycle(10_000)))
    }

    @Test fun onlyFreshLowVoltageAndStoppedRpmExcludeNormalShutdownFromFallback() {
        assertTrue(PollTrialPlan.shuttingDown(10_000, 9_000, 12.5, 13.0, null))
        assertTrue(PollTrialPlan.shuttingDown(10_000, 9_000, 12.5, 13.0, 0.0))
        assertFalse(PollTrialPlan.shuttingDown(20_000, 9_000, 12.5, 13.0, null))
        assertFalse(PollTrialPlan.shuttingDown(10_000, 9_000, 12.5, 13.0, 800.0))
        assertFalse(PollTrialPlan.shuttingDown(10_000, null, null, 13.0, null))
        assertFalse(PollTrialPlan.shuttingDown(10_000, 9_000, 14.0, 13.0, null))
    }

    @Test fun longRunAuxiliaryAttemptsAreFairAndNeverBurst() {
        var at = 0L
        val scheduler = PollTrialScheduler { at }
        val attempts = mutableMapOf<Int, MutableList<Long>>()
        repeat(240) {
            val started = at
            repeat(3) { at += 620; scheduler.observeCommand(620) }
            for (pid in scheduler.commands(PollTrialPlan.optional.toSet()).drop(3)) {
                if (!scheduler.mayRunCommand(pid, started, false)) continue
                scheduler.attempted(pid)
                attempts.getOrPut(pid) { mutableListOf() }.add(at)
                at += 620
                scheduler.observeCommand(620)
            }
            scheduler.observeTail(40)
            at = maxOf(at + 40, started + 3_000)
        }
        assertEquals((PollTrialPlan.optional + PollTrialScheduler.VOLTAGE).toSet(), attempts.keys)
        attempts.forEach { (pid, times) ->
            val minimum = when (pid) { PollTrialScheduler.VOLTAGE -> 5_000; 0x04 -> 15_000; else -> 60_000 }
            assertTrue("PID $pid was starved", times.size >= 5)
            assertTrue("PID $pid caught up", times.zipWithNext().all { (a, b) -> b - a >= minimum })
        }
    }

    @Test fun atomicCaptureUsesOneMonotonicInstantForRowAndResponseAges() = runTest {
        var at = 1_000L
        var reads = 0
        val clock = MonotonicUtcClock(Instant.parse("2026-10-22T00:00:00Z"), 1_000) { reads++; at }
        val timing = PollTiming(targetCycleMillis = 3_000) { at }
        timing.startCycle()
        timedLivePidPoll(0x0D, timing) { at += 620; mapOf("vehicle_speed" to 30) }
        timedLivePidPoll(0x0C, timing) { at += 630; mapOf("engine_rpm" to 900) }
        timedLivePidPoll(0x10, timing) { at += 640; mapOf("mass_air_flow" to 3.0) }
        val capture = clock.capture()
        assertEquals(1, reads)
        assertEquals("2026-10-22T00:00:01.890Z", capture.timestampUtc)
        assertEquals(mapOf(0x0D to 1_270L, 0x0C to 640L, 0x10 to 0L), timing.takeRowAges(capture.elapsedMillis))
        assertTrue(timing.takeRowAges(capture.elapsedMillis).isEmpty())
        timing.finishCycle(completed = true)
        assertEquals(3_000L, timing.drain("drive_end")!!.getLong("target_cycle_ms"))
    }

    @Test fun suspendedRowDropsUnprovedValuesAndRecomputesDerivedFuelWithoutClamping() {
        val observed = mapOf<String, Any>("vehicle_speed" to 30.0, "engine_rpm" to 900.0,
            "mass_air_flow" to 3.0, "estimated_fuel_rate" to 999.0, "estimated_fuel_consumption" to 999.0,
            "oxygen_sensor_1_voltage" to 0.7, "oxygen_sensor_1_short_term_fuel_trim" to 1.0,
            "adapter_voltage" to 14.0, "obd_standard" to "EOBD")
        val fields = mapOf(0x0D to setOf("vehicle_speed"), 0x0C to setOf("engine_rpm"),
            0x10 to setOf("mass_air_flow"), 0x14 to setOf("oxygen_sensor_1_voltage", "oxygen_sensor_1_short_term_fuel_trim"))
        val prepared = prepareTimedTrialRow(observed, fields,
            mapOf(0x0D to 60_001L, 0x0C to 0L, 0x10 to 60_000L, 0x14 to -1L), listOf(0x05))
        assertEquals(mapOf(0x0C to 0L, 0x10 to 60_000L), prepared.ages)
        assertEquals(setOf(0x05, 0x0D, 0x14), prepared.missing.toSet())
        assertFalse(prepared.values.containsKey("vehicle_speed"))
        assertFalse(prepared.values.containsKey("oxygen_sensor_1_voltage"))
        assertFalse(prepared.values.containsKey("oxygen_sensor_1_short_term_fuel_trim"))
        assertFalse(prepared.values.containsKey("estimated_fuel_consumption"))
        assertEquals(ElmProtocol.estimates(mapOf("mass_air_flow" to 3.0))["estimated_fuel_rate"], prepared.values["estimated_fuel_rate"])
        assertEquals(14.0, prepared.values["adapter_voltage"])
        assertEquals("EOBD", prepared.values["obd_standard"])
        assertTrue(observed.containsKey("vehicle_speed"))
    }

    @Test fun partialRowNeverRetainsOrphanMafOrItsDerivedOutputs() {
        val observed = mapOf<String, Any>("vehicle_speed" to 30.0, "mass_air_flow" to 3.0,
            "estimated_fuel_rate" to 1.0, "estimated_fuel_consumption" to 2.0)
        val prepared = prepareTimedTrialRow(observed,
            mapOf(0x0D to setOf("vehicle_speed"), 0x10 to setOf("mass_air_flow")),
            mapOf(0x0D to 1_000L), listOf(0x0C))
        assertEquals(mapOf(0x0D to 1_000L), prepared.ages)
        assertEquals(setOf(0x0C, 0x10), prepared.missing.toSet())
        assertEquals(mapOf<String, Any>("vehicle_speed" to 30.0), prepared.values)
    }

    @Test fun fallbackPreferenceIsDurableBoundedAndExplicitRearmClearsReason() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val prefs = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        try {
            assertFalse(LoggerPreferences.load(context, false).pollTrialEnabled)
            LoggerPreferences.setPollTrial(context, true)
            assertTrue(LoggerPreferences.load(context, false).pollTrialEnabled)
            LoggerPreferences.setPollTrial(context, false, "transport_fault")
            assertFalse(LoggerPreferences.load(context, false).pollTrialEnabled)
            assertEquals("transport_fault", LoggerPreferences.load(context, false).pollTrialFallbackReason)
            assertThrows(IllegalArgumentException::class.java) { LoggerPreferences.setPollTrial(context, false, "unbounded raw reply") }
            assertEquals("transport_fault", LoggerPreferences.load(context, false).pollTrialFallbackReason)
            val staleConfig = LoggerPreferences.load(context, false).copy(pollTrialEnabled = true, pollTrialFallbackReason = null)
            LoggerPreferences.save(context, staleConfig)
            assertFalse(LoggerPreferences.load(context, false).pollTrialEnabled)
            assertEquals("transport_fault", LoggerPreferences.load(context, false).pollTrialFallbackReason)
            LoggerPreferences.setPollTrial(context, true)
            assertNull(LoggerPreferences.load(context, false).pollTrialFallbackReason)
            val terminal = driveTerminalPolicy("poll_trial_fallback")
            assertEquals("interrupted", terminal.status)
            assertFalse(terminal.cleanEnd)
        } finally { prefs.edit().clear().commit() }
    }

    @Test fun qualityAgesRoundTripWithoutChangingLegacyShape() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        // The production DB constructor intentionally owns its fixed database name.
        val database = ObdDatabase(context)
        val id = "trial-quality-${System.nanoTime()}"
        try {
            database.startDrive(DriveRecord(id, "test", "test", "test", "0.3.12", startedAtUtc =
                "2026-10-22T00:00:00Z", originalTimezone = "UTC", startReason = "test", obdProtocol = "test", pollPlanVersion = 8))
            assertTrue(database.addSample(SampleRecord(id, 0, "2026-10-22T00:00:03Z", mapOf("vehicle_speed" to 30, "engine_rpm" to 900, "mass_air_flow" to 3.0),
                pidResponseAgeMs = mapOf(0x0D to 1_200L, 0x0C to 600L, 0x10 to 0L))))
            assertTrue(database.addSample(SampleRecord(id, 1, "2026-10-22T00:00:06Z", emptyMap())))
            val rows = database.samples(id)
            val quality = JSONObject(rows[0].getString("quality_json"))
            assertEquals(setOf("0D", "0C", "10"), quality.getJSONObject("pid_response_age_ms").keys().asSequence().toSet())
            assertEquals(1_200L, quality.getJSONObject("pid_response_age_ms").getLong("0D"))
            assertFalse(JSONObject(rows[1].getString("quality_json")).has("pid_response_age_ms"))
        } finally { database.close() }
    }
}
