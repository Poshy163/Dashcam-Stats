package com.dashcamstats.obdlogger

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.After
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
import java.util.zip.ZipFile

@RunWith(RobolectricTestRunner::class)
class ObdPollPlanProvenanceTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()
    private lateinit var context: Context
    private var database: ObdDatabase? = null

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("obd_drives.db")
        clearBackupDirectories()
        ObdDatabase.migrationRestoreMarker(context).delete()
    }

    @After
    fun tearDown() {
        database?.close()
        context.deleteDatabase("obd_drives.db")
        clearBackupDirectories()
        ObdDatabase.migrationRestoreMarker(context).delete()
    }

    @Test
    fun versionFourMigrationPreservesProducerPlanAndAllExistingSampleValues() {
        createHistoricalDatabase()
        val opened = ObdDatabase(context).also { database = it }
        assertEquals(5, opened.readableDatabase.version)
        for ((version, plan) in knownVersions) {
            val drive = opened.drive(legacyId(version))
            assertEquals(version, drive.getString("logger_version"))
            if (plan == null) assertTrue(drive.isNull("poll_plan_version"))
            else assertEquals(plan.toInt(), drive.getInt("poll_plan_version"))
            val sample = opened.samples(legacyId(version)).single()
            assertEquals(900.0, sample.getDouble("engine_rpm"), 0.0)
            assertEquals(1, sample.getInt("mil_on"))
            assertEquals(1, sample.getInt("dtc_count"))
            assertEquals("${legacyId(version)}-0", sample.getString("sample_id"))
        }
        assertFalse(ObdDatabase.migrationBackupDirectory(context).exists())

        // A delayed export after upgrading the APK keeps the producer's old plan/version.
        val exporter = exporter(opened)
        val exported = exporter.export(legacyId("0.3.4"))
        assertManifest(exported, 5, "0.3.4")
        val before = exported.file.readBytes()
        assertTrue(before.contentEquals(exporter.export(legacyId("0.3.4")).file.readBytes()))
    }

    @Test
    fun newDrivePlanSurvivesReopenAndExporterDoesNotSubstituteTheCurrentPlan() {
        var opened = ObdDatabase(context).also { database = it }
        for (plan in listOf(5, 6)) {
            val version = if (plan == 5) "0.3.4" else "0.3.5"
            opened.startDrive(DriveRecord(
                "recorded-$plan", "car", "adapter", "logger", version, 1,
                started, "UTC", "engine_detected", "ISO 9141-2", pollPlanVersion = plan,
            ))
            opened.addSample(SampleRecord(
                "recorded-$plan", 0, finished, mapOf("engine_rpm" to 900.0),
            ))
            opened.finishDrive("recorded-$plan", "engine_stopped", true, finished)
        }
        opened.startDrive(DriveRecord(
            "default-plan", "car", "adapter", "logger", "0.3.5", 1,
            started, "UTC", "engine_detected", "ISO 9141-2",
        ))
        opened.close()
        opened = ObdDatabase(context).also { database = it }
        assertEquals(6, opened.drive("default-plan").getInt("poll_plan_version"))
        for (plan in listOf(5, 6)) {
            assertEquals(plan, opened.drive("recorded-$plan").getInt("poll_plan_version"))
            assertManifest(exporter(opened).export("recorded-$plan"), plan, if (plan == 5) "0.3.4" else "0.3.5")
        }
    }

    @Test
    fun unknownLegacyProducerExportsWithoutInventingItsPollPlan() {
        createHistoricalDatabase()
        val opened = ObdDatabase(context).also { database = it }
        val driveId = legacyId("0.2.0")
        assertTrue(opened.drive(driveId).isNull("poll_plan_version"))
        ZipFile(exporter(opened).export(driveId).file).use { zip ->
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use { JSONObject(it.readText()) }
            val summary = zip.getInputStream(zip.getEntry("summary.json")).bufferedReader().use { JSONObject(it.readText()) }
            assertEquals("0.2.0", manifest.getString("logger_version"))
            assertFalse(manifest.has("poll_plan_version"))
            assertFalse(manifest.has("last_sample_at_utc"))
            assertFalse(summary.has("last_sample_at_utc"))
            assertEquals(1, summary.getInt("sample_count"))
            assertEquals(900.0, opened.samples(driveId).single().getDouble("engine_rpm"), 0.0)
        }
    }

    @Test
    fun failedAdditiveMigrationRestoresVersionFourAndRetriesWithoutLosingSamples() {
        createHistoricalDatabase()
        val failed = ObdDatabase(context, upgradeFailureForTest = { error("simulated migration failure") })
        database = failed
        assertThrows(IllegalStateException::class.java) { failed.writableDatabase }
        SQLiteDatabase.openDatabase(context.getDatabasePath("obd_drives.db").path, null, SQLiteDatabase.OPEN_READONLY).use { restored ->
            assertEquals(4, restored.version)
            restored.rawQuery("SELECT COUNT(*) FROM samples", null).use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(knownVersions.size, cursor.getInt(0))
            }
            restored.rawQuery("PRAGMA table_info(drives)", null).use { cursor ->
                while (cursor.moveToNext()) assertFalse(cursor.getString(1) == "poll_plan_version")
            }
        }
        val retry = ObdDatabase(context).also { database = it }
        assertEquals(5, retry.readableDatabase.version)
        assertEquals(5, retry.drive(legacyId("0.3.4")).getInt("poll_plan_version"))
        assertEquals(1, retry.samples(legacyId("0.3.4")).single().getInt("dtc_count"))
    }

    @Test
    fun earlierTargetBackupsRecoverMissingCorruptAndPendingRestoreDatabases() {
        for ((target, source, damage) in listOf(
            Triple(2, 1, "missing"), Triple(3, 2, "corrupt"),
            Triple(4, 3, "pending"), Triple(4, 3, "interrupted"),
        )) {
            createHistoricalDatabase(source)
            val backup = retainBackup(target, source)
            val main = context.getDatabasePath("obd_drives.db")
            if (damage == "corrupt") main.writeText("not a database")
            else if (damage != "pending") assertTrue(main.delete())
            if (damage == "pending") {
                ObdDatabase.migrationRestoreMarker(context).writeBytes(backup.resolve("READY.json").readBytes())
            }
            if (damage == "interrupted") {
                assertThrows(IllegalStateException::class.java) {
                    ObdDatabase(context, migrationFileFailureForTest = { phase ->
                        if (phase == "after_restore_main_replace") error("simulated power loss")
                    })
                }
                assertTrue(ObdDatabase.migrationRestoreMarker(context).isFile)
            }

            val recovered = ObdDatabase(context).also { database = it }

            assertEquals(5, recovered.readableDatabase.version)
            assertEquals(900.0, recovered.samples(legacyId("0.3.4")).single().getDouble("engine_rpm"), 0.0)
            assertEquals(5, recovered.drive(legacyId("0.3.4")).getInt("poll_plan_version"))
            assertFalse(backup.exists())
            assertFalse(ObdDatabase.migrationRestoreMarker(context).exists())
        }
    }

    @Test
    fun healthyVersionFourKeepsSamplesWrittenAfterAnOlderSuccessfulUpgrade() {
        createHistoricalDatabase(3)
        val oldBackup = retainBackup(target = 4, source = 3)
        SQLiteDatabase.openDatabase(context.getDatabasePath("obd_drives.db").path, null, SQLiteDatabase.OPEN_READWRITE).use { live ->
            live.execSQL("ALTER TABLE samples ADD COLUMN mil_on INTEGER")
            live.execSQL("ALTER TABLE samples ADD COLUMN dtc_count INTEGER")
            live.version = 4
            addLaterSample(live)
        }

        val upgraded = ObdDatabase(context).also { database = it }

        assertEquals(5, upgraded.readableDatabase.version)
        assertEquals(listOf(900.0, 1200.0), upgraded.samples(legacyId("0.3.4")).map { it.getDouble("engine_rpm") })
        assertFalse(oldBackup.exists())
        assertFalse(ObdDatabase.migrationBackupDirectory(context).exists())
    }

    @Test
    fun ambiguousOrMismatchedRetainedBackupsFailClosedWithoutChangingTheirSnapshots() {
        for (problem in listOf("multiple", "marker", "malformed")) {
            createHistoricalDatabase(3)
            val oldBackup = retainBackup(target = 4, source = 3)
            val original = oldBackup.resolve("main").readBytes()
            if (problem == "multiple") {
                val second = ObdDatabase.migrationBackupDirectory(context).apply { mkdir() }
                oldBackup.resolve("main").copyTo(second.resolve("main"))
                oldBackup.resolve("READY.json").copyTo(second.resolve("READY.json"))
            } else if (problem == "marker") {
                val marker = JSONObject(oldBackup.resolve("READY.json").readText()).put("main_sha256", "0".repeat(64))
                ObdDatabase.migrationRestoreMarker(context).writeText(marker.toString())
            } else {
                oldBackup.resolve("READY.json").writeText("invalid metadata")
            }
            assertTrue(context.getDatabasePath("obd_drives.db").delete())

            assertThrows(IllegalStateException::class.java) { ObdDatabase(context) }

            assertFalse(context.getDatabasePath("obd_drives.db").exists())
            assertTrue(original.contentEquals(oldBackup.resolve("main").readBytes()))
            for (target in 2..5) ObdDatabase.migrationBackupDirectory(context, target).deleteRecursively()
            ObdDatabase.migrationRestoreMarker(context).delete()
        }
    }

    @Test
    fun interruptedStaleBackupHandoffReopensOnlyFromTheMatchingLiveSnapshot() {
        createHistoricalDatabase(3)
        val oldBackup = retainBackup(target = 4, source = 3)
        SQLiteDatabase.openDatabase(context.getDatabasePath("obd_drives.db").path, null, SQLiteDatabase.OPEN_READWRITE).use { live ->
            live.execSQL("ALTER TABLE samples ADD COLUMN mil_on INTEGER")
            live.execSQL("ALTER TABLE samples ADD COLUMN dtc_count INTEGER")
            live.version = 4
            addLaterSample(live)
        }
        assertThrows(IllegalStateException::class.java) {
            ObdDatabase(context, migrationFileFailureForTest = { phase ->
                if (phase == "after_migration_backup_handoff") error("simulated power loss")
            })
        }
        assertTrue(oldBackup.isDirectory)
        assertTrue(ObdDatabase.migrationBackupDirectory(context).isDirectory)

        val recovered = ObdDatabase(context).also { database = it }

        assertEquals(5, recovered.readableDatabase.version)
        assertEquals(listOf(900.0, 1200.0), recovered.samples(legacyId("0.3.4")).map { it.getDouble("engine_rpm") })
        assertFalse(oldBackup.exists())
        assertFalse(ObdDatabase.migrationBackupDirectory(context).exists())
    }

    @Test
    fun interruptedAtomicRetirementWithPartialCleanupDoesNotBlockReopening() {
        createHistoricalDatabase(3)
        val oldBackup = retainBackup(target = 4, source = 3)
        SQLiteDatabase.openDatabase(context.getDatabasePath("obd_drives.db").path, null, SQLiteDatabase.OPEN_READWRITE).use { live ->
            live.execSQL("ALTER TABLE samples ADD COLUMN mil_on INTEGER")
            live.execSQL("ALTER TABLE samples ADD COLUMN dtc_count INTEGER")
            live.version = 4
            addLaterSample(live)
        }
        assertThrows(IllegalStateException::class.java) {
            ObdDatabase(context, migrationFileFailureForTest = { phase ->
                if (phase == "after_migration_backup_retirement_rename") error("simulated power loss")
            })
        }
        val retired = File(oldBackup.parentFile, oldBackup.name + ".retired")
        assertFalse(oldBackup.exists())
        assertTrue(retired.isDirectory)
        assertTrue(retired.resolve("main").delete())

        val recovered = ObdDatabase(context).also { database = it }

        assertEquals(5, recovered.readableDatabase.version)
        assertEquals(listOf(900.0, 1200.0), recovered.samples(legacyId("0.3.4")).map { it.getDouble("engine_rpm") })
        assertFalse(retired.exists())
    }

    private fun clearBackupDirectories() {
        for (target in 2..5) {
            val backup = ObdDatabase.migrationBackupDirectory(context, target)
            backup.deleteRecursively()
            File(backup.parentFile, backup.name + ".retired").deleteRecursively()
        }
    }

    private fun retainBackup(target: Int, source: Int): File {
        val main = context.getDatabasePath("obd_drives.db")
        val backup = ObdDatabase.migrationBackupDirectory(context, target).apply { check(mkdir()) }
        main.copyTo(backup.resolve("main"))
        backup.resolve("READY.json").writeText(JSONObject()
            .put("schema_version", 1).put("source_database_version", source)
            .put("main_size_bytes", main.length()).put("main_sha256", sha256(main.readBytes())).toString())
        return backup
    }

    private fun addLaterSample(live: SQLiteDatabase) {
        val driveId = legacyId("0.3.4")
        live.insertOrThrow("samples", null, ContentValues().apply {
            put("sample_id", "$driveId-1"); put("drive_id", driveId)
            put("timestamp_utc", "2026-10-19T00:00:10Z"); put("sequence", 1)
            put("ecu_data_status", "live"); put("engine_rpm", 1200.0)
            put("quality_json", """{"transport":"ok","parser":"ok","missing_pids":[]}""")
        })
    }

    private fun legacyId(version: String): String = "legacy-${version.replace('.', '-')}"

    private fun exporter(opened: ObdDatabase): BundleExporter {
        val root = temporaryFolder.newFolder()
        val work = temporaryFolder.newFolder()
        return BundleExporter(context, opened, deviceRoot = { root }, workRoot = work)
    }

    private fun assertManifest(exported: ExportedBundle, plan: Int, version: String) {
        ZipFile(exported.file).use { zip ->
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use { JSONObject(it.readText()) }
            assertEquals(plan, manifest.getInt("poll_plan_version"))
            assertEquals(version, manifest.getString("logger_version"))
        }
    }

    private fun createHistoricalDatabase(sourceVersion: Int = 4) {
        database?.close()
        database = null
        context.deleteDatabase("obd_drives.db")
        // Derive the complete schema without the one additive column, using only basic
        // SQLite DDL so this fixture also works with older platform SQLite versions.
        val schema = ObdDatabase(context).use { current ->
            current.readableDatabase.rawQuery(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' " +
                    "AND name <> 'android_metadata' " +
                    "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name",
                null,
            ).use { cursor ->
                buildList {
                    while (cursor.moveToNext()) {
                        var sql = cursor.getString(0).replace(Regex("poll_plan_version INTEGER,\\s*"), "")
                        if (sourceVersion < 4) sql = sql.replace(Regex("mil_on INTEGER, dtc_count INTEGER,\\s*"), "")
                        if (sourceVersion < 3) sql = sql.replace(
                            Regex(",\\s*last_sample_at_utc TEXT,.*?last_processing_error TEXT", RegexOption.DOT_MATCHES_ALL), "",
                        )
                        if (sourceVersion < 2) sql = sql.replace(
                            Regex("oxygen_sensors_present TEXT, obd_standard TEXT, distance_with_mil REAL,\\s*"), "",
                        )
                        add(sql)
                    }
                }
            }
        }
        context.deleteDatabase("obd_drives.db")
        SQLiteDatabase.openOrCreateDatabase(context.getDatabasePath("obd_drives.db"), null).use { legacy ->
            schema.forEach(legacy::execSQL)
            for ((version, _) in knownVersions) {
                val driveId = legacyId(version)
                legacy.insertOrThrow("drives", null, ContentValues().apply {
                    put("drive_id", driveId); put("vehicle_id", "car"); put("logger_id", "logger")
                    put("logger_version", version); put("start_time_utc", started); put("finish_time_utc", finished)
                    put("start_reason", "engine_detected"); put("stop_reason", "engine_stopped")
                    put("status", "complete"); put("sample_count", 1); put("clean_end", 1)
                    if (sourceVersion >= 3) {
                        put("last_sample_at_utc", finished); put("termination_noticed_at_utc", finished)
                        put("finalised_at_utc", finished)
                    }
                })
                legacy.insertOrThrow("samples", null, ContentValues().apply {
                    put("sample_id", "$driveId-0"); put("drive_id", driveId); put("timestamp_utc", finished)
                    put("sequence", 0); put("ecu_data_status", "live"); put("engine_rpm", 900.0)
                    if (sourceVersion >= 4) { put("mil_on", 1); put("dtc_count", 1) }
                    put("quality_json", """{"transport":"ok","parser":"ok","missing_pids":[]}""")
                })
            }
            legacy.version = sourceVersion
        }
    }

    companion object {
        private const val started = "2026-10-19T00:00:00Z"
        private const val finished = "2026-10-19T00:00:05Z"
        private val knownVersions = listOf("0.2.0" to null, "0.2.2" to 2, "0.2.3" to 3, "0.2.8" to 4, "0.3.4" to 5)
    }
}
