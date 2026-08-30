package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun statusV2AndMetricsAreFixedSchemaBoundedAndPayloadFree() {
        val metrics = PipelineMetrics(maximumCounter = 2)
        repeat(5) { metrics.commandRequested() }
        metrics.notificationReceived()
        metrics.sampleCreated()
        metrics.sampleQueued()
        metrics.samplePersisted()
        metrics.parserFailure(checksumFailure = true)
        val snapshot = metrics.snapshot()
        assertEquals(2L, snapshot.values.getValue("commands_requested"))
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
            ),
            pendingCount = 3,
        )
        assertEquals(2, status.getInt("schema_version"))
        assertEquals("ingestion_quiesce_v1", status.getJSONArray("capabilities").getString(0))
        assertEquals("drive-current", status.getString("current_drive_id"))
        assertEquals(3, status.getInt("pending_bundle_count"))
        assertEquals(1L, status.getJSONObject("metrics").getLong("maximum_queue_depth"))
        assertFalse(status.toString().contains("payload"))
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
