package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
class SleepDeadlineEvidenceTest {
    private val boot = SleepBootIdentity("01234567-1234-1234-1234-012345678901", 12)

    private fun SleepDeadlineTracker.read(at: Long, on: Boolean?, window: Int? = 1200) =
        observe(boot, at, at + 100, on, window)

    @Test
    fun startupOffAndRepeatedOffCannotInventAnEdge() {
        val tracker = SleepDeadlineTracker()
        tracker.read(1000, false)
        tracker.read(6000, false)
        assertNull(tracker.snapshot(6100)?.offLowerElapsedMillis)
        assertNull(tracker.snapshot(6100)?.windowSeconds)
    }

    @Test
    fun lowerBoundUsesLastOnAndMinimumActualWindowThenStaysFrozen() {
        val tracker = SleepDeadlineTracker()
        tracker.read(1000, true, 1200)
        tracker.read(6000, true, 300)
        tracker.read(11000, false, 1200)
        val first = tracker.snapshot(11100)!!
        assertEquals(6000L, first.offLowerElapsedMillis)
        assertEquals(11100L, first.offUpperElapsedMillis)
        assertEquals(300, first.windowSeconds)
        tracker.read(16000, false, 3600)
        val later = tracker.snapshot(16100)!!
        assertEquals(first.offLowerElapsedMillis, later.offLowerElapsedMillis)
        assertEquals(first.offUpperElapsedMillis, later.offUpperElapsedMillis)
        assertEquals(first.windowSeconds, later.windowSeconds)
        assertEquals(16100L, later.observedElapsedMillis)
    }

    @Test
    fun unknownAccOrWindowBreaksContinuityUntilNewOnOff() {
        for (failedAcc in listOf(true, false)) {
            val tracker = SleepDeadlineTracker()
            tracker.read(1000, true)
            tracker.read(6000, false)
            tracker.read(11000, if (failedAcc) null else false, if (failedAcc) 1200 else null)
            assertNull(tracker.snapshot(11100))
            tracker.read(16000, false)
            assertNull(tracker.snapshot(16100)?.offLowerElapsedMillis)
            tracker.read(21000, true)
            tracker.read(26000, false)
            assertEquals(21000L, tracker.snapshot(26100)?.offLowerElapsedMillis)
        }
    }

    @Test
    fun slowCaptureAndClockReversalCannotPreserveOldEdge() {
        for (range in listOf(11000L to 17001L, 11000L to 10999L, -1L to 0L)) {
            val tracker = SleepDeadlineTracker()
            tracker.read(1000, true)
            tracker.read(6000, false)
            tracker.observe(boot, range.first, range.second, false, 1200)
            assertNull(tracker.snapshot(18000))
            tracker.read(20000, false)
            assertNull(tracker.snapshot(20100)?.windowSeconds)
        }
    }

    @Test
    fun missedObservationAndStaleSnapshotRequireNewEdge() {
        val tracker = SleepDeadlineTracker()
        tracker.read(1000, true)
        tracker.read(21000, false)
        assertNull(tracker.snapshot(21100)?.windowSeconds)
        tracker.read(26000, true)
        tracker.read(31000, false)
        assertEquals(1200, tracker.snapshot(46100)?.windowSeconds)
        assertNull(tracker.snapshot(46101))
        tracker.read(47000, false)
        assertNull(tracker.snapshot(47100)?.windowSeconds)
    }

    @Test
    fun rebootAndMissingIdentityCannotReuseDeadline() {
        val tracker = SleepDeadlineTracker()
        tracker.read(1000, true)
        tracker.read(6000, false)
        tracker.observe(SleepBootIdentity(null, 13), 11000, 11100, false, 1200)
        assertNull(tracker.snapshot(11100)?.windowSeconds)
        tracker.observe(SleepBootIdentity(null, null), 16000, 16100, true, 1200)
        assertNull(tracker.snapshot(16100))
    }

    @Test
    fun bootCountFallbackAndZeroElapsedClockAreValid() {
        val tracker = SleepDeadlineTracker()
        tracker.observe(SleepBootIdentity(null, 0), 0, 100, true, 300)
        tracker.observe(SleepBootIdentity(null, 0), 5000, 5100, false, 1200)
        val evidence = tracker.snapshot(5100)!!
        assertNull(evidence.bootId)
        assertEquals(0, evidence.bootCount)
        assertEquals(0L, evidence.offLowerElapsedMillis)
        assertEquals(300, evidence.windowSeconds)
    }

    @Test
    fun invalidWindowsDoNotBecomeDeadlines() {
        for (window in listOf(0, -1, 3601)) {
            val tracker = SleepDeadlineTracker()
            tracker.read(1000, true)
            tracker.read(6000, false, window)
            assertNull(tracker.snapshot(6100))
        }
    }

