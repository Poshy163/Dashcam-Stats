package com.dashcamstats.obdlogger;

/** One bounded hold per observed off-to-on edge; attaching mid-drive never interrupts Wi-Fi. */
final class StartupWifiPolicy {
    static final long HOLD_MILLIS = 30_000;
    private Boolean previous;
    private long deadline;

    long observe(Boolean ignitionOn, long elapsedMillis) {
        if (ignitionOn == null || !ignitionOn) {
            deadline = 0;
        } else if (Boolean.FALSE.equals(previous)) {
            deadline = elapsedMillis + HOLD_MILLIS;
        }
        previous = ignitionOn;
        if (elapsedMillis >= deadline) deadline = 0;
        return deadline;
    }
}
