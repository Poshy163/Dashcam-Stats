package com.dashcamstats.obdlogger

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
class ObdDatabaseRetentionTest {
    private lateinit var context: Context
    private lateinit var database: ObdDatabase

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("obd_drives.db")
        clearMigrationArtifacts()
        database = ObdDatabase(context)
    }

    @After
    fun tearDown() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        clearMigrationArtifacts()
    }

    @Test
    fun oldestVerifiedExportsArePrunedTransactionallyInBoundedOrder() {
        repeat(19) { index -> createCompletedDrive(index, exported = true) }
        createCompletedDrive(30, exported = false)
        createRecordingDrive(31)

        val candidates = database.exportedRetentionCandidates(
            retainMostRecent = 16,
            maximumCandidates = 16,
        )
        assertEquals(listOf("drive-00", "drive-01", "drive-02"), candidates.map { it.driveId })

        // A receipt changed after selection must not cause its children to be removed.
        database.recordVerifiedExport("drive-00", digest(100))
        val deleted = database.pruneVerifiedExportedDrives(
            candidates.map { VerifiedExportedDrive(it.driveId, it.bundleSha256) },
            retainAtLeast = 16,
            maximumDeletes = 4,
        )
        assertEquals(listOf("drive-01", "drive-02"), deleted.map { it.driveId })
        assertEquals(1, database.samples("drive-00").size)
        assertEquals(1, database.diagnostics("drive-00").size)
        assertThrows(IllegalStateException::class.java) { database.drive("drive-01") }
        assertTrue(database.samples("drive-01").isEmpty())
        assertTrue(database.diagnostics("drive-01").isEmpty())
        assertThrows(IllegalStateException::class.java) { database.drive("drive-02") }

        // Complete but unexported and recording rows are never candidates or collateral damage.
        assertEquals("waiting_for_backup", database.drive("drive-30").getString("export_status"))
        assertEquals("recording", database.drive("drive-31").getString("status"))
        assertEquals("drive-30", database.lastCompletedDrive()!!.driveId)

        val retry = database.exportedRetentionCandidates(16, 4)
        assertEquals(listOf("drive-00"), retry.map { it.driveId })
        assertEquals(
            1,
            database.pruneVerifiedExportedDrives(
                listOf(VerifiedExportedDrive("drive-00", digest(100))),
                retainAtLeast = 16,
                maximumDeletes = 4,
            ).size,
        )
        assertTrue(database.exportedRetentionCandidates(16, 4).isEmpty())
        assertEquals("exported", database.drive("drive-18").getString("export_status"))
    }

    @Test
    fun invalidDigestAndNonEmptyCompletionAreEnforcedBeforeReceipt() {
        createRecordingDrive(40)
        assertThrows(IllegalArgumentException::class.java) {
            database.recordVerifiedExport("drive-40", "not-a-sha")
        }
        assertThrows(IllegalStateException::class.java) {
            database.recordVerifiedExport("drive-40", digest(40))
        }
    }

    @Test
    fun processRestartRecoversAtLastPersistedSampleAndKeepsRowsExportable() {
        val started = "2025-02-01T10:00:00Z"
        database.startDrive(
            DriveRecord(
                driveId = "restart-drive",
                vehicleId = "test-car",
                adapterId = "test-adapter",
                loggerId = "test-logger",
                loggerVersion = "test",
                startedAtUtc = started,
                originalTimezone = "UTC",
                startReason = "test",
                obdProtocol = "test",
            ),
        )
        database.addSample(
            SampleRecord(
                driveId = "restart-drive",
                sequence = 0,
                timestampUtc = "2025-02-01T10:00:05Z",
                values = mapOf("engine_rpm" to 900.0),
            ),
        )
        database.addSample(
            SampleRecord(
                driveId = "restart-drive",
                sequence = 1,
                timestampUtc = "2025-02-01T10:00:10Z",
                values = mapOf("engine_rpm" to 850.0),
            ),
        )
        database.addDiagnostic(
            "restart-drive",
            "pending_dtcs",
            org.json.JSONObject().put("codes", org.json.JSONArray()),
            "2025-02-01T10:00:07Z",
        )
        database.close()

        database = ObdDatabase(context)
        assertEquals(listOf("restart-drive"), database.recoverInterrupted().map { it.driveId })
        val recovered = database.drive("restart-drive")
        assertEquals("2025-02-01T10:00:10Z", recovered.getString("finish_time_utc"))
        assertEquals("device_restart", recovered.getString("stop_reason"))
        assertEquals("recovered", recovered.getString("status"))
        assertEquals("2025-02-01T10:00:10Z", recovered.getString("last_sample_at_utc"))
        assertFalse(recovered.isNull("termination_noticed_at_utc"))
        assertFalse(recovered.isNull("finalised_at_utc"))
        assertEquals(0, recovered.getInt("clean_end"))
        assertEquals(2, database.samples("restart-drive").size)
        assertEquals(1, database.diagnostics("restart-drive").size)
        assertEquals(listOf("restart-drive"), database.completedDriveIds())
        assertFalse(database.recoverInterrupted().isNotEmpty())

        assertEquals(
            1,
            database.readableDatabase.rawQuery("PRAGMA foreign_keys", null).use { cursor ->
                cursor.moveToFirst()
                cursor.getInt(0)
            },
        )
        assertEquals(
            "wal",
            database.readableDatabase.rawQuery("PRAGMA journal_mode", null).use { cursor ->
                cursor.moveToFirst()
                cursor.getString(0).lowercase()
            },
        )
    }

    @Test
    fun lifecycleMappingAndDuplicateFinalisationKeepOriginalEvidenceClocks() {
        createRecordingDrive(70)
        assertTrue(
            database.markFinalising(
                "drive-70",
                "connection_lost",
                "2025-01-01T00:02:00Z",
                "2025-01-01T00:01:30Z",
            ),
        )
        val first = database.finalizeDrive(
            "drive-70",
            "connection_lost",
            "2025-01-01T00:02:01Z",
        )
        assertEquals("interrupted", first.status)
        assertEquals("2025-01-01T00:01:10Z", first.finishTimeUtc)
        assertEquals("2025-01-01T00:01:30Z", first.lastSuccessfulResponseAtUtc)
        assertEquals("2025-01-01T00:02:00Z", first.terminationNoticedAtUtc)
        assertTrue(first.changed)

        val duplicate = database.finalizeDrive(
            "drive-70",
            "engine_stopped",
            "2025-01-02T00:00:00Z",
            "2025-01-02T00:00:00Z",
        )
        assertFalse(duplicate.changed)
        assertEquals(first.finishTimeUtc, duplicate.finishTimeUtc)
        assertEquals(first.terminationNoticedAtUtc, duplicate.terminationNoticedAtUtc)
        assertEquals(first.finalisedAtUtc, duplicate.finalisedAtUtc)
        assertEquals(first.lastSuccessfulResponseAtUtc, duplicate.lastSuccessfulResponseAtUtc)
        assertEquals("connection_lost", duplicate.stopReason)
        assertEquals("interrupted", database.drive("drive-70").getString("status"))

        createRecordingDrive(71)
        val clean = database.finalizeDrive(
            "drive-71",
            "engine_stopped",
            "2025-01-01T00:03:00Z",
            "2025-01-01T00:03:00Z",
        )
        assertEquals("complete", clean.status)
        assertEquals("2025-01-01T00:03:00Z", clean.finishTimeUtc)
    }

    @Test
    fun repeatedStartupRecoveryHandlesFinalisingOneSampleAndRetainsZeroSampleEvidence() {
        val start = "2025-04-01T00:00:00Z"
        database.startDrive(
            DriveRecord(
                "one-sample", "car", "adapter", "logger", "test", 1, start, "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord("one-sample", 0, "2025-04-01T00:00:05Z", mapOf("engine_rpm" to 800.0)),
        )
        database.markFinalising("one-sample", "ingestion_requested", "2025-04-01T00:00:06Z")

        database.startDrive(
            DriveRecord(
                "zero-sample", "car", "adapter", "logger", "test", 1,
                "2025-04-01T01:00:00Z", "UTC", "test", "test",
            ),
        )

        val recovered = database.recoverInterrupted().associateBy { it.driveId }
        assertEquals("interrupted", recovered.getValue("one-sample").status)
        assertEquals("ingestion_requested", recovered.getValue("one-sample").stopReason)
        assertEquals("2025-04-01T00:00:05Z", recovered.getValue("one-sample").finishTimeUtc)
        assertEquals("recovered", recovered.getValue("zero-sample").status)
        assertEquals("2025-04-01T01:00:00Z", recovered.getValue("zero-sample").finishTimeUtc)
        assertTrue(database.recoverInterrupted().isEmpty())

        assertEquals(listOf("one-sample"), database.prepareCompletedExports())
        assertEquals(
            "not_exportable_zero_samples",
            database.drive("zero-sample").getString("export_status"),
        )
        assertEquals(0, database.drive("zero-sample").getLong("sample_count"))
    }

    @Test
    fun nonBootStartupRecoversRecordingAsInterruptedProcessTermination() {
        createRecordingDrive(72)

        val recovered = database.recoverInterrupted("process_terminated").single()

        assertEquals("interrupted", recovered.status)
        assertEquals("process_terminated", recovered.stopReason)
        assertEquals("process_terminated", database.drive("drive-72").getString("interruption_reason"))
        assertTrue(database.recoverInterrupted("process_terminated").isEmpty())
    }

    @Test
    fun finalizationClampsNoticeAndFinalizedClocksAfterBackwardWallJump() {
        val start = "2099-01-01T00:00:00Z"
        database.startDrive(
            DriveRecord(
                "clock-jump", "car", "adapter", "logger", "test", 1,
                start, "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord(
                "clock-jump",
                0,
                "2099-01-01T00:00:05Z",
                mapOf("engine_rpm" to 800.0),
            ),
        )

        val finalised = database.finalizeDrive(
            "clock-jump",
            "process_terminated",
            // Simulate wall UTC being corrected backward after the sample was committed.
            noticedAtUtc = "2020-01-01T00:00:00Z",
        )

        assertEquals("2099-01-01T00:00:05Z", finalised.finishTimeUtc)
        assertEquals("2099-01-01T00:00:05Z", finalised.terminationNoticedAtUtc)
        assertFalse(
            Instant.parse(finalised.finalisedAtUtc)
                .isBefore(Instant.parse(finalised.terminationNoticedAtUtc)),
        )
    }

    @Test
    fun failedAfterPartialSampleDoesNotOverstateSuccessfulResponseEvidence() {
        val start = "2026-08-30T02:00:00Z"
        database.startDrive(
            DriveRecord(
                "partial-response", "car", "adapter", "logger", "test", 1,
                start, "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord(
                "partial-response", 0, "2026-08-30T02:00:05Z",
                mapOf("engine_rpm" to 800.0),
            ),
        )
        database.addSample(
            SampleRecord(
                "partial-response", 1, "2026-08-30T02:00:10Z",
                mapOf("engine_rpm" to 810.0),
                transportQuality = "failed_after_partial",
            ),
        )

        val drive = database.drive("partial-response")
        assertEquals("2026-08-30T02:00:10Z", drive.getString("last_sample_at_utc"))
        assertEquals(
            "2026-08-30T02:00:05Z",
            drive.getString("last_successful_response_at_utc"),
        )

        database.startDrive(
            DriveRecord(
                "partial-only", "car", "adapter", "logger", "test", 1,
                start, "UTC", "test", "test",
            ),
        )
        database.addSample(
            SampleRecord(
                "partial-only", 0, "2026-08-30T02:00:07Z",
                mapOf("engine_rpm" to 700.0),
                transportQuality = "failed_after_partial",
            ),
        )
        assertTrue(database.drive("partial-only").isNull("last_successful_response_at_utc"))
        val recovered = database.finalizeDrive(
            "partial-only",
            "process_terminated",
            "2026-08-30T02:00:08Z",
        )
        assertEquals(null, recovered.lastSuccessfulResponseAtUtc)
    }

    @Test
    fun versionOneUpgradePreservesDriveAndSamplesWhileAddingLifecycleAndSlowPidColumns() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        SQLiteDatabase.openOrCreateDatabase(context.getDatabasePath("obd_drives.db"), null).use {
            legacy ->
            legacy.execSQL(
                """
                CREATE TABLE drives(
                  drive_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, adapter_id TEXT,
                  logger_id TEXT NOT NULL, logger_version TEXT NOT NULL,
                  schema_version INTEGER NOT NULL DEFAULT 1,
                  start_time_utc TEXT NOT NULL, finish_time_utc TEXT, original_timezone TEXT,
                  start_reason TEXT NOT NULL, stop_reason TEXT, obd_protocol TEXT,
                  status TEXT NOT NULL, export_status TEXT NOT NULL DEFAULT 'waiting_for_backup',
                  bundle_sha256 TEXT, sample_count INTEGER NOT NULL DEFAULT 0,
                  error_count INTEGER NOT NULL DEFAULT 0, clean_end INTEGER NOT NULL DEFAULT 0
                )
                """.trimIndent(),
            )
            legacy.execSQL(
                """
                CREATE TABLE samples(
                  sample_id TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(drive_id),
                  timestamp_utc TEXT NOT NULL, sequence INTEGER NOT NULL,
                  ecu_data_status TEXT NOT NULL, engine_rpm REAL, vehicle_speed REAL,
                  coolant_temperature REAL, intake_air_temperature REAL, engine_load REAL,
                  throttle_position REAL, timing_advance REAL, mass_air_flow REAL,
                  short_term_fuel_trim_bank_1 REAL, long_term_fuel_trim_bank_1 REAL,
                  fuel_system_1 TEXT, oxygen_sensor_1_voltage REAL,
                  oxygen_sensor_1_short_term_fuel_trim REAL, oxygen_sensor_2_voltage REAL,
                  oxygen_sensor_2_short_term_fuel_trim REAL, adapter_voltage REAL,
                  estimated_fuel_rate REAL, estimated_fuel_consumption REAL,
                  quality_json TEXT NOT NULL, UNIQUE(drive_id, sequence)
                )
                """.trimIndent(),
            )
            legacy.execSQL(
                """
                CREATE TABLE diagnostics(
                  diagnostic_id TEXT PRIMARY KEY,
                  drive_id TEXT NOT NULL REFERENCES drives(drive_id),
                  timestamp_utc TEXT NOT NULL, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL
                )
                """.trimIndent(),
            )
            legacy.execSQL(
                """
                INSERT INTO drives(
                  drive_id,vehicle_id,logger_id,logger_version,start_time_utc,start_reason,
                  status,sample_count
                ) VALUES('legacy-drive','test-car','logger','v1','2025-01-01T00:00:00Z',
                  'engine_started','complete',2)
                """.trimIndent(),
            )
            legacy.execSQL(
                """
                INSERT INTO samples(
                  sample_id,drive_id,timestamp_utc,sequence,ecu_data_status,engine_rpm,quality_json
                ) VALUES('legacy-sample','legacy-drive','2025-01-01T00:00:05Z',0,'live',850.0,
                  '{"transport":"ok","parser":"ok","missing_pids":[]}')
                """.trimIndent(),
            )
            legacy.execSQL(
                """
                INSERT INTO samples(
                  sample_id,drive_id,timestamp_utc,sequence,ecu_data_status,engine_rpm,quality_json
                ) VALUES('legacy-partial','legacy-drive','2025-01-01T00:00:10Z',1,'live',860.0,
                  '{"transport":"failed_after_partial","parser":"partial","missing_pids":[]}')
                """.trimIndent(),
            )
            legacy.execSQL("PRAGMA user_version=1")
        }

        database = ObdDatabase(context)

        assertEquals("test-car", database.drive("legacy-drive").getString("vehicle_id"))
        val migratedDrive = database.drive("legacy-drive")
        assertEquals("2025-01-01T00:00:10Z", migratedDrive.getString("last_sample_at_utc"))
        assertEquals(
            "2025-01-01T00:00:05Z",
            migratedDrive.getString("last_successful_response_at_utc"),
        )
        val sample = database.samples("legacy-drive").first { it.getLong("sequence") == 0L }
        assertEquals(850.0, sample.getDouble("engine_rpm"), 0.0)
        assertTrue(sample.isNull("oxygen_sensors_present"))
        assertTrue(sample.isNull("obd_standard"))
        assertTrue(sample.isNull("distance_with_mil"))
        assertEquals(
            3,
            database.readableDatabase.rawQuery("PRAGMA user_version", null).use { cursor ->
                cursor.moveToFirst()
                cursor.getInt(0)
            },
        )
    }

    @Test
    fun failedUpgradeRestoresExactVersionOneDatabaseAndRetainsBackupForRetry() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        createMinimalVersionOneDatabase()
        database = ObdDatabase(context) { error("simulated migration failure") }

        assertThrows(IllegalStateException::class.java) { database.writableDatabase }

        SQLiteDatabase.openDatabase(
            context.getDatabasePath("obd_drives.db").path,
            null,
            SQLiteDatabase.OPEN_READONLY,
        ).use { restored ->
            assertEquals(1, restored.version)
            assertEquals(
                1,
                restored.rawQuery("SELECT COUNT(*) FROM samples", null).use { cursor ->
                    cursor.moveToFirst()
                    cursor.getInt(0)
                },
            )
        }
        val backup = ObdDatabase.migrationBackupDirectory(context)
        assertTrue(backup.resolve("main").isFile)

        database = ObdDatabase(context)
        val sample = database.samples("legacy-drive").single()
        assertEquals(850.0, sample.getDouble("engine_rpm"), 0.0)
        assertTrue(sample.isNull("distance_with_mil"))
        assertFalse(backup.exists())
    }

    @Test
    fun failedVersionTwoUpgradeAlsoRestoresExactDatabaseBeforeRetry() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        createMinimalVersionTwoDatabase()
        database = ObdDatabase(context) { error("simulated v2 migration failure") }

        assertThrows(IllegalStateException::class.java) { database.writableDatabase }

        SQLiteDatabase.openDatabase(
            context.getDatabasePath("obd_drives.db").path,
            null,
            SQLiteDatabase.OPEN_READONLY,
        ).use { restored -> assertEquals(2, restored.version) }
        val backup = ObdDatabase.migrationBackupDirectory(context)
        assertTrue(backup.resolve("main").isFile)

        database = ObdDatabase(context)
        assertEquals(850.0, database.samples("legacy-drive").single().getDouble("engine_rpm"), 0.0)
        assertTrue(database.drive("legacy-drive").isNull("last_sample_at_utc").not())
        assertFalse(backup.exists())
    }

    @Test
    fun stagedRestoreCopyFailureNeverRemovesLiveMainAndRetrySucceeds() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        clearMigrationArtifacts()
        createMinimalVersionOneDatabase()
        val main = context.getDatabasePath("obd_drives.db")
        database = ObdDatabase(
            context,
            migrationFileFailureForTest = { phase ->
                if (phase == "after_restore_stage_copy") error("simulated staged copy failure")
            },
            upgradeFailureForTest = { error("simulated migration failure") },
        )

        assertThrows(IllegalStateException::class.java) { database.writableDatabase }
        assertTrue(main.isFile)
        assertFalse(ObdDatabase.migrationRestoreMarker(context).exists())
        SQLiteDatabase.openDatabase(main.path, null, SQLiteDatabase.OPEN_READONLY).use { live ->
            assertEquals(1, live.version)
            assertEquals(
                1,
                live.rawQuery("SELECT COUNT(*) FROM samples", null).use { cursor ->
                    cursor.moveToFirst()
                    cursor.getInt(0)
                },
            )
        }

        database.close()
        database = ObdDatabase(context)
        assertEquals(850.0, database.samples("legacy-drive").single().getDouble("engine_rpm"), 0.0)
        assertFalse(ObdDatabase.migrationRestoreStaging(context).exists())
    }

    @Test
    fun powerLossAfterMainReplaceReplaysMarkerAndDiscardsWalAndShmOnRestart() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        clearMigrationArtifacts()
        createMinimalVersionOneDatabase()
        val main = context.getDatabasePath("obd_drives.db")
        database = ObdDatabase(
            context,
            migrationFileFailureForTest = { phase ->
                if (phase == "after_restore_main_replace") error("simulated power loss")
            },
            upgradeFailureForTest = { error("simulated migration failure") },
        )

        assertThrows(IllegalStateException::class.java) { database.writableDatabase }
        val marker = ObdDatabase.migrationRestoreMarker(context)
        assertTrue(marker.isFile)
        database.close()
        File(main.path + "-wal").writeBytes(byteArrayOf(1, 2, 3, 4))
        File(main.path + "-shm").writeBytes(byteArrayOf(5, 6, 7, 8))

        var observedCleanRestoreSet = false
        database = ObdDatabase(
            context,
            migrationFileFailureForTest = { phase ->
                if (phase == "after_restore_sidecar_cleanup") {
                    observedCleanRestoreSet = true
                    assertFalse(File(main.path + "-wal").exists())
                    assertFalse(File(main.path + "-shm").exists())
                }
            },
        )
        assertEquals(850.0, database.samples("legacy-drive").single().getDouble("engine_rpm"), 0.0)
        assertTrue(observedCleanRestoreSet)
        assertFalse(marker.exists())
    }

    @Test
    fun validBackupWinsWhenLiveMainIsMissingOrCorrupt() {
        for (damage in listOf("missing", "corrupt")) {
            database.close()
            context.deleteDatabase("obd_drives.db")
            clearMigrationArtifacts()
            createMinimalVersionOneDatabase()
            database = ObdDatabase(context) { error("simulated migration failure") }
            assertThrows(IllegalStateException::class.java) { database.writableDatabase }
            database.close()

            val main = context.getDatabasePath("obd_drives.db")
            if (damage == "missing") {
                assertTrue(main.delete())
            } else {
                main.writeBytes("not a sqlite database".toByteArray())
            }
            database = ObdDatabase(context)
            assertEquals(
                850.0,
                database.samples("legacy-drive").single().getDouble("engine_rpm"),
                0.0,
            )
        }
    }

    @Test
    fun diagnosticsDeduplicateOnlyConsecutiveValuesSoAtoBtoAIsPreserved() {
        createRecordingDrive(50)
        val driveId = "drive-50"
        val a = org.json.JSONObject().put("codes", org.json.JSONArray().put("P0001"))
        val b = org.json.JSONObject().put("codes", org.json.JSONArray().put("P0002"))

        assertTrue(database.addDiagnostic(driveId, "pending_dtcs", a, "2025-01-01T01:00:00Z"))
        assertFalse(database.addDiagnostic(driveId, "pending_dtcs", a, "2025-01-01T01:00:01Z"))
        assertTrue(database.addDiagnostic(driveId, "pending_dtcs", b, "2025-01-01T01:00:02Z"))
        assertTrue(database.addDiagnostic(driveId, "pending_dtcs", a, "2025-01-01T01:00:03Z"))

        val events = database.diagnostics(driveId).filter {
            it.getString("kind") == "pending_dtcs"
        }
        assertEquals(3, events.size)
        assertEquals(
            listOf("P0001", "P0002", "P0001"),
            events.map { it.getJSONObject("payload").getJSONArray("codes").getString(0) },
        )
        assertEquals(
            listOf(
                "2025-01-01T01:00:00Z",
                "2025-01-01T01:00:02Z",
                "2025-01-01T01:00:03Z",
            ),
            events.map { it.getString("timestamp_utc") },
        )

        val completion = org.json.JSONObject().put(
            "modes", org.json.JSONArray(listOf(3, 7, 10)),
        )
        assertTrue(
            database.addDiagnostic(
                driveId, "dtc_scan_complete", completion, "2025-01-01T01:00:04Z",
            ),
        )
        assertTrue(
            database.addDiagnostic(
                driveId, "dtc_scan_complete", completion, "2025-01-01T01:00:05Z",
            ),
        )
        assertEquals(
            2,
            database.diagnostics(driveId).count {
                it.getString("kind") == "dtc_scan_complete"
            },
        )
    }

    @Test
    fun missingExportWithoutReceiptIsRearmedButReceiptOrPresentArchiveIsPreserved() {
        createCompletedDrive(60, exported = true)
        createCompletedDrive(61, exported = true)
        createCompletedDrive(62, exported = true)
        val root = File(context.cacheDir, "export-recovery-${System.nanoTime()}").apply {
            resolve("ready").mkdirs()
            resolve("receipts").mkdirs()
        }
        try {
            root.resolve("receipts/drive-61.verified.json").writeText(
                """{"schema_version":1,"drive_id":"drive-61","bundle_sha256":"${digest(61)}"}""",
                Charsets.UTF_8,
            )
            // A present malformed archive is deliberately left for operator recovery.
            root.resolve("ready/drive-62.obd2.zip").writeBytes(byteArrayOf(1, 2, 3))
            val exporter = BundleExporter(context, database, deviceRoot = { root })

            assertEquals(1, exporter.reconcileMissingExports())

            assertEquals("waiting_for_backup", database.drive("drive-60").getString("export_status"))
            assertTrue(database.drive("drive-60").isNull("bundle_sha256"))
            assertEquals("exported", database.drive("drive-61").getString("export_status"))
            assertEquals("exported", database.drive("drive-62").getString("export_status"))
            assertEquals(listOf("drive-60"), database.prepareCompletedExports())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun recoveryPagesPastSixtyFourValidOlderExports() {
        val root = File(context.cacheDir, "export-recovery-page-${System.nanoTime()}").apply {
            resolve("ready").mkdirs()
            resolve("receipts").mkdirs()
        }
        try {
            for (index in 100..165) {
                createCompletedDrive(index, exported = true)
                if (index < 165) {
                    val driveId = "drive-%02d".format(index)
                    root.resolve("receipts/$driveId.verified.json").writeText(
                        """{"schema_version":1,"drive_id":"$driveId","bundle_sha256":"${digest(index)}"}""",
                        Charsets.UTF_8,
                    )
                }
            }
            val exporter = BundleExporter(context, database, deviceRoot = { root })

            assertEquals(1, exporter.reconcileMissingExports())
            assertEquals("exported", database.drive("drive-164").getString("export_status"))
            assertEquals(
                "waiting_for_backup",
                database.drive("drive-165").getString("export_status"),
            )
        } finally {
            root.deleteRecursively()
        }
    }

    private fun createCompletedDrive(index: Int, exported: Boolean) {
        createRecordingDrive(index)
        val finished = Instant.parse("2025-01-01T00:00:00Z").plusSeconds(index.toLong()).toString()
        database.finishDrive("drive-%02d".format(index), "engine_stopped", true, finished)
        if (exported) database.recordVerifiedExport("drive-%02d".format(index), digest(index))
    }

    private fun createMinimalVersionOneDatabase() {
        SQLiteDatabase.openOrCreateDatabase(context.getDatabasePath("obd_drives.db"), null).use {
            legacy ->
            legacy.execSQL(
                "CREATE TABLE drives(drive_id TEXT PRIMARY KEY,vehicle_id TEXT NOT NULL,status TEXT NOT NULL)",
            )
            legacy.execSQL(
                """
                CREATE TABLE samples(
                  sample_id TEXT PRIMARY KEY,drive_id TEXT NOT NULL REFERENCES drives(drive_id),
                  timestamp_utc TEXT NOT NULL,sequence INTEGER NOT NULL,
                  ecu_data_status TEXT NOT NULL,engine_rpm REAL,quality_json TEXT NOT NULL,
                  UNIQUE(drive_id,sequence)
                )
                """.trimIndent(),
            )
            legacy.execSQL(
                "INSERT INTO drives VALUES('legacy-drive','test-car','complete')",
            )
            legacy.execSQL(
                """
                INSERT INTO samples VALUES(
                  'legacy-sample','legacy-drive','2025-01-01T00:00:05Z',0,'live',850.0,
                  '{"transport":"ok","parser":"ok","missing_pids":[]}'
                )
                """.trimIndent(),
            )
            legacy.version = 1
        }
    }

    private fun createMinimalVersionTwoDatabase() {
        createMinimalVersionOneDatabase()
        SQLiteDatabase.openDatabase(
            context.getDatabasePath("obd_drives.db").path,
            null,
            SQLiteDatabase.OPEN_READWRITE,
        ).use { legacy ->
            legacy.execSQL("ALTER TABLE samples ADD COLUMN oxygen_sensors_present TEXT")
            legacy.execSQL("ALTER TABLE samples ADD COLUMN obd_standard TEXT")
            legacy.execSQL("ALTER TABLE samples ADD COLUMN distance_with_mil REAL")
            legacy.version = 2
        }
    }

    private fun createRecordingDrive(index: Int) {
        val id = "drive-%02d".format(index)
        val started = Instant.parse("2025-01-01T00:00:00Z").plusSeconds(index.toLong()).toString()
        database.startDrive(
            DriveRecord(
                driveId = id,
                vehicleId = "test-car",
                adapterId = "test-adapter",
                loggerId = "test-logger",
                loggerVersion = "test",
                startedAtUtc = started,
                originalTimezone = "UTC",
                startReason = "test",
                obdProtocol = "test",
            ),
        )
        database.addSample(
            SampleRecord(
                driveId = id,
                sequence = 0,
                timestampUtc = started,
                values = mapOf("engine_rpm" to 800.0),
            ),
        )
        database.addDiagnostic(id, "test", org.json.JSONObject().put("index", index), started)
    }

    private fun digest(value: Int): String = value.toString(16).padStart(64, '0')

    private fun clearMigrationArtifacts() {
        val backup = ObdDatabase.migrationBackupDirectory(context)
        backup.deleteRecursively()
        ObdDatabase.migrationBackupStaging(context).deleteRecursively()
        val marker = ObdDatabase.migrationRestoreMarker(context)
        marker.delete()
        File(marker.parentFile, "${marker.name}.partial").delete()
        ObdDatabase.migrationRestoreStaging(context).delete()
    }
}
