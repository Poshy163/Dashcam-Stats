package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class QuietResourceRetryTest {
    private val first = "a".repeat(64)
    private val other = "b".repeat(64)
    private fun refused(reason: QuietAnalysisReason = QuietAnalysisReason.SYSTEM_THERMAL_GUARD) =
        ClipTransferPolicy.emptyVisual("unavailable", reason)

    @Test fun sharedCooldownPreventsRotatingThroughOtherHotClipsAndSurvivesRestart() {
        val retry = QuietResourceRetry()
        retry.record(first, refused(), 1_000)
        assertTrue(retry.pending(first, refused(), 1_000, 361_000))
        assertFalse(retry.ready(first, refused(), 30_999, 361_000))
        assertFalse(retry.ready(other, null, 30_999, 361_000))
        val restored = QuietResourceRetry.fromJson(JSONObject(retry.json().toString()))
        assertFalse(restored.ready(other, null, 30_999, 361_000))
        assertTrue(restored.ready(first, refused(), 31_000, 361_000))
        assertTrue(restored.ready(other, null, 31_000, 361_000))
        restored.record(first, refused(), 31_000)
        assertFalse(restored.ready(first, refused(), 90_999, 361_000))
        assertTrue(restored.ready(first, refused(), 91_000, 361_000))
    }

    @Test fun fourthInterruptedDecodeIsTerminalAndNeitherExpiryNorRestartCreatesANewBudget() {
        var retry = QuietResourceRetry()
        val deadline = 361_000L
        val interrupted = refused().put("input_frames", 2).put("decoded_frames", 1)
        for ((index, at) in listOf(1_000L, 31_000L, 91_000L, 211_000L).withIndex()) {
            retry.record(first, interrupted, at)
            retry = QuietResourceRetry.fromJson(JSONObject(retry.json().toString()))
            assertEquals(index < 3, retry.pending(first, interrupted, at, deadline))
        }
        assertFalse(retry.ready(first, interrupted, 331_000, deadline))
        assertFalse(retry.pending(first, interrupted, deadline, deadline))
        assertFalse(retry.ready(other, null, deadline, deadline))
        val expired = QuietResourceRetry()
        expired.record(first, refused(), deadline - 1)
        assertFalse(expired.pending(first, refused(), deadline, deadline))
        assertFalse(expired.ready(first, refused(), deadline + 120_000, deadline))
    }

    @Test fun hotAdmissionProbesStayRateLimitedWithoutSpendingDecodeBudgetAcrossRestart() {
        var retry = QuietResourceRetry()
        val deadline = 24L * 60 * 60 * 1_000
        var at = 1_000L
        repeat(12) { index ->
            retry.record(first, refused(), at)
            retry = QuietResourceRetry.fromJson(JSONObject(retry.json().toString()))
            assertTrue(retry.pending(first, refused(), at, deadline))
            val delay = when (index) { 0 -> 30_000; 1 -> 60_000; else -> 120_000 }
            assertFalse(retry.ready(first, refused(), at + delay - 1, deadline))
            assertFalse(retry.ready(other, null, at + delay - 1, deadline))
            at += delay
            assertTrue(retry.ready(first, refused(), at, deadline))
        }
        assertEquals(0, retry.json().getJSONArray("attempts").getJSONObject(0).getInt("count"))
        assertFalse(retry.pending(first, refused(), deadline, deadline))
    }

    @Test fun queuedInputWithoutOutputStillSpendsDecodeBudgetAndHotProbeCannotRearmIt() {
        val retry = QuietResourceRetry()
        val queued = refused().put("input_frames", 1)
        for (at in listOf(1_000L, 31_000L, 91_000L, 211_000L)) retry.record(first, queued, at)
        retry.record(first, refused(), 331_000)
        assertFalse(retry.ready(first, refused(), 451_000, 1_000_000))
        assertEquals(QuietResourceRetry.MAX_ATTEMPTS,
            retry.json().getJSONArray("attempts").getJSONObject(0).getInt("count"))
    }

    @Test fun onlyKnownResourceRefusalsWithoutObservedActivityCanRetry() {
        for (reason in listOf(QuietAnalysisReason.SYSTEM_THERMAL_GUARD,
            QuietAnalysisReason.CPU_TEMPERATURE_GUARD, QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT,
            QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD, QuietAnalysisReason.MEMORY_GUARD)) {
            assertTrue(QuietResourceRetry.transient(refused(reason)))
        }
        for (reason in QuietAnalysisReason.entries.filter { it !in setOf(
            QuietAnalysisReason.SYSTEM_THERMAL_GUARD, QuietAnalysisReason.CPU_TEMPERATURE_GUARD,
            QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT, QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD,
            QuietAnalysisReason.MEMORY_GUARD) }) assertFalse(QuietResourceRetry.transient(refused(reason)))
        for (status in listOf("activity", "quiet", "error", "incomplete", "not_analyzed"))
            assertFalse(QuietResourceRetry.transient(refused().put("status", status)))
        for ((key, value) in listOf("motion_frames" to 1, "max_coherent_pixels" to 16,
            "max_rolling_coherent_pixels" to 128, "max_consecutive_coherent_frames" to 2))
            assertFalse(QuietResourceRetry.transient(refused().put(key, value)))
        assertFalse(QuietResourceRetry.transient(JSONObject().put("status", "unavailable")
            .put("reason", "system_thermal_guard")))
    }

    @Test fun rollbackAndMalformedPersistedStateFailClosedWithoutExtendingDeadline() {
        val retry = QuietResourceRetry()
        retry.record(first, refused(), 100_000)
        val restored = QuietResourceRetry.fromJson(JSONObject(retry.json().toString()))
        assertTrue(restored.advance(99_999))
        assertFalse(restored.advance(99_998)) // Do not rewrite the same terminal budget every indexer tick.
        assertFalse(restored.pending(first, refused(), 99_999, 460_000))
        assertFalse(restored.ready(other, null, 99_999, 460_000))
        val again = QuietResourceRetry.fromJson(JSONObject(restored.json().toString()))
        assertFalse(again.pending(first, refused(), 100_000, 460_000))
        for (bad in listOf(null, JSONObject(), retry.json().put("cooldown_until_ms", Long.MAX_VALUE),
            retry.json().put("refusal_streak", -1))) {
            val invalid = QuietResourceRetry.fromJson(bad)
            assertFalse(invalid.pending(first, refused(), 200_000, 460_000))
        }
    }

    @Test fun successfulOrTerminalNonResourceResultClearsCoolingAndRetainsOnlyCurrentIdentities() {
        val retry = QuietResourceRetry()
        retry.record(first, refused(), 1_000)
        retry.record(first, ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.FRAME_QUALITY), 31_000)
        assertTrue(retry.ready(other, null, 31_000, 361_000))
        assertFalse(retry.pending(first, refused(), 31_000, 361_000))
        retry.record(first, refused(), 31_000)
        retry.retain(setOf(other))
        assertFalse(retry.pending(first, refused(), 61_000, 361_000))
    }
}
