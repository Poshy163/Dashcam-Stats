package com.dashcamstats.obdlogger

import android.bluetooth.BluetoothGatt
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class ElmBleLinkPolicyTest {
    @Test
    fun adapterLinkLeavesRadioAirtimeForTheCarPlayHotspot() {
        // The head unit's single Wi-Fi/Bluetooth radio also hosts the CarPlay hotspot. HIGH
        // (~11-15 ms connection events) is reserved for a measured need, not the ELM link.
        assertEquals(BluetoothGatt.CONNECTION_PRIORITY_BALANCED, ElmBleLinkPolicy.CONNECTION_PRIORITY)
        assertNotEquals(BluetoothGatt.CONNECTION_PRIORITY_HIGH, ElmBleLinkPolicy.CONNECTION_PRIORITY)
    }
}
