package com.dashcamstats.obdlogger

class EngineGate(
    private val voltageOff: Double = 13.0,
    private val graceMillis: Long = 30_000,
    private val rpmVetoMillis: Long = 30_000,
) {
    private var belowSince: Long? = null
    private var lastRunningRpmAt: Long? = null

    fun remainsOnline(nowMillis: Long, voltage: Double?, rpm: Double?): Boolean {
        if (rpm != null && rpm > 300) {
            lastRunningRpmAt = nowMillis
            belowSince = null
            return true
        }
        if (voltage != null && voltage >= voltageOff) {
            belowSince = null
            return true
        }
        if (lastRunningRpmAt?.let { nowMillis - it <= rpmVetoMillis } == true) return true
        val firstLow = belowSince
        if (firstLow == null) {
            belowSince = nowMillis
            return true
        }
        return nowMillis - firstLow < graceMillis
    }
}

enum class EngineLifecycleState { PARKED, PROBING, RECORDING, STOPPED }

/** Small state seam shared by the foreground service and deterministic JVM safety tests. */
class EngineLifecycle(
    private val voltageOn: Double,
    voltageOff: Double,
    graceMillis: Long,
) {
    private val liveGate = EngineGate(voltageOff, graceMillis)

    var state: EngineLifecycleState = EngineLifecycleState.PARKED
        private set

    fun observeParkedVoltage(voltage: Double?): Boolean {
        check(state == EngineLifecycleState.PARKED)
        if (voltage == null || voltage < voltageOn) return false
        state = EngineLifecycleState.PROBING
        return true
    }

    fun acceptChecksumValidEcuProof(checksumValid0100: Boolean): Boolean {
        check(state == EngineLifecycleState.PROBING)
        if (!checksumValid0100) {
            state = EngineLifecycleState.PARKED
            return false
        }
        state = EngineLifecycleState.RECORDING
        return true
    }

    fun remainsRecording(nowMillis: Long, voltage: Double?, rpm: Double?): Boolean {
        check(state == EngineLifecycleState.RECORDING)
        if (liveGate.remainsOnline(nowMillis, voltage, rpm)) return true
        state = EngineLifecycleState.STOPPED
        return false
    }
}
