package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
class LiveStatusSummaryTest {
    private val now = Instant.parse("2025-01-01T12:00:00Z").toEpochMilli()

    private fun status(vararg overrides: Pair<String, Any?>): JSONObject {
        val base = PublicStatus(
            state = "logging",
            ownershipEnabled = true,
            currentDriveId = "drive-42",
            adapterConnected = true,
            ecuConnected = true,
            engineRunning = true,
            vehicleState = "driving",
            batteryVoltage = 14.26,
            batteryVoltageFresh = true,
            batteryVoltageQuality = "measured",
            wifiConnected = false,
            lastSampleAtUtc = Instant.ofEpochMilli(now - 3_000).toString(),
        )
        val json = StatusPublisher.buildStatusJson(base, 2)
        json.put("updated_at_utc", Instant.ofEpochMilli(now - 5_000).toString())
        overrides.forEach { (key, value) -> json.put(key, value ?: JSONObject.NULL) }
        return json
    }

    @Test
    fun missingFileSaysSo() {
        assertEquals(listOf("Logger status: not published yet"), LiveStatusSummary.lines(null, now))
    }

    @Test
    fun healthyStatusReadsTopToBottom() {
        val lines = LiveStatusSummary.lines(status(), now)
        assertEquals("Logger: LOGGING · updated 5s", lines[0])
        assertEquals("Vehicle: driving · engine running", lines[1])
        assertEquals("Adapter: connected, ECU responding", lines[2])
        assertEquals("Battery: 14.3 V · measured", lines[3])
        assertEquals("Drive: drive-42", lines[4])
        assertEquals("Last sample: 3s ago", lines[5])
        assertEquals("Bundles waiting for the server: 2", lines[6])
        assertEquals("Wi-Fi: off", lines[7])
        assertEquals(8, lines.size)
    }

    @Test
    fun staleFileIsFlaggedFirst() {
        val stale = status(
            "updated_at_utc" to Instant.ofEpochMilli(now - 10 * 60_000).toString(),
        )
        val first = LiveStatusSummary.lines(stale, now).first()
        assertTrue(first, first.startsWith("Logger: STALE (10m 0s)"))
        assertTrue(first, first.endsWith("last state logging"))
    }

    @Test
    fun nullsAndErrorsRenderWithoutLeakingJsonNull() {
        val lines = LiveStatusSummary.lines(
            status(
                "current_drive_id" to null,
                "battery_voltage" to null,
                "last_sample_at_utc" to null,
                "ecu_connected" to false,
                "adapter_connected" to false,
                "adapter_reachable" to true,
                "last_error" to "adapter dropped",
                "last_error_at_utc" to Instant.ofEpochMilli(now - 65_000).toString(),
                "ingestion_sleep_hold" to true,
                "wifi_connected" to true,
            ),
            now,
        )
        assertFalse(lines.toString(), lines.any { it.contains("null") })
        assertTrue(lines.contains("Adapter: reachable, not connected"))
        assertTrue(lines.contains("Battery: unknown"))
        assertTrue(lines.contains("Last sample: none yet"))
        assertTrue(lines.contains("Wi-Fi: connected · sleep held for backup"))
        assertEquals("Last error (1m 5s ago): adapter dropped", lines.last())
    }

    @Test
    fun ageTextScalesUnits() {
        assertEquals("never", LiveStatusSummary.ageText(null))
        assertEquals("0s", LiveStatusSummary.ageText(-500))
        assertEquals("59s", LiveStatusSummary.ageText(59_999))
        assertEquals("2m 3s", LiveStatusSummary.ageText(123_000))
        assertEquals("1h 1m", LiveStatusSummary.ageText(3_660_000))
    }
}
