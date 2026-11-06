package com.dashcamstats.obdlogger

/**
 * How long the first adapter connection waits after Bluetooth comes back on.
 *
 * Every start from sleep turns Bluetooth on, and the iPhone reconnects for wireless CarPlay
 * over that same radio straight away: on 6 Nov 2026 its CarPlay link came up 10 s after
 * Bluetooth did. The adapter's connect and ELM/ECU setup burst used to land right on top of it
 * (connected at +5.5 s, busy until +14 s). Waiting this long lets the phone's handshake finish
 * first, at the cost of starting the drive's OBD log about this much later.
 */
internal const val BLUETOOTH_START_SETTLE_MILLIS = 12_000L

/**
 * Holds the adapter connection back while Bluetooth is off, and for a settle period after it
 * comes back on.
 *
 * Before this, every wake signal at a start (screen on, user present, ignition, Bluetooth on)
 * fired an immediate connection attempt while the adapter was still off, each failing in about
 * a millisecond and growing the backoff, and backups that switch Bluetooth off produced well
 * over a hundred such failures a day. Only an observed off-to-on transition earns the settle:
 * a service start or reconnect with Bluetooth already on connects at once, as before.
 */
internal class BluetoothReadinessGate(private val settleMillis: Long = BLUETOOTH_START_SETTLE_MILLIS) {
    private val lock = Any()
    private var sawOff = false
    private var onSinceMillis: Long? = null

    init {
        require(settleMillis >= 0) { "settle must not be negative" }
    }

    /** Record an adapter state seen anywhere (a broadcast or a direct read). */
    fun observe(adapterOn: Boolean, nowMillis: Long) {
        synchronized(lock) {
            if (!adapterOn) {
                sawOff = true
                onSinceMillis = null
            } else if (sawOff && onSinceMillis == null) {
                onSinceMillis = nowMillis
            }
        }
    }

    /**
     * `null` while Bluetooth is off (wait for it to come on), `0` when a connection may start
     * now, otherwise how many milliseconds of the settle remain.
     */
    fun delayBeforeConnect(adapterOn: Boolean, nowMillis: Long): Long? =
        synchronized(lock) {
            observe(adapterOn, nowMillis)
            if (!adapterOn) return@synchronized null
            val since = onSinceMillis ?: return@synchronized 0L
            // A clock that went backwards cannot extend the wait beyond one settle.
            val elapsed = (nowMillis - since).coerceAtLeast(0L)
            val remaining = settleMillis - elapsed
            if (remaining > 0) {
                remaining.coerceAtMost(settleMillis)
            } else {
                sawOff = false
                onSinceMillis = null
                0L
            }
        }
}

internal enum class IgnitionPauseDecision {
    /** Use the request as read. */
    KEEP,

    /** The driver is back: release this request now (reply once, then ignore it). */
    RELEASE,

    /** Already released; treat the lingering request as absent. */
    ALREADY_RELEASED,
}

/**
 * Ends a backup's pause of the logger when the ignition comes on mid-backup.
 *
 * The backup only starts with the ignition off and asks the logger to stay off Bluetooth for at
 * most ten minutes. If the driver returns, the server may already be out of reach (the car leaves
 * home Wi-Fi seconds after starting), and the logger used to sit out the rest of that lease:
 * on 3 Nov 2026 it missed the first 10.5 minutes of the drive. A request this logger had already
 * acknowledged is released once the ignition reads on; a request first seen while the ignition is
 * on was the server's own choice and is honoured. A released request stays released until it
 * disappears or a different request replaces it.
 */
internal class IgnitionPauseRelease {
    private val lock = Any()
    private var releasedRequestId: String? = null

    fun decide(
        read: IngestionRequestRead,
        ignitionOn: Boolean,
        acknowledgedRequestId: String?,
    ): IgnitionPauseDecision = synchronized(lock) {
        if (read !is IngestionRequestRead.Valid) {
            if (read is IngestionRequestRead.Absent) releasedRequestId = null
            return@synchronized IgnitionPauseDecision.KEEP
        }
        val requestId = read.request.requestId
        if (requestId == releasedRequestId) return@synchronized IgnitionPauseDecision.ALREADY_RELEASED
        if (ignitionOn && acknowledgedRequestId == requestId) {
            releasedRequestId = requestId
            return@synchronized IgnitionPauseDecision.RELEASE
        }
        IgnitionPauseDecision.KEEP
    }
}
