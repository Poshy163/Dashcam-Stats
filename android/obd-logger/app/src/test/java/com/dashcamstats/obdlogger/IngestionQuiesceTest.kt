package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.nio.file.Files
import java.nio.file.attribute.FileTime
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
class IngestionQuiesceTest {
    @Test
    fun strictRequestParsesAndExtraDuplicateOrMalformedFieldsBlockWithoutAck() {
        val root = Files.createTempDirectory("obd-quiesce-request").toFile()
        try {
            val control = IngestionQuiesceFiles.controlRoot(root).apply { mkdirs() }
            val request = IngestionQuiesceFiles.requestFile(root)
            writeRequest(root, validRequest())
            val parsed = read(root) as IngestionRequestRead.Valid
            assertEquals("019d1234-5678-7abc-8123-456789abcdef", parsed.request.requestId)
            assertEquals(IngestionQuiesceDecision.QUIESCE, ingestionQuiesceDecision(parsed))

            writeRequest(root, validRequest().dropLast(1) + ",\"extra\":true}")
            assertTrue(read(root) is IngestionRequestRead.Invalid)
            assertFalse(IngestionQuiesceFiles.acknowledgementFile(root).exists())

            writeRequest(
                root,
                validRequest().replace("\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1"),
            )
            assertTrue(read(root) is IngestionRequestRead.Invalid)

            writeRequest(
                root,
                validRequest().replace("019d1234-5678-7abc-8123-456789abcdef", "../unsafe"),
            )
            assertTrue(read(root) is IngestionRequestRead.Invalid)

            writeRequest(root, validRequest().replace("2026-08-30T00:00:00Z", "api_key=secret"))
            val redacted = read(root) as IngestionRequestRead.Invalid
            assertFalse(redacted.reason.contains("secret"))
            assertTrue(control.isDirectory)
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun freshMalformedRequestBlocksButCrashStaleMalformedRequestIsCleared() {
        val root = Files.createTempDirectory("obd-quiesce-malformed-stale").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, "{not-json")
            assertTrue(read(root) is IngestionRequestRead.Invalid)
            assertTrue(IngestionQuiesceFiles.requestFile(root).exists())

            writeRequest(root, validRequest())
            val request = (read(root) as IngestionRequestRead.Valid).request
            IngestionQuiesceFiles.publishReady(root, request, readyAtUtc = "2026-08-30T00:00:25Z")
            writeRequest(root, "{not-json", modifiedAtUtc = "2026-08-29T23:48:00Z")
            assertEquals(IngestionRequestRead.Absent, read(root))
            assertFalse(IngestionQuiesceFiles.requestFile(root).exists())
            assertFalse(IngestionQuiesceFiles.acknowledgementFile(root).exists())
            assertEquals(
                IngestionQuiesceDecision.RESUME,
                ingestionQuiesceDecision(read(root)),
            )
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun readyAckUsesExactSchemaRealBundleMetadataAndRemovalResumes() {
        val root = Files.createTempDirectory("obd-quiesce-ready").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, validRequest())
            val request = (read(root) as IngestionRequestRead.Valid).request
            val digest = "ab".repeat(32)
            IngestionQuiesceFiles.publishReady(
                root,
                request,
                IngestionAckMetadata(
                    driveId = "019d9999-5678-7abc-8123-456789abcdef",
                    lastSampleAtUtc = "2026-08-30T00:00:05Z",
                    bundleFilename = "019d9999-5678-7abc-8123-456789abcdef.obd2.zip",
                    bundleSha256 = digest,
                ),
                readyAtUtc = "2026-08-30T00:00:06Z",
            )
            assertTrue(IngestionQuiesceFiles.isReadyFor(root, request.requestId))
            val body = JSONObject(IngestionQuiesceFiles.acknowledgementFile(root).readText())
            assertEquals(
                setOf(
                    "schema_version", "request_id", "state", "ready_at_utc", "drive_id",
                    "last_sample_at_utc", "bundle_filename", "bundle_sha256", "error",
                ),
                body.keys().asSequence().toSet(),
            )
            assertEquals(digest, body.getString("bundle_sha256"))
            assertTrue(body.isNull("error"))

            assertTrue(IngestionQuiesceFiles.requestFile(root).delete())
            assertEquals(
                IngestionQuiesceDecision.RESUME,
                ingestionQuiesceDecision(IngestionQuiesceFiles.readRequest(root)),
            )
            assertTrue(IngestionQuiesceFiles.clearAcknowledgement(root))
            assertFalse(IngestionQuiesceFiles.acknowledgementFile(root).exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun sameRequestIdDeadlineRenewalKeepsTheReadyAckAndLoggerQuiesced() {
        val root = Files.createTempDirectory("obd-quiesce-renewal").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, validRequest())
            val original = (read(root) as IngestionRequestRead.Valid).request
            IngestionQuiesceFiles.publishReady(
                root,
                original,
                readyAtUtc = "2026-08-30T00:00:30Z",
            )

            writeRequest(
                root,
                requestJson("2026-08-30T00:00:45Z", "2026-08-30T00:10:15Z"),
                modifiedAtUtc = "2026-08-30T00:00:45Z",
            )
            val renewed = read(root, "2026-08-30T00:05:00Z")

            assertTrue(renewed is IngestionRequestRead.Valid)
            assertEquals(original.requestId, (renewed as IngestionRequestRead.Valid).request.requestId)
            assertTrue(IngestionQuiesceFiles.isReadyFor(root, original.requestId))
            assertEquals(IngestionQuiesceDecision.QUIESCE, ingestionQuiesceDecision(renewed))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun parkedReadyHasNoFabricatedDriveAndFailedAckRedactsAndBoundsError() {
        val root = Files.createTempDirectory("obd-quiesce-parked").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, validRequest())
            val request = (read(root) as IngestionRequestRead.Valid).request
            IngestionQuiesceFiles.publishReady(root, request, readyAtUtc = "2026-08-30T00:00:06Z")
            var body = JSONObject(IngestionQuiesceFiles.acknowledgementFile(root).readText())
            assertTrue(body.isNull("drive_id"))
            assertTrue(body.isNull("bundle_filename"))

            val secretLike = "authorization=secret\n00:11:22:33:44:55\t" + "x".repeat(400)
            IngestionQuiesceFiles.publishFailed(
                root,
                request,
                secretLike,
                failedAtUtc = "2026-08-30T00:00:07Z",
            )
            assertTrue(IngestionQuiesceFiles.isFailedFor(root, request.requestId))
            body = JSONObject(IngestionQuiesceFiles.acknowledgementFile(root).readText())
            val error = body.getString("error")
            assertTrue(error.length <= 240)
            assertFalse(error.contains("secret"))
            assertFalse(error.contains("00:11:22:33:44:55"))
            assertFalse(error.any { it.code < 32 })
            assertEquals("failed", body.getString("state"))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun expiredOverallLeaseAtomicallyClearsRequestAndReadyAckThenResumes() {
        val root = Files.createTempDirectory("obd-quiesce-expired").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, validRequest())
            val request = (read(root) as IngestionRequestRead.Valid).request
            IngestionQuiesceFiles.publishReady(
                root,
                request,
                readyAtUtc = "2026-08-30T00:00:40Z",
            )

            val expired = read(root, "2026-08-30T00:02:01Z")

            assertEquals(IngestionRequestRead.Absent, expired)
            assertFalse(IngestionQuiesceFiles.requestFile(root).exists())
            assertFalse(IngestionQuiesceFiles.acknowledgementFile(root).exists())
            assertEquals(IngestionQuiesceDecision.RESUME, ingestionQuiesceDecision(expired))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun clockSkewAndLeaseBoundsCannotCreateAnUnboundedPause() {
        val root = Files.createTempDirectory("obd-quiesce-clock-skew").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            val requestFile = IngestionQuiesceFiles.requestFile(root)

            // A small future offset is accepted, avoiding a false expiry for normal clock skew.
            writeRequest(root, requestJson("2026-08-30T00:01:00Z", "2026-08-30T00:02:00Z"))
            assertTrue(read(root, "2026-08-30T00:00:30Z") is IngestionRequestRead.Valid)

            // A materially future request means the local clock moved backward; clear it.
            assertEquals(
                IngestionRequestRead.Absent,
                read(root, "2026-08-29T23:58:00Z"),
            )
            assertFalse(requestFile.exists())

            // A lease longer than the safety horizon is rejected and cleared, not held forever.
            writeRequest(root, requestJson("2026-08-30T00:00:00Z", "2026-08-30T00:10:01Z"))
            assertEquals(IngestionRequestRead.Absent, read(root))
            assertFalse(requestFile.exists())

            writeRequest(root, requestJson("2026-08-30T00:01:00Z", "2026-08-30T00:00:59Z"))
            assertEquals(IngestionRequestRead.Absent, read(root))
            assertFalse(requestFile.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun rebootClearsAnyPriorHoldBeforeLoggerRecovery() {
        val root = Files.createTempDirectory("obd-quiesce-reboot").toFile()
        try {
            IngestionQuiesceFiles.controlRoot(root).mkdirs()
            writeRequest(root, validRequest())
            val request = (read(root) as IngestionRequestRead.Valid).request
            IngestionQuiesceFiles.publishReady(root, request, readyAtUtc = "2026-08-30T00:00:40Z")

            assertTrue(IngestionQuiesceFiles.clearLeaseAfterBoot(root))
            assertFalse(IngestionQuiesceFiles.requestFile(root).exists())
            assertFalse(IngestionQuiesceFiles.acknowledgementFile(root).exists())
            assertEquals(IngestionRequestRead.Absent, read(root))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun statusV4IdentifiesTheBuildAndPublishesBoundedVoltageOnlyEvidence() {
        var monotonicMillis = 1_000L
        val metrics = PipelineMetrics(maximumCounter = 2) { monotonicMillis }
        repeat(5) { metrics.commandRequested(ElmCommandCategory.ADAPTER_LOCAL) }
        metrics.commandSent()
        metrics.commandCompleted(durationMillis = 120, voltageCommand = true)
        metrics.connectionAttempted()
        metrics.connectionSucceeded(340)
        metrics.voltageReadSucceeded("ATRV\r12.74 V\r>", 12.74, "2026-08-30T00:00:04Z")
        metrics.connectedSessionClosed(480)
        metrics.notificationReceived()
        metrics.sampleCreated()
        metrics.sampleQueued()
        metrics.samplePersisted()
        metrics.parserFailure(checksumFailure = true)
        monotonicMillis = 2_000L
        val snapshot = metrics.snapshot()
        assertEquals(2L, snapshot.values.getValue("commands_requested"))
        assertEquals(0L, snapshot.values.getValue("commands_blocked"))
        assertEquals(2L, snapshot.values.getValue("adapter_local_commands"))
        assertEquals(1L, snapshot.values.getValue("commands_sent"))
        assertEquals(0L, snapshot.values.getValue("vehicle_bus_commands"))
        assertEquals(1L, snapshot.values.getValue("voltage_reads_successful"))
        assertEquals(120.0, snapshot.timings.getValue("voltage_response_time").medianMillis!!, 0.0)
        assertEquals(48.0, snapshot.pollingDutyCyclePercent, 0.0)
        assertEquals(0L, snapshot.queueDepth)
        assertEquals(1L, snapshot.maximumQueueDepth)

        val status = StatusPublisher.buildStatusJson(
            PublicStatus(
                state = "ecu_online",
                ownershipEnabled = true,
                currentDriveId = "drive-current",
                ingestionRequestId = "request-1234",
                lastSampleAtUtc = "2026-08-30T00:00:05Z",
                metrics = snapshot,
                adapterReachable = true,
                batteryVoltage = 12.74,
                batteryVoltageSource = "dashcam_elm_atrv",
                batteryVoltageSampleAtUtc = "2026-08-30T00:00:04Z",
                batteryVoltageFresh = true,
                batteryVoltageRawResponse = "12.74 V",
                batteryVoltageQuality = "valid",
                bleOwner = "dashcam_voltage_only",
                voltageOnlyMode = true,
                wifiConnected = true,
                accStateKnown = true,
                accOn = false,
                ingestionSleepHoldKnown = true,
                ingestionSleepHold = true,
                sleepWindowPolicy = "managed_active",
                sleepWindowTargetSeconds = ACTIVE_SLEEP_WINDOW_SECONDS,
                sleepWindowObservedSeconds = ACTIVE_SLEEP_WINDOW_SECONDS,
                sleepWindowVerified = true,
            ),
            pendingCount = 3,
        )
        assertLandingIdentity(status)
        assertEquals("ingestion_quiesce_v1", status.getJSONArray("capabilities").getString(0))
        assertEquals("voltage_only_audit_v1", status.getJSONArray("capabilities").getString(1))
        assertEquals("adaptive_sleep_window_v1", status.getJSONArray("capabilities").getString(3))
        assertEquals("adaptive_sleep_window_v2", status.getJSONArray("capabilities").getString(4))
        assertEquals("app_event_stream_v1", status.getJSONArray("capabilities").getString(5))
        assertEquals("drive-current", status.getString("current_drive_id"))
        assertEquals(3, status.getInt("pending_bundle_count"))
        assertEquals(1L, status.getJSONObject("metrics").getLong("maximum_queue_depth"))
        assertEquals(0L, status.getJSONObject("metrics").getLong("vehicle_bus_commands"))
        assertEquals(12.74, status.getDouble("battery_voltage"), 0.0)
        assertEquals("12.74 V", status.getString("battery_voltage_raw_response"))
        assertEquals("dashcam_voltage_only", status.getString("ble_owner"))
        assertNotNull(Instant.parse(status.getString("updated_at_utc")))
        assertTrue(status.getBoolean("voltage_only_mode"))
        assertTrue(status.getBoolean("wifi_connected"))
        assertTrue(status.getBoolean("acc_state_known"))
        assertFalse(status.getBoolean("acc_on"))
        assertTrue(status.getBoolean("ingestion_sleep_hold_known"))
        assertTrue(status.getBoolean("ingestion_sleep_hold"))
        assertEquals("managed_active", status.getString("sleep_window_policy"))
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, status.getInt("sleep_window_target_s"))
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, status.getInt("sleep_window_observed_s"))
        assertTrue(status.getBoolean("sleep_window_verified"))
        assertTrue(status.isNull("sleep_window_error"))
        assertFalse(status.toString().contains("payload"))
        assertFalse(status.toString().contains("ATRV"))
    }

    @Test
    fun fallbackErrorStatusAlsoIdentifiesTheExactBuild() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val root = DeviceFiles.fallbackStatusRoot(context)
        root.deleteRecursively()
        try {
            StatusPublisher.storageUnavailable(
                context,
                PublicStatus(
                    state = "storage_unavailable",
                    ownershipEnabled = true,
                    lastError = "removable storage is unavailable",
                    lastErrorAtUtc = "2026-08-30T00:00:00Z",
                ),
            )

            val status = JSONObject(File(root, "status.json").readText())
            assertLandingIdentity(status)
            assertEquals("storage_unavailable", status.getString("state"))
            assertEquals("removable storage is unavailable", status.getString("last_error"))
            assertEquals(0, status.getInt("pending_bundle_count"))
        } finally {
            root.deleteRecursively()
        }
    }

    private fun assertLandingIdentity(status: JSONObject) {
        assertEquals(PUBLIC_STATUS_SCHEMA_VERSION, status.getInt("schema_version"))
        assertEquals(BuildConfig.VERSION_NAME, status.getString("app_version_name"))
        assertEquals(BuildConfig.VERSION_CODE, status.getInt("app_version_code"))
        assertEquals(ObdPollPlan.VERSION, status.getInt("poll_plan_version"))
        assertEquals(BuildConfig.BUILD_GIT_SHA, status.getString("build_git_sha"))
        assertTrue(
            BuildConfig.BUILD_GIT_SHA == "unknown" ||
                BuildConfig.BUILD_GIT_SHA.matches(Regex("[0-9a-f]{12}")),
        )
    }

    private fun validRequest(): String = requestJson(
        "2026-08-30T00:00:00Z",
        "2026-08-30T00:01:00Z",
    )

    private fun requestJson(requestedAtUtc: String, deadlineAtUtc: String): String =
        """{"schema_version":1,"request_id":"019d1234-5678-7abc-8123-456789abcdef","action":"prepare_for_ingest","requested_at_utc":"$requestedAtUtc","deadline_at_utc":"$deadlineAtUtc"}"""

    private fun writeRequest(
        root: File,
        body: String,
        modifiedAtUtc: String = "2026-08-30T00:00:20Z",
    ) {
        val request = IngestionQuiesceFiles.requestFile(root)
        request.writeText(body)
        Files.setLastModifiedTime(request.toPath(), FileTime.from(Instant.parse(modifiedAtUtc)))
    }

    private fun read(
        root: File,
        nowUtc: String = "2026-08-30T00:00:30Z",
    ): IngestionRequestRead = IngestionQuiesceFiles.readRequest(root, Instant.parse(nowUtc))
}
