package com.dashcamstats.obdlogger

import java.io.File
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class PollTimingTest {
    private fun pid(body: JSONObject, index: Int = 0) = body.getJSONArray("pids").getJSONObject(index)

    @Test
    fun actualPollingWrapperSeparatesEveryOutcomeAndNeverExportsResponseData() = runTest {
        var clock = 100L
        val timing = PollTiming { clock }
        timing.startCycle()
        val requests: List<suspend (Int) -> Map<String, Any>> = listOf(
            { clock += 10; mapOf("sensitive_raw_fixture" to "not-for-diagnostics") },
            { clock += 20; emptyMap() },
            { clock += 30; throw ElmProtocolException("private response") },
            { clock += 40; throw ElmCommandTimeoutException("private response") },
            { clock += 50; throw ElmException("private address") },
            { clock += 60; throw CancellationException("private identifier") },
        )
        requests.forEach { query ->
            try {
                timedLivePidPoll(0x0C, timing, query)
            } catch (_: Exception) {
                // The production wrapper must rethrow transport and cancellation failures.
            }
        }
        timing.cooldownSkip(0x0C)
        timing.rowPersisted(timing.takeRowAges())
        timing.finishCycle()
        val body = timing.drain("partial_failure")!!
        val row = pid(body)
        assertEquals(6L, row.getLong("attempts"))
        PollOutcome.entries.forEach { assertEquals(1L, row.getLong(it.field)) }
        assertEquals(1L, row.getLong("cooldown_skips"))
        val requestsJson = row.getJSONObject("request_ms")
        assertEquals(6L, requestsJson.getLong("count"))
        assertEquals(35.0, requestsJson.getDouble("median_ms"), 0.0)
        assertEquals(60L, requestsJson.getLong("p95_ms"))
        assertEquals(210L, requestsJson.getLong("total_ms"))
        assertEquals(200L, row.getJSONObject("value_age_at_row_ms").getLong("max_ms"))
        assertEquals(0L, body.getLong("cycles_completed"))
        assertFalse(body.toString().contains("private"))
        assertFalse(body.toString().contains("sensitive"))
        val fixture = File("build/outputs/poll-timing-fixture.json")
        fixture.parentFile.mkdirs()
        fixture.writeText(body.toString(2))
    }

    @Test
    fun timeoutAndCancellationPropagateWithoutCountingSuccess() = runTest {
        var clock = 0L
        val timing = PollTiming { clock }
        val failures = listOf(ElmCommandTimeoutException("timeout"), CancellationException("stop"))
        failures.forEach { expected ->
            var propagated: Exception? = null
            try {
                timedLivePidPoll(0x0D, timing) { clock += 200; throw expected }
            } catch (actual: Exception) {
                propagated = actual
            }
            assertTrue(expected === propagated)
        }
        val row = pid(timing.drain("partial_failure")!!)
        assertEquals(0L, row.getLong("successes"))
        val ages = row.getJSONObject("value_age_at_row_ms")
        assertEquals(0L, ages.getLong("count"))
        assertTrue(ages.isNull("median_ms"))
        assertTrue(ages.isNull("p95_ms"))
        assertEquals(0L, ages.getLong("max_ms"))
        assertEquals(0L, ages.getLong("total_ms"))
    }

    @Test
    fun onlyPersistedRowsGetAgesMeasuredBeforeDatabaseWork() = runTest {
        var clock = 0L
        val timing = PollTiming { clock }
        timing.startCycle()
        timedLivePidPoll(0x0C, timing) { clock = 100; mapOf("engine_rpm" to 800.0) }
        clock = 500
        val ages = timing.takeRowAges()
        clock = 900 // Simulated database latency must not inflate age at row timestamp.
        timing.rowPersisted(ages)
        assertTrue(timing.takeRowAges().isEmpty())
        timing.finishCycle(completed = true)
        clock = 5_000
        timing.startCycle()
        timedLivePidPoll(0x0C, timing) { clock = 5_100; mapOf("engine_rpm" to 800.0) }
        timing.takeRowAges() // A duplicate/failed insert is not passed to rowPersisted.
        timing.finishCycle()
        val body = timing.drain("drive_end")!!
        val row = pid(body)
        assertEquals(2L, row.getLong("successes"))
        assertEquals(1L, row.getJSONObject("value_age_at_row_ms").getLong("count"))
        assertEquals(400L, row.getJSONObject("value_age_at_row_ms").getLong("max_ms"))
        assertEquals(2L, body.getLong("cycles_started"))
        assertEquals(1L, body.getLong("cycles_completed"))
        assertEquals(5_000L, body.getJSONObject("cycle_start_interval_ms").getLong("max_ms"))
        assertEquals(1_000L, body.getJSONObject("cycle_work_ms").getLong("total_ms"))
    }

    @Test
    fun partialRowPreservesEarlierSuccessButFailedRequestGetsNoAge() = runTest {
        var clock = 0L
        val timing = PollTiming { clock }
        timing.startCycle()
        timedLivePidPoll(0x0C, timing) { clock = 100; mapOf("engine_rpm" to 800.0) }
        try {
            timedLivePidPoll(0x0D, timing) { clock = 6_100; throw ElmCommandTimeoutException("timeout") }
        } catch (_: ElmCommandTimeoutException) {
            timing.rowPersisted(timing.takeRowAges())
        }
        timing.finishCycle()
        timing.finishCycle() // Final cleanup must not count the same interrupted cycle twice.
        val body = timing.drain("partial_failure")!!
        assertEquals(1L, body.getLong("overrun_count"))
        assertEquals(1L, body.getJSONObject("cycle_work_ms").getLong("count"))
        assertEquals(6_000L, pid(body).getJSONObject("value_age_at_row_ms").getLong("max_ms"))
        assertEquals(0L, pid(body, 1).getJSONObject("value_age_at_row_ms").getLong("count"))
    }

    @Test
    fun monotonicRegressionCannotCreateNegativeWorkOrAge() {
        var clock = 1_000L
        val timing = PollTiming(driveOriginMillis = 500) { clock }
        timing.startCycle()
        val started = timing.now()
        clock = 900
        timing.recordRequest(0x0C, started, PollOutcome.SUCCESS)
        clock = 800
        timing.rowPersisted(timing.takeRowAges())
        timing.finishCycle(completed = true)
        val body = timing.drain("drive_end")!!
        assertEquals(500L, body.getLong("window_start_elapsed_ms"))
        assertEquals(0L, body.getLong("window_duration_ms"))
        assertEquals(0L, body.getJSONObject("cycle_work_ms").getLong("max_ms"))
        assertEquals(0L, pid(body).getJSONObject("request_ms").getLong("max_ms"))
        assertEquals(0L, pid(body).getJSONObject("value_age_at_row_ms").getLong("max_ms"))
    }

    @Test
    fun periodicWindowsRespectSixtySecondsResetAndRetainBoundaryCadence() {
        var clock = 0L
        val timing = PollTiming { clock }
        timing.startCycle()
        clock = 5_001
        timing.finishCycle(completed = true)
        clock = 59_999
        assertNull(timing.drain("periodic"))
        clock = 60_000
        val first = timing.drain("periodic")!!
        assertEquals(1L, first.getLong("overrun_count"))
        assertEquals(0L, first.getLong("window_index"))
        assertNull(timing.drain("drive_end"))
        timing.startCycle()
        clock += 10
        timing.finishCycle(completed = true)
        assertNull(timing.drain("periodic"))
        val last = timing.drain("drive_end")!!
        assertEquals(1L, last.getLong("window_index"))
        assertEquals(60_000L, last.getLong("window_start_elapsed_ms"))
        assertEquals(10L, last.getJSONObject("cycle_work_ms").getLong("max_ms"))
        assertEquals(60_000L, last.getJSONObject("cycle_start_interval_ms").getLong("max_ms"))
    }

    @Test
    fun retainedDistributionsAndLongDriveEventCountAreBounded() {
        var clock = 0L
        val timing = PollTiming { clock }
        repeat(64) {
            timing.startCycle()
            clock += 60_000
            timing.finishCycle(completed = true)
            assertTrue(timing.drain("periodic") != null)
        }
        repeat(300) {
            timing.startCycle()
            val started = timing.now()
            clock += 100
            timing.recordRequest(0x0C, started, PollOutcome.MISSING)
            timing.recordRequest(0xFF, started, PollOutcome.MISSING) // Unknown IDs cannot enter output.
            timing.finishCycle(completed = true)
            clock += 60_000
            assertNull(timing.drain("periodic"))
        }
        val last = timing.drain("drive_end")!!
        assertEquals(64L, last.getLong("window_index"))
        assertEquals(300L, last.getLong("cycles_started"))
        assertEquals(1, last.getJSONArray("pids").length())
        val distribution = pid(last).getJSONObject("request_ms")
        assertEquals(300L, distribution.getLong("count"))
        assertEquals(256, distribution.getInt("retained"))
        assertEquals(30_000L, distribution.getLong("total_ms"))
        assertNull(timing.drain("drive_end"))
    }
}
