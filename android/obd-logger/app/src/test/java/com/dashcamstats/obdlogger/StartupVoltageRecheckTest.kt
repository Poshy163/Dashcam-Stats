package com.dashcamstats.obdlogger

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class StartupVoltageRecheckTest {
    private val gate = StartupVoltageRecheck()
    private var time = 30_000L
    private var reads = 0
    private var retries = 0
    private var allowed = true
    private var afterSleep: () -> Unit = {}
    private var afterRead: () -> Unit = {}

    private fun start() {
        gate.observeAcc(false, time - 20_000)
        gate.observeAcc(true, time - 15_000)
    }

    private fun result(low: Double? = 12.6, refreshed: Double? = 14.2): Double? = runBlocking {
        gate.afterLowReading(low, 13.5, { time }, { allowed },
            wait = { time += it; afterSleep() },
            onRetry = { retries++ },
            readVoltage = { reads++; afterRead(); refreshed },
        )
    }

    @Test fun oneRetrySharedByBothGatesThenParkOrRequireNormalEcuProof() {
        start()
        assertNull(result(null))
        assertEquals(13.5, result(13.5))
        assertEquals(14.2, result())
        assertEquals(35_000L, time)
        gate.observeAcc(true, time)
        assertEquals(12.6, result())
        assertEquals(1, reads)
        start()
        assertEquals(12.7, result(refreshed = 12.7))
        assertEquals(12.6, result())
        assertEquals(2, reads)
    }

    @Test fun requiresRecentObservedEdgeAndDisarmsAfterProof() {
        gate.observeAcc(true, time)
        assertEquals(12.6, result())
        gate.observeAcc(false, time)
        gate.observeAcc(null, time)
        gate.observeAcc(true, time)
        assertEquals(12.6, result())
        start()
        time += 50_000
        assertEquals(12.6, result())
        start()
        gate.engineProved()
        assertEquals(12.6, result())
        assertEquals(0, retries)
    }

    @Test fun handoffOrReplacedIgnitionCancelsPendingRead() {
        start()
        afterSleep = { allowed = false }
        assertEquals(12.6, result())
        assertEquals(30_500L, time)
        allowed = true
        start()
        afterSleep = {
            gate.observeAcc(false, time)
            gate.observeAcc(true, time)
        }
        assertEquals(12.6, result())
        assertEquals(0, reads)
    }

    @Test fun lostIgnitionDuringAdapterReplyCannotOpenEcuGate() {
        start()
        afterRead = { gate.observeAcc(null, time) }
        assertEquals(12.6, result())
        assertEquals(1, reads)
    }
}
