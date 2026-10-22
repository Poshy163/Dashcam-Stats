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
        val success = PollTrialPlan.critical.toSet()
        val health = PollTrialHealth()
        assertNull(health.completed(3_001, success))
        assertNull(health.completed(3_001, success))
        assertNull(health.completed(3_000, success))
        repeat(2) { assertNull(health.completed(3_001, success)) }
        assertEquals("sustained_overrun", health.completed(3_001, success))
        health.reset()
        repeat(2) { assertNull(health.completed(1_900, success - 0x10)) }
        assertNull(health.completed(1_900, success))
        repeat(2) { assertNull(health.completed(1_900, success - 0x0D)) }
        assertEquals("critical_readings_missing", health.completed(1_900, success - 0x0D))
        health.reset() // The normal shutdown grace period does not contribute trial failures.
        assertNull(health.completed(1_900, success - 0x0D))
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
