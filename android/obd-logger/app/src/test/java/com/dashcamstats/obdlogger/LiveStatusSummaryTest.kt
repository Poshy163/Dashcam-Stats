package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.time.Instant
import java.io.File

@RunWith(RobolectricTestRunner::class)
class LiveStatusSummaryTest {
    private val now = Instant.parse("2025-01-01T12:00:00Z").toEpochMilli()
    @get:Rule
    val temporaryFolder = TemporaryFolder()

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
            batteryVoltageQuality = "valid",
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
        assertEquals("Battery: 14.3 V · valid", lines[3])
        assertEquals("Drive: recording", lines[4])
        assertEquals("Last sample: 3s ago", lines[5])
        assertEquals("Bundles waiting for the server: 2", lines[6])
        assertEquals("Movement report: starting · 0 clips (advisory only)", lines[7])
        assertEquals("Wi-Fi: off", lines[8])
        assertEquals(9, lines.size)
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
        assertEquals("Last error: reported (1m 5s ago)", lines.last())
    }

    @Test
    fun futureTimestampIsStaleInsteadOfLookingFresh() {
        val future = status("updated_at_utc" to Instant.ofEpochMilli(now + 60_000).toString())
        assertEquals("Logger: STALE (clock ahead) · last state logging", LiveStatusSummary.lines(future, now).first())
        val malformed = status("updated_at_utc" to "not a timestamp")
        assertEquals("Logger: STALE (never) · last state logging", LiveStatusSummary.lines(malformed, now).first())
    }

    @Test
    fun newestValidSnapshotWinsAcrossPrimaryAndFallback() {
        val primary = status("updated_at_utc" to Instant.ofEpochMilli(now - 120_000).toString())
        val fallback = status("updated_at_utc" to Instant.ofEpochMilli(now - 2_000).toString())
        assertSame(fallback, LiveStatusSummary.newestStatus(listOf(primary, fallback), now))
        assertSame(fallback, LiveStatusSummary.newestStatus(listOf(fallback, primary), now))
        val invalid = status("schema_version" to 999)
        val future = status("updated_at_utc" to Instant.ofEpochMilli(now + 60_000).toString())
        assertSame(fallback, LiveStatusSummary.newestStatus(listOf(invalid, future, fallback), now))
        // If a clock jump is the only available evidence, the panel can still explain it.
        assertSame(future, LiveStatusSummary.newestStatus(listOf(future), now))
        assertNull(LiveStatusSummary.newestStatus(listOf(JSONObject(), invalid), now))
    }

    @Test
    fun fileReaderSkipsMalformedAndOversizedStatusAndChecksBothVolumes() {
        val primary = temporaryFolder.newFolder("removable")
        val fallback = temporaryFolder.newFolder("fallback")
        File(primary, "status.json").writeText(status("updated_at_utc" to Instant.ofEpochMilli(now - 120_000).toString()).toString())
        File(fallback, "status.json").writeText(status().toString())
        val roots = listOf(primary, fallback)
        assertEquals("Logger: LOGGING · updated 5s", LiveStatusSummary.lines(LiveStatusSummary.readPublishedStatus(roots, now), now).first())
        File(primary, "status.json").writeText("{".repeat(65_537))
        assertTrue(LiveStatusSummary.readPublishedStatus(roots, now) != null)
        File(fallback, "status.json").writeText("{malformed")
        assertNull(LiveStatusSummary.readPublishedStatus(roots, now))
    }

    @Test
    fun detailsShowPublishedPipelineAndVerifiedSleepEvidence() {
        val json = status(
            "sleep_window_policy" to "managed_active",
            "sleep_window_target_s" to 1200,
            "sleep_window_observed_s" to 1200,
            "sleep_window_verified" to true,
            "acc_state_known" to true,
            "acc_on" to false,
            "ingestion_sleep_hold_known" to true,
            "ingestion_sleep_hold" to true,
            "metrics" to JSONObject()
                .put("commands_requested", 100).put("commands_sent", 99).put("commands_completed", 98)
                .put("samples_created", 40).put("samples_persisted", 39).put("samples_dropped", 1)
                .put("database_write_failures", 2).put("polling_duty_cycle_percent", 12.35),
        )
        val lines = LiveStatusSummary.detailLines(json)
        assertTrue(lines.contains("Sleep window · active Wi-Fi / backup"))
        assertTrue(lines.contains("Target: 1200 s · readback: 1200 s"))
        assertTrue(lines.contains("Verification: verified"))
        assertTrue(lines.contains("ACC: off · backup hold: active"))
        assertTrue(lines.contains("Commands requested / sent / completed: 100 / 99 / 98"))
        assertTrue(lines.contains("Samples created / persisted / dropped: 40 / 39 / 1"))
        assertTrue(lines.contains("Database write failures: 2"))
        assertTrue(lines.contains("Polling duty cycle: 12.4%"))
    }

    @Test
    fun malformedDetailsNeverClaimVerificationOrCoerceCounters() {
        val json = status(
            "sleep_window_target_s" to 0,
            "sleep_window_observed_s" to 10_000,
            "sleep_window_verified" to true,
            "metrics" to JSONObject()
                .put("commands_requested", "99").put("commands_sent", -1).put("commands_completed", 1.5)
                .put("database_write_failures", Long.MAX_VALUE)
                .put("polling_duty_cycle_percent", 101),
        )
        val lines = LiveStatusSummary.detailLines(json)
        assertTrue(lines.contains("Target: unknown · readback: unknown"))
        assertTrue(lines.contains("Verification: not verified"))
        assertTrue(lines.contains("Commands requested / sent / completed: unknown / unknown / unknown"))
        assertTrue(lines.contains("Database write failures: unknown"))
        assertTrue(lines.contains("Polling duty cycle: unknown"))
        assertEquals(listOf("Metrics: not published yet"), LiveStatusSummary.detailLines(null))
        assertTrue(LiveStatusSummary.detailLines(status("metrics" to "wrong type")).contains("Database write failures: unknown"))
    }

    @Test
    fun untrustedTextAndIdentifiersNeverEnterEitherPanel() {
        val secret = "token-secret-123 AA:BB:CC:DD:EE:FF"
        val json = status(
            "state" to secret,
            "vehicle_state" to secret,
            "current_drive_id" to secret,
            "last_error" to secret,
            "battery_voltage_quality" to secret,
            "sleep_window_policy" to secret,
            "sleep_window_error" to secret,
            "sleep_deadline_evidence" to JSONObject().put("boot_id", secret),
            "metrics" to JSONObject().put("commands_requested", secret).put("extra", secret),
        )
        val formatted = (LiveStatusSummary.lines(json, now) + LiveStatusSummary.detailLines(json)).joinToString("\n")
        assertFalse(formatted, formatted.contains(secret))
        assertFalse(formatted, formatted.contains("boot_id"))
        assertTrue(formatted.contains("Last error: reported"))
        assertTrue(formatted.contains("Sleep error: reported"))
        assertTrue(formatted.length < 2000)
    }

    @Test
    fun sleepMismatchAndKnownFailuresStayActionable() {
        val lines = LiveStatusSummary.detailLines(status(
            "sleep_window_target_s" to 1200,
            "sleep_window_observed_s" to 300,
            "sleep_window_verified" to true,
            "sleep_window_error" to "sleep countdown readback did not match the requested value",
        ))
        assertTrue(lines.contains("Verification: not verified"))
        assertTrue(lines.contains("Sleep error: readback mismatch"))
    }

    @Test
    fun realProducerRetentionFailureRemainsVisible() {
        val json = StatusPublisher.buildStatusJson(
            PublicStatus("retention_error", true, lastError = "bounded retention failure"), 0,
        ).put("updated_at_utc", Instant.ofEpochMilli(now).toString())
        assertSame(json, LiveStatusSummary.newestStatus(listOf(json), now))
        val lines = LiveStatusSummary.lines(json, now)
        assertEquals("Logger: RETENTION_ERROR · updated 0s", lines.first())
        assertTrue(lines.contains("Last error: reported"))
    }

    @Test
    fun voltageQualitiesFromProducerAreRecognized() {
        val at = Instant.ofEpochMilli(now)
        val probes = listOf(
            null,
            VoltageProbeSnapshot("valid", at.toString()),
            VoltageProbeSnapshot("valid", at.minusSeconds(600).toString()),
            VoltageProbeSnapshot("invalid", at.toString()),
            VoltageProbeSnapshot("failed", at.toString()),
        )
        for (probe in probes) {
            val producer = voltagePublicState(probe, at, 90)
            val json = status(
                "battery_voltage_quality" to producer.quality,
                "battery_voltage_fresh" to producer.fresh,
            )
            val batteryLine = LiveStatusSummary.lines(json, now).first { it.startsWith("Battery:") }
            assertTrue(batteryLine, batteryLine.endsWith(" · ${producer.quality}"))
            assertEquals(producer.quality == "valid", !batteryLine.contains("(old)"))
        }
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
