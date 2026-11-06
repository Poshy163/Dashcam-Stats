package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class StartupRadioPoliciesTest {
    private val settle = 12_000L

    @Test
    fun bluetoothAlreadyOnConnectsImmediately() {
        val gate = BluetoothReadinessGate(settle)
        assertEquals(0L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 1_000))
        assertEquals(0L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 2_000))
    }

    @Test
    fun bluetoothOffNeverStartsAConnection() {
        val gate = BluetoothReadinessGate(settle)
        assertNull(gate.delayBeforeConnect(adapterOn = false, nowMillis = 0))
        assertNull(gate.delayBeforeConnect(adapterOn = false, nowMillis = 60_000))
    }

    @Test
    fun aStartFromSleepWaitsForTheSettleAfterBluetoothComesOn() {
        val gate = BluetoothReadinessGate(settle)
        assertNull(gate.delayBeforeConnect(adapterOn = false, nowMillis = 0))
        assertEquals(settle, gate.delayBeforeConnect(adapterOn = true, nowMillis = 1_000))
        assertEquals(7_000L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 6_000))
        assertEquals(0L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 13_000))
        // Settled: later reconnects in the same session do not wait again.
        assertEquals(0L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 60_000))
    }

    @Test
    fun theBroadcastStartsTheSettleClockBeforeTheLoopLooks() {
        val gate = BluetoothReadinessGate(settle)
        gate.observe(adapterOn = false, nowMillis = 0)
        gate.observe(adapterOn = true, nowMillis = 2_000)
        assertEquals(4_000L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 10_000))
        assertEquals(0L, gate.delayBeforeConnect(adapterOn = true, nowMillis = 14_000))
    }

    @Test
    fun bluetoothGoingOffAgainRestartsTheSettle() {
        val gate = BluetoothReadinessGate(settle)
        gate.observe(adapterOn = false, nowMillis = 0)
        gate.observe(adapterOn = true, nowMillis = 1_000)
        gate.observe(adapterOn = false, nowMillis = 5_000)
        assertNull(gate.delayBeforeConnect(adapterOn = false, nowMillis = 6_000))
        assertEquals(settle, gate.delayBeforeConnect(adapterOn = true, nowMillis = 8_000))
    }

    @Test
    fun aClockGoingBackwardsCannotExtendTheWait() {
        val gate = BluetoothReadinessGate(settle)
        gate.observe(adapterOn = false, nowMillis = 50_000)
        gate.observe(adapterOn = true, nowMillis = 50_000)
        assertEquals(settle, gate.delayBeforeConnect(adapterOn = true, nowMillis = 10_000))
    }

    private fun valid(id: String) = IngestionRequestRead.Valid(
        IngestionRequest(id, "2026-11-06T04:00:00Z", "2026-11-06T04:10:00Z"),
    )

    @Test
    fun anAcknowledgedPauseIsReleasedOnceWhenTheIgnitionComesOn() {
        val release = IgnitionPauseRelease()
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(valid("r1"), ignitionOn = false, acknowledgedRequestId = "r1"),
        )
        assertEquals(
            IgnitionPauseDecision.RELEASE,
            release.decide(valid("r1"), ignitionOn = true, acknowledgedRequestId = "r1"),
        )
        // The lingering request stays released even after the ignition goes off again.
        assertEquals(
            IgnitionPauseDecision.ALREADY_RELEASED,
            release.decide(valid("r1"), ignitionOn = false, acknowledgedRequestId = null),
        )
    }

    @Test
    fun aRequestFirstSeenWithTheIgnitionOnIsHonoured() {
        val release = IgnitionPauseRelease()
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(valid("r1"), ignitionOn = true, acknowledgedRequestId = null),
        )
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(valid("r2"), ignitionOn = true, acknowledgedRequestId = "r1"),
        )
    }

    @Test
    fun aWithdrawnOrReplacedRequestIsJudgedAfresh() {
        val release = IgnitionPauseRelease()
        release.decide(valid("r1"), ignitionOn = true, acknowledgedRequestId = "r1")
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(IngestionRequestRead.Absent, ignitionOn = true, acknowledgedRequestId = null),
        )
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(valid("r1"), ignitionOn = false, acknowledgedRequestId = "r1"),
        )
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(valid("r2"), ignitionOn = false, acknowledgedRequestId = "r2"),
        )
    }

    @Test
    fun anInvalidRequestIsNeverMaskedAsReleased() {
        val release = IgnitionPauseRelease()
        assertEquals(
            IgnitionPauseDecision.KEEP,
            release.decide(
                IngestionRequestRead.Invalid("malformed"),
                ignitionOn = true,
                acknowledgedRequestId = "r1",
            ),
        )
    }
}
