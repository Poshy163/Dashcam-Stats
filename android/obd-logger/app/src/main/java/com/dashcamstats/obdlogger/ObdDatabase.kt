package com.dashcamstats.obdlogger

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Instant
import java.util.UUID
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream

data class DriveRecord(
    val driveId: String,
    val vehicleId: String,
    val adapterId: String,
    val loggerId: String,
    val loggerVersion: String,
    val schemaVersion: Int = 1,
    val startedAtUtc: String,
    val originalTimezone: String,
    val startReason: String,
    val obdProtocol: String,
)

data class SampleRecord(
    val driveId: String,
    val sequence: Long,
    val timestampUtc: String,
    val values: Map<String, Any>,
    val transportQuality: String = "ok",
    val parserQuality: String = "ok",
    val missingPids: List<Int> = emptyList(),
) {
    val sampleId: String = "$driveId-$sequence"
}

enum class ExportRecoveryAction { EXPORT, QUARANTINE_ZERO_SAMPLES, SKIP }

object ExportRecoveryPlanner {
    fun action(sampleCount: Long, exportStatus: String): ExportRecoveryAction = when {
        exportStatus != "waiting_for_backup" -> ExportRecoveryAction.SKIP
        sampleCount <= 0 -> ExportRecoveryAction.QUARANTINE_ZERO_SAMPLES
        else -> ExportRecoveryAction.EXPORT
    }
}

data class LastCompletedDrive(
    val driveId: String,
    val finishedAtUtc: String,
    val lastSampleAtUtc: String?,
)

data class DriveFinalization(
    val driveId: String,
    val status: String,
    val finishTimeUtc: String,
    val lastSampleAtUtc: String?,
    val lastSuccessfulResponseAtUtc: String?,
    val terminationNoticedAtUtc: String,
    val finalisedAtUtc: String,
    val stopReason: String,
    val sampleCount: Long,
    val changed: Boolean,
)

private data class ExistingDriveLifecycle(
    val status: String,
    val startTimeUtc: String,
    val finishTimeUtc: String?,
    val lastSampleAtUtc: String?,
    val lastSuccessfulResponseAtUtc: String?,
    val terminationNoticedAtUtc: String?,
    val finalisedAtUtc: String?,
    val stopReason: String?,
    val sampleCount: Long,
)

data class ExportedDriveRetentionCandidate(
    val driveId: String,
    val finishedAtUtc: String,
    val bundleSha256: String,
)

data class VerifiedExportedDrive(val driveId: String, val bundleSha256: String)

internal val TERMINAL_DRIVE_STATUSES = setOf("complete", "interrupted", "recovered", "failed")
internal const val OBD_DATABASE_SYNCHRONOUS_MODE = "FULL"

