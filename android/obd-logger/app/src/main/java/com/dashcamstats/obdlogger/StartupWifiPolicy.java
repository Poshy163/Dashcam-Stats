package com.dashcamstats.obdlogger;

/** One bounded hold per observed off-to-on edge; attaching mid-drive never interrupts Wi-Fi. */
final class StartupWifiPolicy {
    static final long HOLD_MILLIS = 30_000;
    private static final long RECONNECT_CHECK_MILLIS = 35_000;
    private Boolean previous;
    private long deadline;
    private long pendingWake = -1;
    private long wake = -1;
    private boolean planStarted;
    private long lateDue = -1;
    private long lastObservation = -1;

    long observe(Boolean ignitionOn, long elapsedMillis) {
        // A long gap while already ON can hide an entire off/on cycle. Do not carry
        // a phone decision across it. The independent 30-second Wi-Fi hold still expires normally.
        if (wake >= 0 && Boolean.TRUE.equals(previous) && elapsedMillis - lastObservation > 5_000) {
            wake = -1;
            pendingWake = -1;
            lateDue = -1;
        }
        lastObservation = elapsedMillis;
        if (ignitionOn == null || !ignitionOn) {
            deadline = 0;
            pendingWake = -1;
            wake = -1;
            planStarted = false;
            lateDue = -1;
        } else if (Boolean.FALSE.equals(previous)) {
            deadline = elapsedMillis + HOLD_MILLIS;
            pendingWake = elapsedMillis;
            wake = elapsedMillis;
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

    long currentWake() { return wake; }

    /** Read the lease history once, after the normal phone startup and early retry. */
    boolean takeLeasePlan(long now) {
        if (wake < 0 || planStarted || now - wake < 70_000) return false;
        planStarted = true;
        return now - wake <= 75_000;
    }

    void acceptLeasePlan(long plannedWake, long now, long due) {
        if (wake >= 0 && plannedWake == wake && planStarted && lateDue == -1
                && now - wake <= 100_000 && due >= wake + 90_000 && due <= wake + 200_000) {
            lateDue = due;
        }
    }

    boolean takeLateRetry(long now) {
        if (lateDue < 0 || now < lateDue) return false;
        long due = lateDue;
        lateDue = -2;
        return now - due <= 5_000 && now - wake <= 200_000;
    }
}
