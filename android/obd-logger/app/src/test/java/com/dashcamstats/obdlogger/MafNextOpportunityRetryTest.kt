package com.dashcamstats.obdlogger

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class MafNextOpportunityRetryTest {
    private fun tracker() = LivePidMalformedTracker(nextOpportunityRetryPids = setOf(0x10))
    private fun outcomes(maf: CriticalPollOutcome) = PollTrialPlan.critical.associateWith {
        if (it == 0x10) maf else CriticalPollOutcome.SUCCESS
    }

    @Test fun retryNeedsANewScheduledRowEvenWhenThreeSecondRowsShareAnElapsedBucket() {
        val tracker = tracker()
        tracker.recordMalformed(0x10, cycle = 4, opportunity = 7)
        assertFalse(tracker.shouldPoll(0x10, cycle = 4, opportunity = 7))
        assertFalse(tracker.shouldPoll(0x10, cycle = 4, opportunity = 6))
        assertFalse(tracker.shouldPoll(0x10, cycle = 3, opportunity = 8))
        assertTrue(tracker.shouldPoll(0x10, cycle = 4, opportunity = 8))
        // Reading eligibility does not issue or repeat any command itself.
        assertTrue(tracker.shouldPoll(0x10, cycle = 5, opportunity = 8))
        assertThrows(IllegalArgumentException::class.java) {
            LivePidMalformedTracker(nextOpportunityRetryPids = setOf(0x0D))
        }
    }

    @Test fun retainedEightMalformedMafOpportunitiesNoLongerNeedTenSecondArtificialCooldown() {
        // Actual retained 0.3.15 row sequences and row-end offsets (milliseconds).
        // No raw rejected bytes were retained. These are opportunity/timing replays,
        // not a claim that the original malformed bytes decode successfully.
        val retained = listOf(
            longArrayOf(80, 251_528, 254_953, 83, 261_065),
            longArrayOf(111, 346_384, 348_213, 115, 357_909),
            longArrayOf(125, 388_718, 391_192, 128, 397_957),
            longArrayOf(177, 545_896, 547_680, 181, 557_282),
            longArrayOf(66, 206_334, 208_050, 69, 215_329),
            longArrayOf(96, 296_191, 299_832, 99, 306_077),
            longArrayOf(237, 731_285, 733_047, 240, 739_727),
            longArrayOf(490, 1_500_730, 1_503_863, 493, 1_510_001),
        )
        for ((failedSequence, failedEnd, nextEnd, oldRetrySequence, oldRetryEnd) in retained) {
            val tracker = tracker()
            // The row-start bucket is not retained. Exercise both possible adjacent
            // bucket alignments instead of manufacturing an exact start timestamp.
            for (shift in listOf(0L, -1L)) {
                val failedBucket = PollTrialPlan.cooldownCycle(failedEnd) + shift
                val nextBucket = maxOf(failedBucket, PollTrialPlan.cooldownCycle(nextEnd) + shift)
                tracker.recordValid(0x10)
                tracker.recordMalformed(0x10, failedBucket, opportunity = failedSequence)
                assertFalse(tracker.shouldPoll(0x10, failedBucket, opportunity = failedSequence))
                assertTrue(tracker.shouldPoll(0x10, nextBucket, opportunity = failedSequence + 1))
                assertTrue(oldRetrySequence > failedSequence + 1)
                assertTrue(oldRetryEnd > nextEnd)
                val health = PollTrialHealth()
                assertNull(health.completed(2_800, outcomes(CriticalPollOutcome.MALFORMED), failedEnd))
                assertNull(health.completed(2_800, outcomes(CriticalPollOutcome.SUCCESS), nextEnd))
            }
        }
    }

    @Test fun freshPlainRetryKeepsBadRowMissingAndDoesNotBurstWithinTheFailedRow() = runTest {
        val reader = ElmLivePidReader()
        val tracker = tracker()
        val session = ElmCommandSession().apply { freshConnection() }
        val sent = mutableListOf<String>()
        var now = 0L
        val timing = PollTiming(targetCycleMillis = PollTrialPlan.TARGET_MILLIS) { now }
        // Deliberately synthetic ISO frames: representative MAF values with one
        // corrupted checksum followed by a separate new, fully valid response.
        val replies = ArrayDeque(listOf(iso(0x10, 0x04, 0x77, checksumDelta = 1), iso(0x10, 0x03, 0x62)))
        suspend fun query(pid: Int): Map<String, Any> = reader.queryPayload(pid, 0x10) { command ->
            sent += command
            session.beginCommand()
            now += 650
            val assembled = session.accept(replies.removeFirst().toByteArray())
            assertTrue(assembled is ElmResponseAssembly.Complete)
            (assembled as ElmResponseAssembly.Complete).response
        }!!.let { ElmProtocol.decode(pid, it) }

        timing.startCycle()
        val refused = timedLivePidPoll(0x10, timing, ::query)
        assertTrue(refused is LivePidPollResult.Malformed)
        tracker.recordMalformed(0x10, cycle = 0, opportunity = 0)
        assertFalse(tracker.shouldPoll(0x10, cycle = 0, opportunity = 0))
        assertTrue(timing.takeRowAges().isEmpty())
        assertEquals(listOf("01101"), sent)
        timing.finishCycle(completed = true)
        now = 3_000
        assertTrue(tracker.shouldPoll(0x10, cycle = PollTrialPlan.cooldownCycle(now), opportunity = 1))
        timing.startCycle()
        val retried = timedLivePidPoll(0x10, timing, ::query) as LivePidPollResult.Values
        assertEquals(mapOf("mass_air_flow" to 8.66), retried.decoded)
        assertEquals(mapOf(0x10 to 0L), timing.takeRowAges())
        tracker.recordValid(0x10)
        timing.finishCycle(completed = true)
        assertEquals(listOf("01101", "0110"), sent)
        assertFalse(session.isTainted)
        val stats = timing.drain("drive_end")!!.getJSONArray("pids").getJSONObject(0)
        assertEquals(2, stats.getInt("attempts"))
        assertEquals(1, stats.getInt("malformed"))
        assertEquals(1, stats.getInt("successes"))
        assertEquals(0, stats.getInt("cooldown_skips"))
    }

    @Test fun repeatedBadPlainRetryRetainsBreakerAndAbsoluteHealthDeadline() = runTest {
        val reader = ElmLivePidReader()
        val tracker = tracker()
        val health = PollTrialHealth()
        val sent = mutableListOf<String>()
        repeat(2) { row ->
            val at = row * 3_000L
            assertTrue(tracker.shouldPoll(0x10, PollTrialPlan.cooldownCycle(at), opportunity = row.toLong()))
            val failure = runCatching {
                reader.queryPayload(0x10, 0x10) { command ->
                    sent += command
                    iso(0x10, 0x04, 0x77, checksumDelta = 1)
                }
            }.exceptionOrNull()
            assertTrue(failure is ElmProtocolException)
            tracker.recordMalformed(0x10, PollTrialPlan.cooldownCycle(at), opportunity = row.toLong())
            assertNull(health.completed(2_800, outcomes(CriticalPollOutcome.MALFORMED), at))
        }
        for (row in 2L..4L) {
            val at = row * 3_000
            assertFalse(tracker.shouldPoll(0x10, PollTrialPlan.cooldownCycle(at), opportunity = row))
            assertNull(health.completed(2_800, outcomes(CriticalPollOutcome.COOLDOWN), at))
        }
        assertEquals("critical_readings_missing", health.completed(2_800,
            outcomes(CriticalPollOutcome.COOLDOWN), 15_000))
        assertEquals(listOf("01101", "0110"), sent)
        tracker.recordValid(0x10)
        tracker.recordMalformed(0x10, cycle = 8, opportunity = 20)
        assertTrue(tracker.shouldPoll(0x10, cycle = 8, opportunity = 21))
        // Other PIDs and baseline/default callers keep their established cooldown.
        tracker.recordMalformed(0x0D, cycle = 0, opportunity = 0)
        assertFalse(tracker.shouldPoll(0x0D, cycle = 0, opportunity = 1))
        val baseline = LivePidMalformedTracker()
        assertEquals(2L, baseline.recordMalformed(0x10, 0).retryAtCycle)
        assertFalse(baseline.shouldPoll(0x10, 1))
    }

    @Test fun nextOpportunityStillRejectsSyntheticWrongPidSourceLengthModeAndChecksum() = runTest {
        val invalid = listOf(
            iso(0x10, 0x04, 0x77, source = 0x11),
            iso(0x10, 0x04),
            iso(0x10, 0x04, 0x77, 0x00),
            iso(0x10, 0x04, 0x77, mode = 0x42),
            iso(0x0C, 0x04, 0x77),
            iso(0x10, 0x04, 0x77, checksumDelta = 1),
        )
        for (reply in invalid) {
            val reader = ElmLivePidReader()
            val commands = mutableListOf<String>()
            repeat(2) {
                assertTrue(runCatching {
                    reader.queryPayload(0x10, 0x10) { command -> commands += command; reply }
                }.exceptionOrNull() is ElmProtocolException)
            }
            assertEquals(listOf("01101", "0110"), commands)
        }
        val stream = ElmCommandSession().apply { freshConnection(); beginCommand() }
        assertEquals(ElmResponseAssembly.TrailingData,
            stream.accept((iso(0x10, 0x04, 0x77) + "STALE\r>").toByteArray()))
        assertTrue(stream.isTainted)
    }

    @Test fun timeoutsCancellationQuiesceAndSustainedWorkNeverGainAHiddenMafRetry() = runTest {
        for (failure in listOf(ElmCommandTimeoutException("prompt timeout"),
            ElmQuiesceRequestedException(), CancellationException("stop"))) {
            val reader = ElmLivePidReader()
            var commands = 0
            val actual = runCatching {
                reader.queryPayload(0x10, 0x10) { commands++; throw failure }
            }.exceptionOrNull()
            assertSame(failure, actual)
            assertEquals(1, commands)
        }
        val health = PollTrialHealth()
        assertNull(health.completed(3_001, outcomes(CriticalPollOutcome.MALFORMED), 0))
        assertNull(health.completed(3_001, outcomes(CriticalPollOutcome.SUCCESS), 3_000))
        assertEquals("sustained_overrun",
            health.completed(3_001, outcomes(CriticalPollOutcome.SUCCESS), 6_000))
        val session = ElmCommandSession().apply { freshConnection(); beginCommand() }
        val timeout = runCatching {
            session.awaitResponse(kotlinx.coroutines.CompletableDeferred(), "0110", 1)
        }.exceptionOrNull()
        assertTrue(timeout is ElmCommandTimeoutException)
        assertFalse((timeout as ElmCommandTimeoutException).promptRecovered)
        assertTrue(session.isTainted)
    }

    private fun iso(pid: Int, vararg data: Int, source: Int = 0x10, mode: Int = 0x41,
                    checksumDelta: Int = 0): String {
        val body = listOf(0x48, 0x6B, source, mode, pid) + data.toList()
        return (body + ((body.sum() + checksumDelta) and 0xFF)).joinToString(" ") { "%02X".format(it) } + "\r>"
    }
}
