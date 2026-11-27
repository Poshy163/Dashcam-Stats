package com.dashcamstats.obdlogger

/** A low startup voltage can be a temporary dip. Share one adapter-only retry between both gates. */
internal class StartupVoltageRecheck {
    companion object {
        const val DELAY_MILLIS = 5_000L
        private const val STARTUP_WINDOW_MILLIS = 60_000L
    }

    private var lastAcc: Boolean? = null
    private var edge: Long? = null
    private var claimed = false

    @Synchronized
    fun observeAcc(on: Boolean?, now: Long) {
        if (on != true) edge = null
        if (on == true && lastAcc == false) {
            edge = now
            claimed = false
        }
        lastAcc = on
    }

    @Synchronized
    fun engineProved() {
        edge = null
    }

    @Synchronized
    private fun claim(now: Long): Long? {
        val observed = edge ?: return null
        if (claimed || now - observed !in 0..(STARTUP_WINDOW_MILLIS - DELAY_MILLIS)) return null
        claimed = true
        return observed
    }

    @Synchronized
    private fun current(observed: Long, now: Long): Boolean =
        edge == observed && now - observed in 0..STARTUP_WINDOW_MILLIS

    suspend fun afterLowReading(
        voltage: Double?,
        voltageOn: Double,
        now: () -> Long,
        allowed: () -> Boolean,
        wait: suspend (Long) -> Unit,
        onRetry: () -> Unit,
        readVoltage: suspend () -> Double?,
    ): Double? {
        if (voltage == null || !voltage.isFinite()) return null
        if (voltage >= voltageOn) return voltage
        val observed = claim(now()) ?: return voltage
        if (!allowed()) return voltage
        onRetry()
        // Release the connection promptly if backup takes ownership or the ignition changes.
        repeat((DELAY_MILLIS / 500L).toInt()) {
            wait(500L)
            if (!current(observed, now()) || !allowed()) return voltage
        }
        val refreshed = readVoltage()
        return if (current(observed, now()) && allowed()) refreshed else voltage
    }
}
