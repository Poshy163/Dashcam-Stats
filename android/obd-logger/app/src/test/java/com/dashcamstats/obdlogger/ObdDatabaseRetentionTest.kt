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
        ObdDatabase.migrationBackupDirectory(context).deleteRecursively()
        database = ObdDatabase(context)
    }

    @After
    fun tearDown() {
        database.close()
        context.deleteDatabase("obd_drives.db")
        ObdDatabase.migrationBackupDirectory(context).deleteRecursively()
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
        assertEquals(listOf("restart-drive"), database.recoverInterrupted())
        val recovered = database.drive("restart-drive")
        assertEquals("2025-02-01T10:00:10Z", recovered.getString("finish_time_utc"))
        assertEquals("device_restart", recovered.getString("stop_reason"))
        assertEquals("complete", recovered.getString("status"))
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
    fun versionOneUpgradePreservesDriveAndSamplesWhileAddingSlowPidColumns() {
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
                  'engine_started','complete',1)
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
            legacy.execSQL("PRAGMA user_version=1")
        }

        database = ObdDatabase(context)

        assertEquals("test-car", database.drive("legacy-drive").getString("vehicle_id"))
        val sample = database.samples("legacy-drive").single()
        assertEquals(850.0, sample.getDouble("engine_rpm"), 0.0)
        assertTrue(sample.isNull("oxygen_sensors_present"))
        assertTrue(sample.isNull("obd_standard"))
        assertTrue(sample.isNull("distance_with_mil"))
        assertEquals(
            2,
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
}