class ObdDatabase(
    context: Context,
    private val migrationFileFailureForTest: ((String) -> Unit)? = null,
    private val upgradeFailureForTest: (() -> Unit)? = null,
) : SQLiteOpenHelper(context, DATABASE_NAME, null, DATABASE_VERSION) {
    private val databaseFile = context.getDatabasePath(DATABASE_NAME)
    private val migrationBackup = prepareMigrationBackup(databaseFile, migrationFileFailureForTest)

    init {
        setWriteAheadLoggingEnabled(true)
    }

    @Synchronized
    override fun getWritableDatabase(): SQLiteDatabase = openWithMigrationRecovery {
        super.getWritableDatabase()
    }

    @Synchronized
    override fun getReadableDatabase(): SQLiteDatabase = openWithMigrationRecovery {
        super.getReadableDatabase()
    }

    private fun openWithMigrationRecovery(open: () -> SQLiteDatabase): SQLiteDatabase {
        try {
            val database = open()
            if (migrationBackup != null) {
                val integrity = database.rawQuery("PRAGMA quick_check", null).use { cursor ->
                    cursor.moveToFirst() && cursor.getString(0) == "ok"
                }
                check(database.version == DATABASE_VERSION && integrity) {
                    "upgraded OBD database failed its integrity check"
                }
                retireMigrationBackup(databaseFile, migrationBackup)
            }
            return database
        } catch (error: Throwable) {
            if (migrationBackup != null) {
                runCatching { super.close() }
                try {
                    restoreMigrationBackup(
                        databaseFile,
                        migrationBackup,
                        migrationFileFailureForTest,
                    )
                } catch (restoreError: Throwable) {
                    error.addSuppressed(restoreError)
                }
            }
            throw error
        }
    }

    override fun onConfigure(db: SQLiteDatabase) {
        super.onConfigure(db)
        db.setForeignKeyConstraintsEnabled(true)
        db.rawQuery("PRAGMA busy_timeout=30000", null).close()
        // The head unit can lose power abruptly when its external supply is removed.  In WAL
        // mode NORMAL may acknowledge a sample and still roll that transaction back after a
        // hard power loss.  Samples are only written every five seconds, so prefer the bounded
        // fsync cost of FULL and do not claim a row is persisted before it is power-loss durable.
        db.execSQL("PRAGMA synchronous=$OBD_DATABASE_SYNCHRONOUS_MODE")
        val synchronousMode = db.rawQuery("PRAGMA synchronous", null).use { cursor ->
            check(cursor.moveToFirst()) { "OBD SQLite synchronous mode could not be read back" }
            cursor.getInt(0)
        }
        check(synchronousMode == 2) { "OBD SQLite refused synchronous=FULL" }
        val hasDriveSchema = db.rawQuery(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drives' LIMIT 1",
            null,
        ).use(Cursor::moveToFirst)
        if (!hasDriveSchema) {
            // Enable this only before first schema creation. Existing databases are deliberately
            // not converted with a blocking VACUUM; their freed pages are safely reused instead.
            db.execSQL("PRAGMA auto_vacuum=INCREMENTAL")
        }
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE drives(
              drive_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, adapter_id TEXT,
              logger_id TEXT NOT NULL, logger_version TEXT NOT NULL,
              schema_version INTEGER NOT NULL DEFAULT 1,
              start_time_utc TEXT NOT NULL, finish_time_utc TEXT, original_timezone TEXT,
              start_reason TEXT NOT NULL, stop_reason TEXT, obd_protocol TEXT,
              status TEXT NOT NULL, export_status TEXT NOT NULL DEFAULT 'waiting_for_backup',
              bundle_sha256 TEXT, sample_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0, clean_end INTEGER NOT NULL DEFAULT 0,
              last_sample_at_utc TEXT, last_successful_response_at_utc TEXT,
              termination_noticed_at_utc TEXT, finalised_at_utc TEXT,
              interruption_reason TEXT, last_processing_error TEXT
            )
            """.trimIndent(),
        )
        db.execSQL(
            """
            CREATE TABLE samples(
              sample_id TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(drive_id),
              timestamp_utc TEXT NOT NULL, sequence INTEGER NOT NULL, ecu_data_status TEXT NOT NULL,
              engine_rpm REAL, vehicle_speed REAL, coolant_temperature REAL,
              intake_air_temperature REAL, engine_load REAL, throttle_position REAL,
              timing_advance REAL, mass_air_flow REAL, short_term_fuel_trim_bank_1 REAL,
              long_term_fuel_trim_bank_1 REAL, fuel_system_1 TEXT,
              oxygen_sensor_1_voltage REAL, oxygen_sensor_1_short_term_fuel_trim REAL,
              oxygen_sensor_2_voltage REAL, oxygen_sensor_2_short_term_fuel_trim REAL,
              oxygen_sensors_present TEXT, obd_standard TEXT, distance_with_mil REAL,
              mil_on INTEGER, dtc_count INTEGER,
              adapter_voltage REAL, estimated_fuel_rate REAL, estimated_fuel_consumption REAL,
              quality_json TEXT NOT NULL, UNIQUE(drive_id, sequence)
            )
            """.trimIndent(),
        )
        db.execSQL("CREATE INDEX samples_drive_time ON samples(drive_id,timestamp_utc,sequence)")
        db.execSQL(
            """
            CREATE TABLE diagnostics(
              diagnostic_id TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(drive_id),
              timestamp_utc TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL
            )
            """.trimIndent(),
        )
        db.execSQL(
            "CREATE INDEX diagnostics_drive_kind_time " +
                "ON diagnostics(drive_id,kind,timestamp_utc,diagnostic_id)",
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2 && newVersion >= 2) {
            db.execSQL("ALTER TABLE samples ADD COLUMN oxygen_sensors_present TEXT")
            db.execSQL("ALTER TABLE samples ADD COLUMN obd_standard TEXT")
            db.execSQL("ALTER TABLE samples ADD COLUMN distance_with_mil REAL")
        }
        if (oldVersion < 3 && newVersion >= 3) {
            db.execSQL("ALTER TABLE drives ADD COLUMN last_sample_at_utc TEXT")
            db.execSQL("ALTER TABLE drives ADD COLUMN last_successful_response_at_utc TEXT")
            db.execSQL("ALTER TABLE drives ADD COLUMN termination_noticed_at_utc TEXT")
            db.execSQL("ALTER TABLE drives ADD COLUMN finalised_at_utc TEXT")
            db.execSQL("ALTER TABLE drives ADD COLUMN interruption_reason TEXT")
            db.execSQL("ALTER TABLE drives ADD COLUMN last_processing_error TEXT")
            if (tableHasColumn(db, "samples", "timestamp_utc")) {
                db.execSQL(
                    """
                    UPDATE drives SET
                      last_sample_at_utc=(
                        SELECT timestamp_utc FROM samples WHERE samples.drive_id=drives.drive_id
                        ORDER BY sequence DESC LIMIT 1
                      ),
                      last_successful_response_at_utc=(
                        SELECT timestamp_utc FROM samples WHERE samples.drive_id=drives.drive_id
                          AND quality_json NOT LIKE '%"transport":"failed_after_partial"%'
                        ORDER BY sequence DESC LIMIT 1
                      )
                    """.trimIndent(),
                )
            }
            if (tableHasColumn(db, "drives", "finish_time_utc")) {
                db.execSQL(
                    "UPDATE drives SET termination_noticed_at_utc=finish_time_utc," +
                        "finalised_at_utc=finish_time_utc",
                )
            }
        }
        if (oldVersion < 4 && newVersion >= 4) {
            // Poll-plan v4 added PID 0x01 (mil_on, dtc_count) to the sample values without
            // adding their columns here. The first 0x01 poll of every drive then failed the
            // insert with "table samples has no column named dtc_count", the drive ended as
            // database_fault, and a three-minute trip became six twenty-second fragments.
            // Every sample value is a column; a new value needs its column in the same change.
            db.execSQL("ALTER TABLE samples ADD COLUMN mil_on INTEGER")
            db.execSQL("ALTER TABLE samples ADD COLUMN dtc_count INTEGER")
        }
        upgradeFailureForTest?.invoke()
        if (oldVersion !in 1 until newVersion || newVersion > DATABASE_VERSION) {
            throw IllegalStateException("unsupported OBD database upgrade $oldVersion -> $newVersion")
        }
    }

    companion object {
        private const val DATABASE_NAME = "obd_drives.db"
        private const val DATABASE_VERSION = 4
        private const val BACKUP_READY_NAME = "READY.json"
        private const val BACKUP_STAGING_NAME = ".obd-migration-v3.partial"
        private const val RESTORE_MARKER_SUFFIX = ".migration-restore.pending"
        private const val RESTORE_STAGING_SUFFIX = ".migration-restore.partial"
        private val volatileSidecarSuffixes = listOf("-wal", "-shm", "-journal")
        private val backupMetadataKeys = setOf(
            "schema_version",
            "source_database_version",
            "main_size_bytes",
            "main_sha256",
        )

        private data class DatabaseInspection(val version: Int)

        private data class ValidatedMigrationBackup(
            val main: File,
            val sourceVersion: Int,
            val sizeBytes: Long,
            val sha256: String,
        )

        /**
         * Resolve every interrupted restore before SQLiteOpenHelper can open the live path. A
         * validated old-version backup wins only when the live set is absent/corrupt or a durable
         * restore marker exists; a healthy current database wins and retires stale artifacts.
         */
        private fun prepareMigrationBackup(
            databaseFile: File,
            failureForTest: ((String) -> Unit)?,
        ): File? {
            databaseFile.parentFile?.mkdirs()
            val backup = migrationBackupDirectory(databaseFile)
            val marker = restoreMarker(databaseFile)
            var restoredFromBackup = false
            if (marker.isFile) {
                check(validateMigrationBackup(backup) != null) {
                    "OBD migration restore is pending but its validated backup is missing"
                }
                restoreMigrationBackup(databaseFile, backup, failureForTest)
                restoredFromBackup = true
            } else {
                Files.deleteIfExists(restoreStaging(databaseFile).toPath())
                Files.deleteIfExists(File(marker.parentFile, "${marker.name}.partial").toPath())
            }

            var live = inspectDatabase(databaseFile, readWrite = false)
            val existingBackup = validateMigrationBackup(backup)
            if (live == null && existingBackup != null) {
                restoreMigrationBackup(databaseFile, backup, failureForTest)
                restoredFromBackup = true
                live = inspectDatabase(databaseFile, readWrite = false)
            }
            if (live?.version == DATABASE_VERSION) {
                retireMigrationBackup(databaseFile, backup)
                return null
            }
            if (live == null || live.version !in 1 until DATABASE_VERSION) return null
            if (existingBackup != null) {
                check(existingBackup.sourceVersion == live.version) {
                    "existing OBD migration backup is for a different database version"
                }
                // A retained backup means a prior upgrade never reached its validated commit
                // point. Normalize the live set from that authoritative snapshot before retrying;
                // the old-schema app cannot have continued writing after the failed open.
                if (!restoredFromBackup) {
                    restoreMigrationBackup(databaseFile, backup, failureForTest)
                }
                return backup
            }
            val checkpointed = checkpointAndInspect(databaseFile)
            check(checkpointed.version == live.version) {
                "OBD database version changed while preparing its migration backup"
            }
            return createMigrationBackup(databaseFile, backup, checkpointed.version)
        }

        /**
         * Restore uses one checkpointed main database; WAL is never copied and SHM is never
         * treated as durable. The marker remains until main replacement, sidecar cleanup and a
         * final integrity check all complete, so any power loss is safely replayed on startup.
         */
        private fun restoreMigrationBackup(
            databaseFile: File,
            backup: File,
            failureForTest: ((String) -> Unit)? = null,
        ) {
            val validated = checkNotNull(validateMigrationBackup(backup)) {
                "OBD migration backup is missing or invalid"
            }
            val staging = restoreStaging(databaseFile)
            Files.deleteIfExists(staging.toPath())
            copySynced(validated.main, staging)
            check(staging.length() == validated.sizeBytes && hashFile(staging) == validated.sha256) {
                "staged OBD migration restore does not match its backup"
            }
            check(inspectDatabase(staging, readWrite = false)?.version == validated.sourceVersion) {
                "staged OBD migration restore failed validation"
            }
            failureForTest?.invoke("after_restore_stage_copy")

            val marker = restoreMarker(databaseFile)
            writeSyncedAtomic(
                marker,
                JSONObject()
                    .put("schema_version", 1)
                    .put("source_database_version", validated.sourceVersion)
                    .put("main_size_bytes", validated.sizeBytes)
                    .put("main_sha256", validated.sha256)
                    .toString()
                    .toByteArray(Charsets.UTF_8),
            )
            failureForTest?.invoke("after_restore_marker")

            // Never unlink the only live main database. The fully synced and validated staged
            // file replaces it in one same-directory atomic operation.
            Files.move(
                staging.toPath(),
                databaseFile.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
            syncDirectory(checkNotNull(databaseFile.parentFile))
            failureForTest?.invoke("after_restore_main_replace")

            for (suffix in volatileSidecarSuffixes) {
                Files.deleteIfExists(File(databaseFile.path + suffix).toPath())
            }
            syncDirectory(checkNotNull(databaseFile.parentFile))
            failureForTest?.invoke("after_restore_sidecar_cleanup")

            check(databaseFile.length() == validated.sizeBytes && hashFile(databaseFile) == validated.sha256) {
                "restored OBD database does not match its validated backup"
            }
            check(inspectDatabase(databaseFile, readWrite = false)?.version == validated.sourceVersion) {
                "restored OBD database failed its integrity check"
            }
            Files.deleteIfExists(marker.toPath())
            Files.deleteIfExists(File(marker.parentFile, "${marker.name}.partial").toPath())
            syncDirectory(checkNotNull(databaseFile.parentFile))
        }

        private fun createMigrationBackup(
            databaseFile: File,
            backup: File,
            sourceVersion: Int,
        ): File {
            val staging = backupStaging(databaseFile)
            staging.deleteRecursively()
            check(staging.mkdir()) { "could not create OBD migration backup staging directory" }
            try {
                val stagedMain = File(staging, "main")
                copySynced(databaseFile, stagedMain)
                val size = stagedMain.length()
                val digest = hashFile(stagedMain)
                check(inspectDatabaseOrThrow(stagedMain, readWrite = false).version == sourceVersion) {
                    "staged OBD migration backup failed its integrity check"
                }
                writeSyncedFile(
                    File(staging, BACKUP_READY_NAME),
                    JSONObject()
                        .put("schema_version", 1)
                        .put("source_database_version", sourceVersion)
                        .put("main_size_bytes", size)
                        .put("main_sha256", digest)
                        .toString()
                        .toByteArray(Charsets.UTF_8),
                )
                syncDirectory(staging)
                if (backup.exists()) backup.deleteRecursively()
                Files.move(staging.toPath(), backup.toPath(), StandardCopyOption.ATOMIC_MOVE)
                syncDirectory(checkNotNull(backup.parentFile))
                check(validateMigrationBackup(backup) != null) {
                    "published OBD migration backup failed validation"
                }
                return backup
            } catch (error: Throwable) {
                staging.deleteRecursively()
                throw IllegalStateException("could not create OBD database migration backup", error)
            }
        }

        private fun validateMigrationBackup(backup: File): ValidatedMigrationBackup? = runCatching {
            check(backup.isDirectory && !Files.isSymbolicLink(backup.toPath()))
            check(backup.listFiles()?.map(File::getName)?.toSet() == setOf("main", BACKUP_READY_NAME))
            val main = File(backup, "main")
            val ready = File(backup, BACKUP_READY_NAME)
            check(Files.isRegularFile(main.toPath(), LinkOption.NOFOLLOW_LINKS))
            check(Files.isRegularFile(ready.toPath(), LinkOption.NOFOLLOW_LINKS))
            check(ready.length() in 1..4_096)
            val metadata = JSONObject(ready.readText(Charsets.UTF_8))
            check(metadata.keys().asSequence().toSet() == backupMetadataKeys)
            check(metadata.getInt("schema_version") == 1)
            val sourceVersion = metadata.getInt("source_database_version")
            check(sourceVersion in 1 until DATABASE_VERSION)
            val size = metadata.getLong("main_size_bytes")
            val digest = metadata.getString("main_sha256")
            check(size > 0 && main.length() == size)
            check(sha256Pattern.matches(digest) && hashFile(main) == digest)
            check(inspectDatabase(main, readWrite = false)?.version == sourceVersion)
            ValidatedMigrationBackup(main, sourceVersion, size, digest)
        }.getOrNull()

        private fun checkpointAndInspect(databaseFile: File): DatabaseInspection {
            val database = SQLiteDatabase.openDatabase(
                databaseFile.path,
                null,
                SQLiteDatabase.OPEN_READWRITE,
            )
            return database.use { db ->
                check(databaseIntegrityIsOk(db)) { "source OBD database failed quick_check" }
                db.rawQuery("PRAGMA wal_checkpoint(TRUNCATE)", null).use { cursor ->
                    check(cursor.moveToFirst() && cursor.getInt(0) == 0) {
                        "source OBD WAL could not be checkpointed for migration"
                    }
                    if (cursor.columnCount >= 3) {
                        val logFrames = cursor.getInt(1)
                        val checkpointedFrames = cursor.getInt(2)
                        check(logFrames < 0 || logFrames == checkpointedFrames) {
                            "source OBD WAL checkpoint was incomplete"
                        }
                    }
                }
                check(databaseIntegrityIsOk(db)) { "checkpointed OBD database failed quick_check" }
                DatabaseInspection(db.version)
            }
        }

        private fun inspectDatabase(databaseFile: File, readWrite: Boolean): DatabaseInspection? {
            if (!Files.isRegularFile(databaseFile.toPath(), LinkOption.NOFOLLOW_LINKS)) return null
            return runCatching { inspectDatabaseOrThrow(databaseFile, readWrite) }.getOrNull()
        }

        private fun inspectDatabaseOrThrow(
            databaseFile: File,
            readWrite: Boolean,
        ): DatabaseInspection = SQLiteDatabase.openDatabase(
            databaseFile.path,
            null,
            if (readWrite) SQLiteDatabase.OPEN_READWRITE else SQLiteDatabase.OPEN_READONLY,
        ).use { database ->
            check(databaseIntegrityIsOk(database)) { "SQLite quick_check failed for ${databaseFile.name}" }
            DatabaseInspection(database.version)
        }

        private fun databaseIntegrityIsOk(database: SQLiteDatabase): Boolean =
            database.rawQuery("PRAGMA quick_check", null).use { cursor ->
                cursor.count == 1 && cursor.moveToFirst() && cursor.getString(0) == "ok"
            }

        private fun retireMigrationBackup(databaseFile: File, backup: File) {
            backup.deleteRecursively()
            backupStaging(databaseFile).deleteRecursively()
            Files.deleteIfExists(restoreStaging(databaseFile).toPath())
            val marker = restoreMarker(databaseFile)
            Files.deleteIfExists(marker.toPath())
            Files.deleteIfExists(File(marker.parentFile, "${marker.name}.partial").toPath())
            syncDirectory(checkNotNull(databaseFile.parentFile))
        }

        private fun copySynced(source: File, target: File) {
            FileInputStream(source).use { input ->
                FileOutputStream(target).use { output ->
                    val size = input.channel.size()
                    var position = 0L
                    while (position < size) {
                        val copied = input.channel.transferTo(position, size - position, output.channel)
                        check(copied > 0) { "OBD migration copy made no progress" }
                        position += copied
                    }
                    output.channel.force(true)
                }
            }
            check(source.length() == target.length()) { "OBD migration backup copy is incomplete" }
        }

        private fun writeSyncedFile(target: File, bytes: ByteArray) {
            FileOutputStream(target).use { output ->
                output.write(bytes)
                output.channel.force(true)
            }
        }

        private fun writeSyncedAtomic(target: File, bytes: ByteArray) {
            val partial = File(target.parentFile, "${target.name}.partial")
            Files.deleteIfExists(partial.toPath())
            writeSyncedFile(partial, bytes)
            Files.move(
                partial.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
            syncDirectory(checkNotNull(target.parentFile))
        }

        private fun hashFile(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            FileInputStream(file).use { input ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    digest.update(buffer, 0, count)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }

        private fun syncDirectory(directory: File) {
            // Android/Linux requires the containing directory metadata to be durable after each
            // marker or replacement rename. Host-side Robolectric runs on Windows and cannot open
            // a directory descriptor, so it validates the protocol without this platform syscall.
            if (!System.getProperty("java.runtime.name", "")
                    .orEmpty()
                    .contains("Android", ignoreCase = true)
            ) {
                return
            }
            val descriptor = android.system.Os.open(
                directory.path,
                android.system.OsConstants.O_RDONLY,
                0,
            )
            try {
                android.system.Os.fsync(descriptor)
            } finally {
                android.system.Os.close(descriptor)
            }
        }

        private fun migrationBackupDirectory(databaseFile: File): File = File(
            databaseFile.parentFile,
            "${databaseFile.name}.migration-backup-v1-to-v$DATABASE_VERSION",
        )

        private fun backupStaging(databaseFile: File): File =
            File(databaseFile.parentFile, BACKUP_STAGING_NAME)

        private fun restoreMarker(databaseFile: File): File =
            File(databaseFile.parentFile, databaseFile.name + RESTORE_MARKER_SUFFIX)

        private fun restoreStaging(databaseFile: File): File =
            File(databaseFile.parentFile, databaseFile.name + RESTORE_STAGING_SUFFIX)

        internal fun migrationBackupDirectory(context: Context): File =
            migrationBackupDirectory(context.getDatabasePath(DATABASE_NAME))

        internal fun migrationRestoreMarker(context: Context): File =
            restoreMarker(context.getDatabasePath(DATABASE_NAME))

        internal fun migrationRestoreStaging(context: Context): File =
            restoreStaging(context.getDatabasePath(DATABASE_NAME))

        internal fun migrationBackupStaging(context: Context): File =
            backupStaging(context.getDatabasePath(DATABASE_NAME))
    }

    private fun tableHasColumn(db: SQLiteDatabase, table: String, column: String): Boolean =
        db.rawQuery("PRAGMA table_info($table)", null).use { cursor ->
            val nameIndex = cursor.getColumnIndexOrThrow("name")
            while (cursor.moveToNext()) {
                if (cursor.getString(nameIndex) == column) return@use true
            }
            false
        }

    fun startDrive(record: DriveRecord) {
        val values = ContentValues().apply {
            put("drive_id", record.driveId)
            put("vehicle_id", record.vehicleId)
            put("adapter_id", record.adapterId)
            put("logger_id", record.loggerId)
            put("logger_version", record.loggerVersion)
            put("schema_version", record.schemaVersion)
            put("start_time_utc", record.startedAtUtc)
            put("original_timezone", record.originalTimezone)
            put("start_reason", record.startReason)
            put("obd_protocol", record.obdProtocol)
            put("status", "recording")
        }
        writableDatabase.insertOrThrow("drives", null, values)
    }

    fun addSample(sample: SampleRecord): Boolean {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val quality = JSONObject()
                .put("transport", sample.transportQuality)
                .put("parser", sample.parserQuality)
                .put("missing_pids", JSONArray(sample.missingPids))
            val values = ContentValues().apply {
                put("sample_id", sample.sampleId)
                put("drive_id", sample.driveId)
                put("timestamp_utc", sample.timestampUtc)
                put("sequence", sample.sequence)
                put("ecu_data_status", "live")
                put("quality_json", quality.toString())
                for ((key, value) in sample.values) {
                    when (value) {
                        // Whole numbers bind as integers so an INTEGER column reads back as one
                        // and exports as a JSON integer; the server refuses 3.0 for a count.
                        is Int -> put(key, value)
                        is Long -> put(key, value)
                        is Number -> put(key, value.toDouble())
                        is Boolean -> put(key, if (value) 1 else 0)
                        is String -> put(key, value)
                        is List<*> -> put(key, JSONArray(value).toString())
                    }
                }
            }
            val inserted = db.insertWithOnConflict(
                "samples",
                null,
                values,
                SQLiteDatabase.CONFLICT_IGNORE,
            )
            if (inserted == -1L) return false
            db.execSQL(
                """
                UPDATE drives SET sample_count=sample_count+1,
                  last_sample_at_utc=(
                    SELECT timestamp_utc FROM samples WHERE drive_id=?
                    ORDER BY sequence DESC LIMIT 1
                  ),
                  last_successful_response_at_utc=CASE
                    WHEN ?='failed_after_partial' THEN last_successful_response_at_utc
                    ELSE (
                      SELECT timestamp_utc FROM samples WHERE drive_id=?
                      ORDER BY sequence DESC LIMIT 1
                    )
                  END
                WHERE drive_id=? AND status='recording'
                """.trimIndent(),
                arrayOf(
                    sample.driveId,
                    sample.transportQuality,
                    sample.driveId,
                    sample.driveId,
                ),
            )
            db.setTransactionSuccessful()
            return true
        } finally {
            db.endTransaction()
        }
    }

    fun addDiagnostic(
        driveId: String,
        kind: String,
        payload: JSONObject,
        timestampUtc: String = Instant.now().toString(),
    ): Boolean {
        val text = payload.toString()
        val payloadHash = sha256(text.toByteArray())
        val values = ContentValues().apply {
            put("diagnostic_id", uuid7())
            put("drive_id", driveId)
            put("timestamp_utc", timestampUtc)
            put("kind", kind)
            put("payload_json", text)
            put("payload_sha256", payloadHash)
        }
        val db = writableDatabase
        db.beginTransaction()
        try {
            val observationKind = kind in setOf(
                "dtc_scan_complete",
                "freeze_frame_scan_complete",
                "mode09_probe_status",
                "dtc_mode_status",
                "readiness_scan_complete",
                "mode09_support_scan_complete",
            )
            val unchanged = !observationKind && db.rawQuery(
                """
                SELECT payload_sha256 FROM diagnostics
                WHERE drive_id=? AND kind=? ORDER BY timestamp_utc DESC,rowid DESC LIMIT 1
                """.trimIndent(),
                arrayOf(driveId, kind),
            ).use { cursor -> cursor.moveToFirst() && cursor.getString(0) == payloadHash }
            if (unchanged) {
                db.setTransactionSuccessful()
                return false
            }
            db.insertOrThrow("diagnostics", null, values)
            db.setTransactionSuccessful()
            return true
        } finally {
            db.endTransaction()
        }
    }

    fun incrementError(driveId: String) {
        writableDatabase.execSQL(
            "UPDATE drives SET error_count=error_count+1 WHERE drive_id=?",
            arrayOf(driveId),
        )
    }

    fun recordProcessingError(driveId: String, message: String) {
        writableDatabase.execSQL(
            "UPDATE drives SET last_processing_error=? WHERE drive_id=?",
            arrayOf(message.take(240), driveId),
        )
    }

    fun markFinalising(
        driveId: String,
        stopReason: String,
        noticedAtUtc: String = Instant.now().toString(),
        lastSuccessfulResponseAtUtc: String? = null,
    ): Boolean {
        val noticed = Instant.parse(noticedAtUtc).toString()
        val lastResponse = lastSuccessfulResponseAtUtc?.let { Instant.parse(it).toString() }
        check(lastResponse == null || !Instant.parse(lastResponse).isAfter(Instant.parse(noticed))) {
            "last successful response follows termination notice"
        }
        return writableDatabase.update(
            "drives",
            ContentValues().apply {
                put("status", "finalising")
                put("stop_reason", stopReason)
                put("termination_noticed_at_utc", noticed)
                if (lastResponse != null) put("last_successful_response_at_utc", lastResponse)
                if (driveTerminalPolicy(stopReason).status != "complete") {
                    put("interruption_reason", stopReason)
                }
            },
            "drive_id=? AND status='recording'",
            arrayOf(driveId),
        ) == 1
    }

    fun finishDrive(
        driveId: String,
        stopReason: String,
        cleanEnd: Boolean,
        finishedAtUtc: String = Instant.now().toString(),
    ): String = finalizeDrive(
        driveId = driveId,
        stopReason = stopReason,
        noticedAtUtc = finishedAtUtc,
        requestedFinishAtUtc = if (cleanEnd) finishedAtUtc else null,
    ).finishTimeUtc

    /**
     * Atomically and idempotently moves recording/finalising to one explicit terminal state.
     * Interrupted/recovered end time is evidence-based; the later notice/finalisation clocks are
     * retained separately and repeated calls return the original immutable result.
     */
    fun finalizeDrive(
        driveId: String,
        stopReason: String,
        noticedAtUtc: String = Instant.now().toString(),
        requestedFinishAtUtc: String? = null,
        lastSuccessfulResponseAtUtc: String? = null,
        finalisedAtUtc: String? = null,
    ): DriveFinalization {
        val noticed = Instant.parse(noticedAtUtc).toString()
        val requestedFinish = requestedFinishAtUtc?.let { Instant.parse(it).toString() }
        val suppliedLastResponse = lastSuccessfulResponseAtUtc?.let { Instant.parse(it).toString() }
        val suppliedFinalised = finalisedAtUtc?.let(Instant::parse)
        val db = writableDatabase
        db.beginTransaction()
        try {
            val existing = db.rawQuery(
                """
                SELECT status,start_time_utc,finish_time_utc,last_sample_at_utc,
                  last_successful_response_at_utc,
                  termination_noticed_at_utc,finalised_at_utc,stop_reason,sample_count
                FROM drives WHERE drive_id=?
                """.trimIndent(),
                arrayOf(driveId),
            ).use { cursor ->
                check(cursor.moveToFirst()) { "drive not found" }
                val status = cursor.getString(0)
                val start = cursor.getString(1)
                val finish = if (cursor.isNull(2)) null else cursor.getString(2)
                val persistedLast = if (cursor.isNull(3)) null else cursor.getString(3)
                val persistedLastResponse = if (cursor.isNull(4)) null else cursor.getString(4)
                val existingNoticed = if (cursor.isNull(5)) null else cursor.getString(5)
                val finalised = if (cursor.isNull(6)) null else cursor.getString(6)
                val existingReason = if (cursor.isNull(7)) null else cursor.getString(7)
                val count = cursor.getLong(8)
                ExistingDriveLifecycle(
                    status = status,
                    startTimeUtc = start,
                    finishTimeUtc = finish,
                    lastSampleAtUtc = persistedLast,
                    lastSuccessfulResponseAtUtc = persistedLastResponse,
                    terminationNoticedAtUtc = existingNoticed,
                    finalisedAtUtc = finalised,
                    stopReason = existingReason,
                    sampleCount = count,
                )
            }
            val existingStatus = existing.status
            val terminal = existingStatus in TERMINAL_DRIVE_STATUSES
            if (terminal) {
                val result = DriveFinalization(
                    driveId = driveId,
                    status = existingStatus,
                    finishTimeUtc = checkNotNull(existing.finishTimeUtc),
                    lastSampleAtUtc = existing.lastSampleAtUtc,
                    lastSuccessfulResponseAtUtc = existing.lastSuccessfulResponseAtUtc,
                    terminationNoticedAtUtc = checkNotNull(existing.terminationNoticedAtUtc),
                    finalisedAtUtc = checkNotNull(existing.finalisedAtUtc),
                    stopReason = checkNotNull(existing.stopReason),
                    sampleCount = existing.sampleCount,
                    changed = false,
                )
                db.setTransactionSuccessful()
                return result
            }
            check(existingStatus == "recording" || existingStatus == "finalising") {
                "drive has unsupported lifecycle state $existingStatus"
            }
            val queriedLast = db.rawQuery(
                "SELECT timestamp_utc FROM samples WHERE drive_id=? ORDER BY sequence DESC LIMIT 1",
                arrayOf(driveId),
            ).use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else null
            }
            val queriedResponseFallback = db.rawQuery(
                """
                SELECT timestamp_utc,quality_json FROM samples
                WHERE drive_id=? ORDER BY sequence DESC
                """.trimIndent(),
                arrayOf(driveId),
            ).use { cursor ->
                var responseTimestamp: String? = null
                while (cursor.moveToNext() && responseTimestamp == null) {
                    val transport = JSONObject(cursor.getString(1)).optString("transport", "ok")
                    if (transport != "failed_after_partial") responseTimestamp = cursor.getString(0)
                }
                responseTimestamp
            }
            val lastSample = queriedLast ?: existing.lastSampleAtUtc
            val start = existing.startTimeUtc
            val startInstant = Instant.parse(start)
            val effectiveReason = existing.stopReason
                ?.takeIf { existingStatus == "finalising" }
                ?: stopReason
            val effectivePolicy = driveTerminalPolicy(effectiveReason)
            val rawFinish = if (effectivePolicy.useLastValidSampleAsEnd) {
                lastSample ?: start
            } else {
                requestedFinish ?: noticed
            }
            val finishInstant = maxInstant(startInstant, Instant.parse(rawFinish))
            val finish = finishInstant.toString()
            val effectiveLastResponse = listOfNotNull(
                suppliedLastResponse,
                existing.lastSuccessfulResponseAtUtc,
                queriedResponseFallback,
            ).firstOrNull { !Instant.parse(it).isBefore(startInstant) }
            val effectiveNoticedInstant = maxInstant(
                startInstant,
                finishInstant,
                Instant.parse(existing.terminationNoticedAtUtc ?: noticed),
                effectiveLastResponse?.let(Instant::parse),
            )
            val effectiveNoticed = effectiveNoticedInstant.toString()
            val finalisedAt = maxInstant(
                suppliedFinalised ?: Instant.now(),
                effectiveNoticedInstant,
            ).toString()
            val values = ContentValues().apply {
                put("finish_time_utc", finish)
                put("last_sample_at_utc", lastSample)
                put("last_successful_response_at_utc", effectiveLastResponse)
                put("termination_noticed_at_utc", effectiveNoticed)
                put("finalised_at_utc", finalisedAt)
                put("stop_reason", effectiveReason)
                put("status", effectivePolicy.status)
                put("clean_end", if (effectivePolicy.cleanEnd) 1 else 0)
                if (effectivePolicy.status == "complete") putNull("interruption_reason")
                else put("interruption_reason", effectiveReason)
            }
            val updated = db.update(
                "drives",
                values,
                "drive_id=? AND status IN ('recording','finalising')",
                arrayOf(driveId),
            )
            check(updated == 1) { "drive finalisation lost its lifecycle race" }
            val result = DriveFinalization(
                driveId = driveId,
                status = effectivePolicy.status,
                finishTimeUtc = finish,
                lastSampleAtUtc = lastSample,
                lastSuccessfulResponseAtUtc = effectiveLastResponse,
                terminationNoticedAtUtc = effectiveNoticed,
                finalisedAtUtc = finalisedAt,
                stopReason = effectiveReason,
                sampleCount = existing.sampleCount,
                changed = true,
            )
            db.setTransactionSuccessful()
            return result
        } finally {
            db.endTransaction()
        }
    }

    fun recoverInterrupted(recordingReason: String = "device_restart"): List<DriveFinalization> {
        require(recordingReason == "device_restart" || driveTerminalPolicy(recordingReason).status == "interrupted")
        val pending = mutableListOf<Pair<String, String>>()
        readableDatabase.rawQuery(
            """
            SELECT drive_id,stop_reason FROM drives
            WHERE status IN ('recording','finalising') ORDER BY start_time_utc,drive_id
            """.trimIndent(),
            null,
        ).use { cursor ->
            while (cursor.moveToNext()) {
                pending += cursor.getString(0) to if (cursor.isNull(1)) {
                    recordingReason
                } else {
                    cursor.getString(1)
                }
            }
        }
        val now = Instant.now().toString()
        return pending.map { (driveId, reason) -> finalizeDrive(driveId, reason, now) }
    }

    fun completedDriveIds(): List<String> {
        val result = mutableListOf<String>()
        readableDatabase.rawQuery(
            """
            SELECT drive_id FROM drives
            WHERE status IN ('complete','interrupted','recovered','failed')
              AND export_status='waiting_for_backup' AND sample_count>0
            ORDER BY start_time_utc,drive_id
            """.trimIndent(),
            null,
        ).use { cursor -> while (cursor.moveToNext()) result += cursor.getString(0) }
        return result
    }

    fun prepareCompletedExports(): List<String> {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.rawQuery(
                """
                SELECT drive_id,sample_count,export_status FROM drives
                WHERE status IN ('complete','interrupted','recovered','failed')
                  AND export_status!='exported'
                ORDER BY start_time_utc,drive_id
                """.trimIndent(),
                null,
            ).use { cursor ->
                while (cursor.moveToNext()) {
                    if (
                        ExportRecoveryPlanner.action(cursor.getLong(1), cursor.getString(2)) ==
                        ExportRecoveryAction.QUARANTINE_ZERO_SAMPLES
                    ) {
                        db.execSQL(
                            "UPDATE drives SET export_status='not_exportable_zero_samples' WHERE drive_id=?",
                            arrayOf(cursor.getString(0)),
                        )
                    }
                }
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        return completedDriveIds()
    }

    fun lastCompletedDrive(): LastCompletedDrive? = readableDatabase.rawQuery(
        """
        SELECT drive_id,finish_time_utc,last_sample_at_utc FROM drives
        WHERE status IN ('complete','interrupted','recovered','failed')
          AND finish_time_utc IS NOT NULL
        ORDER BY julianday(finish_time_utc) DESC,drive_id DESC LIMIT 1
        """.trimIndent(),
        null,
    ).use { cursor ->
        if (cursor.moveToFirst()) {
            LastCompletedDrive(
                cursor.getString(0),
                cursor.getString(1),
                if (cursor.isNull(2)) null else cursor.getString(2),
            )
        }
        else null
    }

    /** Called only after BundleExporter validates the immutable archive and hashes its bytes. */
    fun recordVerifiedExport(driveId: String, bundleSha256: String) {
        require(sha256Pattern.matches(bundleSha256)) { "bundle SHA-256 is invalid" }
        val updated = writableDatabase.update(
            "drives",
            ContentValues().apply {
                put("export_status", "exported")
                put("bundle_sha256", bundleSha256)
            },
            "drive_id=? AND status IN ('complete','interrupted','recovered','failed') AND sample_count>0",
            arrayOf(driveId),
        )
        check(updated == 1) { "only a completed non-empty drive can be marked exported" }
    }

    fun exportedRecoveryCandidates(
        maximumCandidates: Int = 64,
        afterFinishedAtUtc: String? = null,
        afterDriveId: String? = null,
    ): List<ExportedDriveRetentionCandidate> {
        require(maximumCandidates in 1..256)
        require((afterFinishedAtUtc == null) == (afterDriveId == null))
        val result = mutableListOf<ExportedDriveRetentionCandidate>()
        val pageClause = if (afterFinishedAtUtc == null) "" else {
            """
              AND (julianday(finish_time_utc)>julianday(?)
                   OR (finish_time_utc=? AND drive_id>?))
            """.trimIndent()
        }
        val arguments = if (afterFinishedAtUtc == null) {
            arrayOf(maximumCandidates.toString())
        } else {
            arrayOf(
                afterFinishedAtUtc,
                afterFinishedAtUtc,
                checkNotNull(afterDriveId),
                maximumCandidates.toString(),
            )
        }
        readableDatabase.rawQuery(
            """
            SELECT drive_id,finish_time_utc,bundle_sha256 FROM drives
            WHERE status IN ('complete','interrupted','recovered','failed') AND export_status='exported'
              AND finish_time_utc IS NOT NULL AND bundle_sha256 IS NOT NULL
            $pageClause
            ORDER BY julianday(finish_time_utc),drive_id LIMIT ?
            """.trimIndent(),
            arguments,
        ).use { cursor ->
            while (cursor.moveToNext()) {
                val digest = cursor.getString(2)
                if (sha256Pattern.matches(digest)) {
                    result += ExportedDriveRetentionCandidate(
                        cursor.getString(0), cursor.getString(1), digest,
                    )
                }
            }
        }
        return result
    }

    /** Reset only the exact still-exported identity selected by reconciliation. */
    fun resetMissingExport(driveId: String, bundleSha256: String): Boolean =
        writableDatabase.update(
            "drives",
            ContentValues().apply {
                put("export_status", "waiting_for_backup")
                putNull("bundle_sha256")
            },
            "drive_id=? AND status IN ('complete','interrupted','recovered','failed') " +
                "AND export_status='exported' AND bundle_sha256=?",
            arrayOf(driveId, bundleSha256),
        ) == 1

    fun exportedRetentionCandidates(
        retainMostRecent: Int,
        maximumCandidates: Int,
    ): List<ExportedDriveRetentionCandidate> {
        require(retainMostRecent >= 1)
        require(maximumCandidates >= 1)
        val eligibleCount = readableDatabase.rawQuery(
            """
            SELECT COUNT(*) FROM drives
            WHERE status IN ('complete','interrupted','recovered','failed') AND export_status='exported'
              AND finish_time_utc IS NOT NULL AND bundle_sha256 IS NOT NULL
            """.trimIndent(),
            null,
        ).use { cursor ->
            check(cursor.moveToFirst())
            cursor.getInt(0)
        }
        val excess = (eligibleCount - retainMostRecent).coerceAtLeast(0)
        if (excess == 0) return emptyList()
        val result = mutableListOf<ExportedDriveRetentionCandidate>()
        readableDatabase.rawQuery(
            """
            SELECT drive_id,finish_time_utc,bundle_sha256 FROM drives
            WHERE status IN ('complete','interrupted','recovered','failed') AND export_status='exported'
              AND finish_time_utc IS NOT NULL AND bundle_sha256 IS NOT NULL
            ORDER BY julianday(finish_time_utc) ASC,drive_id ASC LIMIT ?
            """.trimIndent(),
            arrayOf(minOf(excess, maximumCandidates).toString()),
        ).use { cursor ->
            while (cursor.moveToNext()) {
                val digest = cursor.getString(2)
                if (sha256Pattern.matches(digest)) {
                    result += ExportedDriveRetentionCandidate(
                        driveId = cursor.getString(0),
                        finishedAtUtc = cursor.getString(1),
                        bundleSha256 = digest,
                    )
                }
            }
        }
        return result
    }

    /**
     * Deletes only rows whose verified receipt still matches, and deletes children first because
     * the v1 schema intentionally did not declare cascading foreign keys.
     */
    fun pruneVerifiedExportedDrives(
        verified: List<VerifiedExportedDrive>,
        retainAtLeast: Int,
        maximumDeletes: Int,
    ): List<VerifiedExportedDrive> {
        require(retainAtLeast >= 1)
        require(maximumDeletes >= 1)
        if (verified.isEmpty()) return emptyList()
        val db = writableDatabase
        val deleted = mutableListOf<VerifiedExportedDrive>()
        db.beginTransaction()
        try {
            for (candidate in verified.take(maximumDeletes)) {
                val eligibleCount = db.rawQuery(
                    """
                    SELECT COUNT(*) FROM drives
                    WHERE status IN ('complete','interrupted','recovered','failed')
                      AND export_status='exported'
                      AND finish_time_utc IS NOT NULL AND bundle_sha256 IS NOT NULL
                    """.trimIndent(),
                    null,
                ).use { cursor ->
                    check(cursor.moveToFirst())
                    cursor.getInt(0)
                }
                if (eligibleCount <= retainAtLeast) break
                val stillVerified = db.rawQuery(
                    """
                    SELECT 1 FROM drives
                    WHERE drive_id=? AND status IN ('complete','interrupted','recovered','failed')
                      AND export_status='exported'
                      AND finish_time_utc IS NOT NULL AND bundle_sha256=? LIMIT 1
                    """.trimIndent(),
                    arrayOf(candidate.driveId, candidate.bundleSha256),
                ).use(Cursor::moveToFirst)
                if (!stillVerified) continue
                db.delete("diagnostics", "drive_id=?", arrayOf(candidate.driveId))
                db.delete("samples", "drive_id=?", arrayOf(candidate.driveId))
                val removed = db.delete(
                    "drives",
                    "drive_id=? AND status IN ('complete','interrupted','recovered','failed') " +
                        "AND export_status='exported' AND bundle_sha256=?",
                    arrayOf(candidate.driveId, candidate.bundleSha256),
                )
                if (removed == 1) deleted += candidate
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        if (deleted.isNotEmpty()) reclaimDeletedPagesIfConfigured(db)
        return deleted
    }

    fun drive(driveId: String): JSONObject = readableDatabase.rawQuery(
        "SELECT * FROM drives WHERE drive_id=?",
        arrayOf(driveId),
    ).use { cursor ->
        check(cursor.moveToFirst()) { "drive not found" }
        cursor.toJson()
    }

    fun samples(driveId: String): List<JSONObject> = rows(
        "SELECT * FROM samples WHERE drive_id=? ORDER BY sequence",
        driveId,
    )

    fun forEachSample(driveId: String, action: (JSONObject) -> Unit): Int {
        var count = 0
        readableDatabase.rawQuery(
            "SELECT * FROM samples WHERE drive_id=? ORDER BY sequence",
            arrayOf(driveId),
        ).use { cursor ->
            while (cursor.moveToNext()) {
                action(cursor.toJson())
                count += 1
            }
        }
        return count
    }

    fun diagnostics(driveId: String): List<JSONObject> = rows(
        "SELECT * FROM diagnostics WHERE drive_id=? ORDER BY timestamp_utc,diagnostic_id",
        driveId,
    ).map { row ->
        JSONObject()
            .put("diagnostic_id", row.getString("diagnostic_id"))
            .put("drive_id", row.getString("drive_id"))
            .put("timestamp_utc", row.getString("timestamp_utc"))
            .put("kind", row.getString("kind"))
            .put("payload", JSONObject(row.getString("payload_json")))
    }

    /** A bounded FULL checkpoint used before the ingestion controller is allowed to cut radios. */
    fun checkpointForIngestion() {
        writableDatabase.rawQuery("PRAGMA wal_checkpoint(FULL)", null).use { cursor ->
            check(cursor.moveToFirst())
            check(cursor.getInt(0) == 0) { "OBD WAL checkpoint remained busy" }
        }
    }

    private fun rows(sql: String, argument: String): List<JSONObject> {
        val result = mutableListOf<JSONObject>()
        readableDatabase.rawQuery(sql, arrayOf(argument)).use { cursor ->
            while (cursor.moveToNext()) result += cursor.toJson()
        }
        return result
    }
}

private fun Cursor.toJson(): JSONObject {
    val value = JSONObject()
    for (index in 0 until columnCount) {
        when (getType(index)) {
            Cursor.FIELD_TYPE_NULL -> value.put(columnNames[index], JSONObject.NULL)
            Cursor.FIELD_TYPE_INTEGER -> value.put(columnNames[index], getLong(index))
            Cursor.FIELD_TYPE_FLOAT -> value.put(columnNames[index], getDouble(index))
            Cursor.FIELD_TYPE_STRING -> value.put(columnNames[index], getString(index))
            Cursor.FIELD_TYPE_BLOB -> error("OBD schema has no blob values")
        }
    }
    return value
}

private val random = SecureRandom()
private val sha256Pattern = Regex("^[0-9a-f]{64}$")

private fun maxInstant(vararg values: Instant?): Instant =
    checkNotNull(values.filterNotNull().maxOrNull())

private fun reclaimDeletedPagesIfConfigured(db: SQLiteDatabase) {
    // WAL checkpointing and page reclamation are separate. Keep both bounded and best-effort:
    // pruning already committed successfully, so maintenance failure must not misreport export.
    runCatching { db.rawQuery("PRAGMA wal_checkpoint(PASSIVE)", null).close() }
    runCatching {
        val autoVacuum = db.rawQuery("PRAGMA auto_vacuum", null).use { cursor ->
            check(cursor.moveToFirst())
            cursor.getInt(0)
        }
        if (autoVacuum == 2) {
            val freePages = db.rawQuery("PRAGMA freelist_count", null).use { cursor ->
                check(cursor.moveToFirst())
                cursor.getInt(0)
            }
            if (freePages > 0) db.execSQL("PRAGMA incremental_vacuum(${minOf(freePages, 256)})")
        }
    }
}

fun uuid7(nowMillis: Long = System.currentTimeMillis()): String {
    val bytes = ByteArray(16).also(random::nextBytes)
    for (index in 0 until 6) bytes[index] = (nowMillis ushr (40 - index * 8)).toByte()
    bytes[6] = ((bytes[6].toInt() and 0x0F) or 0x70).toByte()
    bytes[8] = ((bytes[8].toInt() and 0x3F) or 0x80).toByte()
    val buffer = ByteBuffer.wrap(bytes)
    return UUID(buffer.long, buffer.long).toString()
}

fun sha256(bytes: ByteArray): String =
    java.security.MessageDigest.getInstance("SHA-256").digest(bytes)
        .joinToString("") { "%02x".format(it) }
