package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.time.Instant
import java.util.zip.ZipEntry
import java.util.zip.ZipFile

@RunWith(RobolectricTestRunner::class)
class BundleExporterProductionTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    private lateinit var context: Context
    private lateinit var database: ObdDatabase
    private lateinit var deviceRoot: File
    private lateinit var workRoot: File
    private lateinit var exporter: BundleExporter

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("obd_drives.db")
        database = ObdDatabase(context)
        deviceRoot = temporaryFolder.newFolder("device-obd")
        workRoot = temporaryFolder.newFolder("work")
        exporter = BundleExporter(context, database, deviceRoot = { deviceRoot }, workRoot = workRoot)
    }

    @After
    fun tearDown() {
        database.close()
        context.deleteDatabase("obd_drives.db")
    }

    @Test
    fun productionExporterAtomicallyPublishesValidatesHashesAndRecoversExistingFinal() {
        val driveId = "atomic-drive"
        createCompletedDrive(driveId)
        val ready = File(deviceRoot, "ready").apply { mkdirs() }
        val partial = File(ready, "$driveId.obd2.zip.partial").apply { writeText("stale") }

        val exported = exporter.export(driveId)
        assertFalse(partial.exists())
        assertTrue(exported.file.isFile)
        assertEquals("$driveId.obd2.zip", exported.file.name)
        val firstBytes = exported.file.readBytes()
        assertEquals(sha256(firstBytes), exported.sha256)

        ZipFile(exported.file).use { zip ->
            val entries = zip.entries().toList()
            assertEquals(
                listOf("manifest.json", "samples.ndjson.gz", "diagnostics.json", "summary.json"),
                entries.map { it.name },
            )
            assertTrue(entries.all { !it.isDirectory && it.method == ZipEntry.STORED })
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use {
                JSONObject(it.readText())
            }
            assertEquals(1, manifest.getInt("sample_count"))
            assertEquals(1, manifest.getInt("diagnostic_count"))
            assertEquals(2, manifest.getInt("poll_plan_version"))
            assertEquals("complete", manifest.getString("completion_status"))
            for (name in listOf("samples.ndjson.gz", "diagnostics.json", "summary.json")) {
                val entry = zip.getEntry(name)
                val expected = manifest.getJSONObject("files").getJSONObject(name)
                val bytes = zip.getInputStream(entry).use { it.readBytes() }
                assertEquals(entry.size, expected.getLong("size_bytes"))
                assertEquals(sha256(bytes), expected.getString("sha256"))
            }
            assertEquals(1, manifest.getJSONObject("files").getJSONObject("samples.ndjson.gz").getInt("record_count"))
            assertEquals(1, manifest.getJSONObject("files").getJSONObject("diagnostics.json").getInt("record_count"))
        }
        val row = database.drive(driveId)
        assertEquals("exported", row.getString("export_status"))
        assertEquals(exported.sha256, row.getString("bundle_sha256"))
        assertTrue(workRoot.listFiles().orEmpty().isEmpty())

        // A process restart after rename but before observing success takes this branch: validate
        // and hash the existing immutable final, then restore the same DB export receipt.
        val recovered = exporter.export(driveId)
        assertEquals(exported.sha256, recovered.sha256)
        assertArrayEquals(firstBytes, recovered.file.readBytes())
    }

    @Test
    fun interruptedOneSampleDriveExportsWithEvidenceBasedEndAndLifecycleMetadata() {
        val driveId = "interrupted-one"
        val start = Instant.parse("2025-03-02T00:00:00Z")
        database.startDrive(
            DriveRecord(
                driveId, "test-car", "test-adapter", "test-logger", "0.2.0", 1,
                start.toString(), "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord(
                driveId, 0, start.plusSeconds(5).toString(),
                mapOf("engine_rpm" to 900.0),
            ),
        )
        database.markFinalising(
            driveId,
            "connection_lost",
            start.plusSeconds(30).toString(),
            start.plusSeconds(20).toString(),
        )
        database.finalizeDrive(
            driveId,
            "connection_lost",
            start.plusSeconds(31).toString(),
            lastSuccessfulResponseAtUtc = start.plusSeconds(20).toString(),
        )

        val exported = exporter.export(driveId)
        ZipFile(exported.file).use { zip ->
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use {
                JSONObject(it.readText())
            }
            assertEquals("interrupted", manifest.getString("completion_status"))
            assertEquals("connection_lost", manifest.getString("interruption_reason"))
            assertEquals(start.plusSeconds(5).toString(), manifest.getString("finish_time_utc"))
            assertEquals(start.plusSeconds(5).toString(), manifest.getString("last_sample_at_utc"))
            assertEquals(
                start.plusSeconds(20).toString(),
                manifest.getString("last_successful_obd_response_at_utc"),
            )
            assertEquals(start.plusSeconds(30).toString(), manifest.getString("termination_noticed_at_utc"))
            assertEquals(2, manifest.getInt("poll_plan_version"))
        }
    }

    @Test
    fun backwardWallJumpStillProducesOrderedExportLifecycleTimestamps() {
        val driveId = "clock-jump-export"
        val start = Instant.parse("2099-01-01T00:00:00Z")
        database.startDrive(
            DriveRecord(
                driveId, "test-car", "test-adapter", "test-logger", "0.2.0", 1,
                start.toString(), "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord(
                driveId,
                0,
                start.plusSeconds(5).toString(),
                mapOf("engine_rpm" to 900.0),
            ),
        )
        database.finalizeDrive(
            driveId,
            "process_terminated",
            noticedAtUtc = "2020-01-01T00:00:00Z",
        )

        val exported = exporter.export(driveId)
        ZipFile(exported.file).use { zip ->
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use {
                JSONObject(it.readText())
            }
            val finish = Instant.parse(manifest.getString("finish_time_utc"))
            val noticed = Instant.parse(manifest.getString("termination_noticed_at_utc"))
            val finalised = Instant.parse(manifest.getString("finalised_at_utc"))
            val created = Instant.parse(manifest.getString("created_at_utc"))
            assertFalse(noticed.isBefore(finish))
            assertFalse(finalised.isBefore(noticed))
            assertFalse(created.isBefore(finalised))
        }
    }

    @Test
    fun failedPartialReplacementNeverPublishesFinalOrMarksDriveExported() {
        val driveId = "failed-drive"
        createCompletedDrive(driveId)
        val ready = File(deviceRoot, "ready").apply { mkdirs() }
        val blockedPartial = File(ready, "$driveId.obd2.zip.partial").apply {
            mkdirs()
            resolve("blocker").writeText("keep directory non-empty")
        }

        assertThrows(IllegalStateException::class.java) { exporter.export(driveId) }
        assertTrue(blockedPartial.isDirectory)
        assertFalse(File(ready, "$driveId.obd2.zip").exists())
        val row = database.drive(driveId)
        assertEquals("waiting_for_backup", row.getString("export_status"))
        assertTrue(row.isNull("bundle_sha256"))
        assertFalse(File(deviceRoot, "receipts/$driveId.verified.json").exists())
        assertTrue(workRoot.listFiles().orEmpty().isEmpty())
    }

    @Test
    fun retentionUsesExactServerReceiptsAndRemovesReceiptOnlyAfterDbCommit() {
        val receipts = File(deviceRoot, "receipts").apply { mkdirs() }
        repeat(17) { index ->
            val driveId = "retention-%02d".format(index)
            createCompletedDrive(driveId, timeOffsetSeconds = index.toLong())
            val digest = sha256(driveId.toByteArray())
            database.recordVerifiedExport(driveId, digest)
            File(receipts, "$driveId.verified.json").writeText(
                """{"schema_version":1,"drive_id":"$driveId","bundle_sha256":"$digest"}""",
            )
        }

        assertEquals(1, exporter.enforceRetention())
        assertThrows(IllegalStateException::class.java) { database.drive("retention-00") }
        assertFalse(File(receipts, "retention-00.verified.json").exists())
        assertEquals("exported", database.drive("retention-16").getString("export_status"))
        assertTrue(File(receipts, "retention-16.verified.json").isFile)
    }

    private fun createCompletedDrive(driveId: String, timeOffsetSeconds: Long = 0) {
        val start = Instant.parse("2025-03-01T00:00:00Z").plusSeconds(timeOffsetSeconds)
        database.startDrive(
            DriveRecord(
                driveId = driveId,
                vehicleId = "test-car",
                adapterId = "test-adapter",
                loggerId = "test-logger",
                loggerVersion = "test",
                startedAtUtc = start.toString(),
                originalTimezone = "UTC",
                startReason = "test",
                obdProtocol = "test",
            ),
        )
        database.addSample(
            SampleRecord(
                driveId = driveId,
                sequence = 0,
                timestampUtc = start.plusSeconds(5).toString(),
                values = mapOf(
                    "engine_rpm" to 900.0,
                    "vehicle_speed" to 10.0,
                    "adapter_voltage" to 13.5,
                ),
            ),
        )
        database.addDiagnostic(
            driveId,
            "pending_dtcs",
            JSONObject().put("codes", org.json.JSONArray()),
            start.plusSeconds(5).toString(),
        )
        database.finishDrive(
            driveId,
            "engine_stopped",
            cleanEnd = true,
            finishedAtUtc = start.plusSeconds(10).toString(),
        )
    }
}
