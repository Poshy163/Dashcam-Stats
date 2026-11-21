package com.dashcamstats.obdlogger;

/** One bounded hold per observed off-to-on edge; attaching mid-drive never interrupts Wi-Fi. */
final class StartupWifiPolicy {
    static final long HOLD_MILLIS = 30_000;
    private static final long RECONNECT_CHECK_MILLIS = 35_000;
    private Boolean previous;
    private long deadline;
    private long pendingWake = -1;

    long observe(Boolean ignitionOn, long elapsedMillis) {
        if (ignitionOn == null || !ignitionOn) {
            deadline = 0;
            pendingWake = -1;
        } else if (Boolean.FALSE.equals(previous)) {
            deadline = elapsedMillis + HOLD_MILLIS;
            pendingWake = elapsedMillis;
        }
        previous = ignitionOn;
        if (elapsedMillis >= deadline) deadline = 0;
        return deadline;
    }

    /** Consume the original wake once. A delayed poll after sleep must not retry mid-drive. */
    long takeReconnectWake(long elapsedMillis) {
        if (pendingWake < 0 || elapsedMillis - pendingWake < RECONNECT_CHECK_MILLIS) return -1;
        long wake = pendingWake;
        pendingWake = -1;
        return elapsedMillis - wake <= RECONNECT_CHECK_MILLIS + 5_000 ? wake : -1;
    }
}
