package com.dashcamstats.obdlogger;

import org.junit.Test;
import static org.junit.Assert.assertEquals;

public class StartupWifiPolicyTest {
    @Test public void reconnectCheckFiresOnceAfterTheNormalStartupWindow() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertEquals(-1, policy.takeReconnectWake(35_999));
        assertEquals(0, policy.observe(true, 36_000));
        assertEquals(1_000, policy.takeReconnectWake(36_000));
        assertEquals(-1, policy.takeReconnectWake(36_001));
    }

    @Test public void reconnectCheckRequiresAnUninterruptedRecentIgnitionEdge() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(true, 0);
        assertEquals(-1, policy.takeReconnectWake(35_000));
        policy.observe(false, 40_000);
        policy.observe(true, 41_000);
        policy.observe(null, 42_000);
        assertEquals(-1, policy.takeReconnectWake(76_000));
        policy.observe(false, 80_000);
        policy.observe(true, 81_000);
        assertEquals(-1, policy.takeReconnectWake(125_000));
        policy.observe(false, 130_000);
        policy.observe(true, 131_000);
        policy.observe(false, 160_000);
        assertEquals(-1, policy.takeReconnectWake(166_000));
    }

    @Test public void wakeHoldsOnceForThirtySeconds() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        assertEquals(0, policy.observe(false, 100));
        assertEquals(31_000, policy.observe(true, 1_000));
        assertEquals(31_000, policy.observe(true, 30_999));
        assertEquals(0, policy.observe(true, 31_000));
        assertEquals(0, policy.observe(true, 90_000));
    }
    @Test public void offCancelsAndNextStartGetsFreshHold() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertEquals(0, policy.observe(false, 2_000));
        assertEquals(33_000, policy.observe(true, 3_000));
    }
    @Test public void attachingMidDriveDoesNotDisconnect() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        assertEquals(0, policy.observe(true, 1_000));
        assertEquals(0, policy.observe(true, 2_000));
    }
    @Test public void unknownReleasesAndCannotInventIgnitionEdge() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertEquals(0, policy.observe(null, 2_000));
        assertEquals(0, policy.observe(true, 3_000));
        policy.observe(false, 4_000);
        assertEquals(35_000, policy.observe(true, 5_000));
    }
    @Test public void timeSpentSuspendedDoesNotExtendExistingHold() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertEquals(0, policy.observe(true, 600_000));
    }
}
