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
import java.nio.file.StandardCopyOption
import java.security.SecureRandom
import java.time.Instant
import java.util.UUID
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile

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

data class LastCompletedDrive(val driveId: String, val finishedAtUtc: String)

data class ExportedDriveRetentionCandidate(
    val driveId: String,
    val finishedAtUtc: String,
    val bundleSha256: String,
)

data class VerifiedExportedDrive(val driveId: String, val bundleSha256: String)

class ObdDatabase(
    context: Context,
    private val upgradeFailureForTest: (() -> Unit)? = null,
) : SQLiteOpenHelper(context, DATABASE_NAME, null, DATABASE_VERSION) {
    private val databaseFile = context.getDatabasePath(DATABASE_NAME)
    private val migrationBackup = prepareMigrationBackup(databaseFile)

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
                migrationBackup.deleteRecursively()
            }
            return database
        } catch (error: Throwable) {
            if (migrationBackup != null) {
                runCatching { super.close() }
                try {
                    restoreMigrationBackup(databaseFile, migrationBackup)
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
        db.rawQuery("PRAGMA synchronous=NORMAL", null).close()
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
              error_count INTEGER NOT NULL DEFAULT 0, clean_end INTEGER NOT NULL DEFAULT 0
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
        if (oldVersion == 1 && newVersion >= 2) {
            db.execSQL("ALTER TABLE samples ADD COLUMN oxygen_sensors_present TEXT")
            db.execSQL("ALTER TABLE samples ADD COLUMN obd_standard TEXT")
            db.execSQL("ALTER TABLE samples ADD COLUMN distance_with_mil REAL")
            upgradeFailureForTest?.invoke()
            return
        }
        throw IllegalStateException("unsupported OBD database upgrade $oldVersion -> $newVersion")
    }

    companion object {
        private const val DATABASE_NAME = "obd_drives.db"
        private const val DATABASE_VERSION = 2
        private const val SQLITE_USER_VERSION_OFFSET = 60L
        private val sidecars = listOf("" to "main", "-wal" to "wal", "-shm" to "shm")

        private fun prepareMigrationBackup(databaseFile: File): File? {
            if (!databaseFile.isFile || sqliteHeaderVersion(databaseFile) != 1) return null
            val backup = File(
                databaseFile.parentFile,
                "${databaseFile.name}.migration-backup-v1-to-v$DATABASE_VERSION",
            )
            if (backup.isDirectory && File(backup, "main").isFile) return backup
            val staging = File(backup.parentFile, "${backup.name}.partial")
            staging.deleteRecursively()
            check(staging.mkdir()) { "could not create OBD migration backup staging directory" }
            try {
                for ((suffix, name) in sidecars) {
                    val source = File(databaseFile.path + suffix)
                    if (source.isFile) copySynced(source, File(staging, name))
                }
                check(File(staging, "main").isFile) { "OBD migration backup has no main database" }
                Files.move(staging.toPath(), backup.toPath(), StandardCopyOption.ATOMIC_MOVE)
            } catch (error: Throwable) {
                staging.deleteRecursively()
                throw IllegalStateException("could not create OBD database migration backup", error)
            }
            return backup
        }

        private fun restoreMigrationBackup(databaseFile: File, backup: File) {
            check(backup.isDirectory && File(backup, "main").isFile) {
                "OBD migration backup is missing"
            }
            for ((suffix, name) in sidecars) {
                val target = File(databaseFile.path + suffix)
                val saved = File(backup, name)
                Files.deleteIfExists(target.toPath())
                if (!saved.isFile) continue
                val partial = File(target.parentFile, "${target.name}.restore.partial")
                Files.deleteIfExists(partial.toPath())
                copySynced(saved, partial)
                Files.move(
                    partial.toPath(),
                    target.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING,
                )
            }
        }

        private fun copySynced(source: File, target: File) {
            FileInputStream(source).use { input ->
                FileOutputStream(target).use { output ->
                    input.channel.transferTo(0, input.channel.size(), output.channel)
                    output.fd.sync()
                }
            }
            check(source.length() == target.length()) { "OBD migration backup copy is incomplete" }
        }

        private fun sqliteHeaderVersion(databaseFile: File): Int? = runCatching {
            RandomAccessFile(databaseFile, "r").use { source ->
                if (source.length() < SQLITE_USER_VERSION_OFFSET + 4) return@use null
                val magic = ByteArray(16)
                source.readFully(magic)
                if (!magic.contentEquals("SQLite format 3\u0000".toByteArray())) return@use null
                source.seek(SQLITE_USER_VERSION_OFFSET)
                source.readInt()
            }
        }.getOrNull()

        internal fun migrationBackupDirectory(context: Context): File = File(
            context.getDatabasePath(DATABASE_NAME).parentFile,
            "$DATABASE_NAME.migration-backup-v1-to-v$DATABASE_VERSION",
        )
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
                        is Number -> put(key, value.toDouble())
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
                "UPDATE drives SET sample_count=sample_count+1 WHERE drive_id=? AND status='recording'",
                arrayOf(sample.driveId),
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

    fun finishDrive(
        driveId: String,
        stopReason: String,
        cleanEnd: Boolean,
        finishedAtUtc: String = Instant.now().toString(),
    ): String {
        writableDatabase.execSQL(
            """
            UPDATE drives SET finish_time_utc=?,stop_reason=?,status='complete',clean_end=?
            WHERE drive_id=? AND status='recording'
            """.trimIndent(),
            arrayOf(finishedAtUtc, stopReason, if (cleanEnd) 1 else 0, driveId),
        )
        return finishedAtUtc
    }

    fun recoverInterrupted(): List<String> {
        val ids = mutableListOf<String>()
        readableDatabase.rawQuery(
            "SELECT drive_id FROM drives WHERE status='recording' ORDER BY start_time_utc",
            null,
        ).use { cursor -> while (cursor.moveToNext()) ids += cursor.getString(0) }
        if (ids.isNotEmpty()) {
            writableDatabase.execSQL(
                """
                UPDATE drives SET finish_time_utc=COALESCE(
                    (SELECT MAX(timestamp_utc) FROM samples WHERE samples.drive_id=drives.drive_id),
                    start_time_utc
                ),stop_reason='device_restart',status='complete',clean_end=0
                WHERE status='recording'
                """.trimIndent(),
            )
        }
        return ids
    }

    fun completedDriveIds(): List<String> {
        val result = mutableListOf<String>()
        readableDatabase.rawQuery(
            """
            SELECT drive_id FROM drives
            WHERE status='complete' AND export_status='waiting_for_backup' AND sample_count>0
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
                WHERE status='complete' AND export_status!='exported'
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
        SELECT drive_id,finish_time_utc FROM drives
        WHERE status='complete' AND finish_time_utc IS NOT NULL
        ORDER BY julianday(finish_time_utc) DESC,drive_id DESC LIMIT 1
        """.trimIndent(),
        null,
    ).use { cursor ->
        if (cursor.moveToFirst()) LastCompletedDrive(cursor.getString(0), cursor.getString(1))
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
            "drive_id=? AND status='complete' AND sample_count>0",
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
            WHERE status='complete' AND export_status='exported'
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
            "drive_id=? AND status='complete' AND export_status='exported' AND bundle_sha256=?",
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
            WHERE status='complete' AND export_status='exported'
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
            WHERE status='complete' AND export_status='exported'
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
                    WHERE status='complete' AND export_status='exported'
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
                    WHERE drive_id=? AND status='complete' AND export_status='exported'
                      AND finish_time_utc IS NOT NULL AND bundle_sha256=? LIMIT 1
                    """.trimIndent(),
                    arrayOf(candidate.driveId, candidate.bundleSha256),
                ).use(Cursor::moveToFirst)
                if (!stillVerified) continue
                db.delete("diagnostics", "drive_id=?", arrayOf(candidate.driveId))
                db.delete("samples", "drive_id=?", arrayOf(candidate.driveId))
                val removed = db.delete(
                    "drives",
                    "drive_id=? AND status='complete' AND export_status='exported' AND bundle_sha256=?",
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