    @Test
    fun offObservationPrecedesPolicyWriteWithoutWifiAndLaterWritesDoNotRestartIt() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        var on: Boolean? = true
        var seconds = 1200
        val operations = mutableListOf<String>()
        val property = object : SleepWindowPropertyAccessor {
            override fun readSeconds(): Int = seconds.also { operations += "read:$it" }
            override fun writeSeconds(value: Int): Boolean {
                operations += "write:$value"
                seconds = value
                return true
            }
        }
        val controller = AdaptiveSleepWindowController(
            context, this, property,
            object : AccStateAccessor { override fun readAccOn(): Boolean? = on },
            elapsedMillis = { testScheduler.currentTime },
            bootIdentity = { boot },
        )
        try {
            controller.start()
            runCurrent()
            controller.setIngestionRequestActive(false)
            runCurrent()
            seconds = 1200
            advanceTimeBy(5000)
            runCurrent()
            operations.clear()
            on = false
            advanceTimeBy(5000)
            runCurrent()
            val edge = controller.snapshot().deadline!!
            assertFalse(controller.snapshot().wifiConnected)
            assertEquals(5000L, edge.offLowerElapsedMillis)
            assertEquals(10000L, edge.offUpperElapsedMillis)
            assertEquals(1200, edge.windowSeconds)
            assertTrue(operations.indexOf("read:1200") < operations.indexOf("write:300"))
            advanceTimeBy(5000)
            runCurrent()
            assertEquals(1200, controller.snapshot().deadline?.windowSeconds)
            on = null
            advanceTimeBy(5000)
            runCurrent()
            assertNull(controller.snapshot().deadline)
        } finally {
            controller.close()
            runCurrent()
        }
    }

    @Test
    fun offDuringWindowReenlargementRetainsInterveningSmallerWindowBetweenPolls() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        var on = true
        var seconds = 1200
        var switchOffWhileEnlarging = false
        val property = object : SleepWindowPropertyAccessor {
            override fun readSeconds(): Int = seconds
            override fun writeSeconds(value: Int): Boolean {
                // The native timer can latch300 immediately before the app writes1200.
                if (value == 1200 && switchOffWhileEnlarging) on = false
                seconds = value
                return true
            }
        }
        val controller = AdaptiveSleepWindowController(
            context, this, property,
            object : AccStateAccessor { override fun readAccOn(): Boolean = on },
            elapsedMillis = { testScheduler.currentTime },
            bootIdentity = { boot },
        )
        try {
            controller.start()
            runCurrent()
            advanceTimeBy(1000)
            controller.setIngestionRequestActive(false)
            runCurrent()
            assertEquals(300, seconds)
            advanceTimeBy(1000)
            switchOffWhileEnlarging = true
            controller.setIngestionRequestActive(true)
            runCurrent()
            assertEquals(1200, seconds)
            val edge = controller.snapshot().deadline!!
            assertFalse(edge.ignitionOn)
            assertEquals(300, edge.windowSeconds)
            assertEquals(2000L, edge.offLowerElapsedMillis)
            assertEquals(2000L, edge.offUpperElapsedMillis)
        } finally {
            controller.close()
            runCurrent()
        }
    }

    @Test
    fun statusSerializesExactNullableContractAndCapability() {
        val evidence = SleepDeadlineEvidence(null, 12, 11000, false, 5000, 10000, 300)
        val status = PublicStatus("parked", true, sleepDeadlineEvidence = evidence)
        val json = StatusPublisher.buildStatusJson(status, 0)
        val deadline = json.getJSONObject("sleep_deadline_evidence")
        assertEquals(setOf("schema_version", "boot_id", "boot_count", "observed_elapsed_ms", "ignition_on",
            "off_lower_elapsed_ms", "off_upper_elapsed_ms", "window_s"), deadline.keys().asSequence().toSet())
        assertEquals(1, deadline.getInt("schema_version"))
        assertTrue(deadline.isNull("boot_id"))
        assertEquals(12, deadline.getInt("boot_count"))
        assertEquals(11000L, deadline.getLong("observed_elapsed_ms"))
        assertEquals(300, deadline.getInt("window_s"))
        assertTrue(json.getJSONArray("capabilities").toString().contains("sleep_deadline_evidence_v1"))
        assertTrue(StatusPublisher.buildStatusJson(status.copy(sleepDeadlineEvidence = null), 0)
            .isNull("sleep_deadline_evidence"))
    }

    @Test
    fun heartbeatPublishesFreshObservationsAtTenSecondsNotEveryPollAndInvalidationImmediately() {
        val first = PublicStatus("parked", true, sleepDeadlineEvidence =
            SleepDeadlineEvidence(boot.bootId, boot.bootCount, 10000, false, 4000, 9000, 1200))
        val newer = first.copy(sleepDeadlineEvidence = first.sleepDeadlineEvidence!!.copy(observedElapsedMillis = 15000))
        val signature = durableStatusSignature(first)
        assertEquals(signature, durableStatusSignature(newer))
        val gate = StatusWriteGate()
        assertTrue(gate.shouldWrite(signature, 10000, DEADLINE_STATUS_HEARTBEAT_MS))
        assertFalse(gate.shouldWrite(signature, 15000, DEADLINE_STATUS_HEARTBEAT_MS))
        assertTrue(gate.shouldWrite(signature, 20000, DEADLINE_STATUS_HEARTBEAT_MS))
        assertTrue(gate.shouldWrite(durableStatusSignature(first.copy(sleepDeadlineEvidence = null)), 20001))
        assertFalse(gate.shouldWrite(durableStatusSignature(first.copy(sleepDeadlineEvidence = null)), 30001))
    }
}
