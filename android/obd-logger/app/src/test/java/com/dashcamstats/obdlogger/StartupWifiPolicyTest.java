package com.dashcamstats.obdlogger;

import org.junit.Test;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class StartupWifiPolicyTest {
    @Test public void oneLeaseProbeAndOneDeadlinePerWake() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertFalse(policy.takeLeasePlan(70_999));
        assertTrue(policy.takeLeasePlan(71_000));
        assertFalse(policy.takeLeasePlan(71_250));
        policy.acceptLeasePlan(1_000, 72_000, 107_500);
        assertFalse(policy.takeLateRetry(107_499));
        assertTrue(policy.takeLateRetry(107_500));
        policy.acceptLeasePlan(1_000, 73_000, 110_000);
        assertFalse(policy.takeLateRetry(110_000));
    }

    @Test public void ignoresOldPlannersAndAbandonedDeadlines() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertTrue(policy.takeLeasePlan(71_000));
        policy.observe(null, 72_000);
        policy.observe(true, 74_000);
        assertEquals(-1, policy.currentWake());
        policy.observe(false, 80_000);
        policy.observe(true, 81_000);
        assertTrue(policy.takeLeasePlan(151_000));
        policy.acceptLeasePlan(1_000, 151_000, 172_000);
        assertFalse(policy.takeLateRetry(172_000));
        policy.acceptLeasePlan(81_000, 152_000, 171_000);
        assertFalse(policy.takeLateRetry(177_000));
        assertFalse(policy.takeLateRetry(178_000));
    }

    @Test public void rejectsPlansOutsideStartupAndLateRepliesAfterSuspend() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        assertFalse(policy.takeLeasePlan(80_000));
        policy.observe(false, 100_000);
        policy.observe(true, 101_000);
        assertTrue(policy.takeLeasePlan(171_000));
        policy.acceptLeasePlan(101_000, 172_000, 160_000);
        assertFalse(policy.takeLateRetry(172_000));
        policy.acceptLeasePlan(101_000, 172_000, 301_001);
        policy.acceptLeasePlan(101_000, 201_001, 250_000);
        assertFalse(policy.takeLateRetry(250_000));
        policy.acceptLeasePlan(101_000, 172_000, 301_000);
        assertFalse(policy.takeLateRetry(301_001));
    }

    @Test public void reconnectCheckFiresOnceAfterTheNormalStartupWindow() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 1_000);
        for (long now = 5_000; now <= 35_000; now += 5_000) policy.observe(true, now);
        assertEquals(-1, policy.takeReconnectWake(35_999));
        assertEquals(0, policy.observe(true, 36_000));
        assertEquals(1_000, policy.takeReconnectWake(36_000));
        assertEquals(-1, policy.takeReconnectWake(36_001));
    }

    @Test public void missedOnObservationsDropPhonePlansButAnOffToOnWakeMayFollowSleep() {
        StartupWifiPolicy policy = new StartupWifiPolicy();
        policy.observe(false, 0);
        policy.observe(true, 500_000);
        assertEquals(500_000, policy.currentWake());
        policy.observe(true, 505_000);
        policy.observe(true, 510_001);
        assertEquals(-1, policy.currentWake());
        assertEquals(-1, policy.takeReconnectWake(535_000));
        assertFalse(policy.takeLeasePlan(570_000));
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
