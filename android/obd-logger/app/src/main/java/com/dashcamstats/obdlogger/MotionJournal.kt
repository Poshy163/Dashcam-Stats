package com.dashcamstats.obdlogger

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneId
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference

internal const val MOTION_MAX_GAP_MS = 10_000L
internal const val MOTION_RETENTION_MS = 7L * 24 * 60 * 60 * 1_000
internal const val MOTION_MAX_ROWS = 100_000
internal const val MOTION_BATCH_WINDOW_MS = 10_000L

/** No coordinates or raw adapter replies are ever accepted by this data model. */
internal data class MotionObservation(
    val wallMs: Long,
    val elapsedMs: Long,
    val sessionId: String,
    val segmentId: String,
    val kind: String,
    val acc: Boolean? = null,
    val speed: Double? = null,
    val accuracy: Double? = null,
    val ageMs: Long? = null,
    val horizontalAccuracy: Double? = null,
    val timezone: String = ZoneId.systemDefault().id,
)

internal class MotionClock(
    private val sessionId: String,
    private val wall: () -> Long = System::currentTimeMillis,
    private val elapsed: () -> Long = SystemClock::elapsedRealtime,
) {
    private var lastWall: Long? = null
    private var lastElapsed: Long? = null
    private var segment = UUID.randomUUID().toString()
    private var lastZone = ZoneId.systemDefault().id

    @Synchronized
    fun observation(kind: String, acc: Boolean? = null, speed: Double? = null,
                    accuracy: Double? = null, ageMs: Long? = null,
                    horizontalAccuracy: Double? = null): MotionObservation {
        val nowWall = wall()
        val nowElapsed = elapsed()
        val zone = ZoneId.systemDefault().id
        if (zone != lastZone || (lastWall != null && kotlin.math.abs(
                (nowWall - checkNotNull(lastWall)) - (nowElapsed - checkNotNull(lastElapsed)),
            ) > 2_000L)) segment = UUID.randomUUID().toString()
        lastZone = zone
        lastWall = nowWall
        lastElapsed = nowElapsed
        return MotionObservation(nowWall, nowElapsed, sessionId, segment, kind,
            acc, speed, accuracy, ageMs, horizontalAccuracy, zone)
    }
}

/** Separate bounded database: optional reporting cannot alter the immutable OBD bundle schema. */
internal class MotionJournal(context: Context, scope: CoroutineScope? = null) :
    SQLiteOpenHelper(context, "clip-movement.db", null, 1) {
    private val ownsScope = scope == null
    private val writerScope = scope ?: CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val pending = Channel<MotionObservation>(256)
    private val pendingGaps = mutableListOf<Pair<Long, Long>>()
    private val writer: Job
    val sourceId: String
    val sessionId = UUID.randomUUID().toString()
    val bootId: String = runCatching {
        UUID.fromString(File("/proc/sys/kernel/random/boot_id").readText().trim()).toString()
    }.getOrElse { UUID.randomUUID().toString() }
    private val clock = MotionClock(sessionId)
    private val recentAcc = AtomicReference<Pair<Boolean?, Long>?>(null)
    @Volatile var failed = false
        private set

    init {
        val prefs = context.getSharedPreferences("clip_movement", Context.MODE_PRIVATE)
        sourceId = prefs.getString("source_id", null) ?: UUID.randomUUID().toString().also {
            check(prefs.edit().putString("source_id", it).commit())
        }
        setWriteAheadLoggingEnabled(true)
        writer = writerScope.launch {
            while (isActive) {
                val first = pending.receiveCatching().getOrNull() ?: break
                val batch = mutableListOf(first)
                // One fsync per bounded batch, away from the live OBD poll coroutine.
                withTimeoutOrNull(MOTION_BATCH_WINDOW_MS) {
                    while (batch.size < 128) {
                        val next = pending.receiveCatching().getOrNull() ?: break
                        batch += next
                    }
                }
                runCatching { appendBatch(batch) }.onFailure {
                    markGap(batch.minOf { it.wallMs }, batch.maxOf { it.wallMs })
                }
            }
        }
    }

    override fun onConfigure(db: SQLiteDatabase) {
        db.rawQuery("PRAGMA busy_timeout=1000", null).close()
        db.execSQL("PRAGMA synchronous=FULL")
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE observations(id INTEGER PRIMARY KEY, wall_ms INTEGER NOT NULL," +
            "elapsed_ms INTEGER NOT NULL, session_id TEXT NOT NULL, segment_id TEXT NOT NULL," +
            "kind TEXT NOT NULL, acc INTEGER, speed REAL, accuracy REAL, age_ms INTEGER, hacc REAL, timezone TEXT NOT NULL)")
        db.execSQL("CREATE INDEX observations_time ON observations(wall_ms)")
        db.execSQL("CREATE TABLE coverage_gaps(id INTEGER PRIMARY KEY, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    fun acc(value: Boolean?) {
        recentAcc.set(value to SystemClock.elapsedRealtime())
        offer(clock.observation("acc", acc = value))
    }

    fun parkedNow(): Boolean = recentAcc.get()?.let {
        it.first == false && SystemClock.elapsedRealtime() - it.second in 0..7_500
    } == true

    fun speed(value: Double) {
        if (value.isFinite() && value in 0.0..255.0) offer(clock.observation("obd", speed = value))
    }

    fun gps(speed: Double?, accuracy: Double?, ageMs: Long?, horizontalAccuracy: Double?) {
        val usable = speed != null && speed.isFinite() && speed in 0.0..150.0 &&
            accuracy != null && accuracy.isFinite() && accuracy in 0.0..2.0 &&
            ageMs != null && ageMs in 0..5_000 && horizontalAccuracy != null &&
            horizontalAccuracy.isFinite() && horizontalAccuracy in 0.0..15.0
        offer(clock.observation("gps", speed = speed.takeIf { usable },
            accuracy = accuracy.takeIf { usable }, ageMs = ageMs.takeIf { usable },
            horizontalAccuracy = horizontalAccuracy.takeIf { usable }))
    }

    private fun offer(value: MotionObservation) {
        // A dropped observation creates an observable time gap; it never becomes stationary.
        if (pending.trySend(value).isFailure) markGap(value.wallMs, value.wallMs)
    }

    private fun markGap(start: Long, end: Long) {
        failed = true
        synchronized(pendingGaps) {
            pendingGaps += (start - MOTION_MAX_GAP_MS) to (end + MOTION_MAX_GAP_MS)
            // Merge rather than discard: exhausted bookkeeping broadens unknown coverage safely.
            while (pendingGaps.size > 64) {
                val a = pendingGaps.removeAt(0); val b = pendingGaps.removeAt(0)
                pendingGaps.add(0, minOf(a.first, b.first) to maxOf(a.second, b.second))
            }
        }
    }

    private fun appendBatch(values: List<MotionObservation>) {
        val db = writableDatabase
        val gaps = synchronized(pendingGaps) { pendingGaps.toList() }
        db.beginTransaction()
        try {
            for ((start, end) in gaps) db.execSQL("INSERT INTO coverage_gaps(start_ms,end_ms) VALUES(?,?)", arrayOf(start, end))
            for (value in values) {
                val row = ContentValues().apply {
                    put("wall_ms", value.wallMs); put("elapsed_ms", value.elapsedMs)
                    put("session_id", value.sessionId); put("segment_id", value.segmentId)
                    put("kind", value.kind)
                    if (value.acc == null) putNull("acc") else put("acc", if (value.acc) 1 else 0)
                    put("speed", value.speed); put("accuracy", value.accuracy)
                    put("age_ms", value.ageMs); put("hacc", value.horizontalAccuracy)
                    put("timezone", value.timezone)
                }
                db.insertOrThrow("observations", null, row)
            }
            db.delete("observations", "wall_ms < ?", arrayOf((System.currentTimeMillis() - MOTION_RETENTION_MS).toString()))
            db.delete("coverage_gaps", "end_ms < ?", arrayOf((System.currentTimeMillis() - MOTION_RETENTION_MS).toString()))
            var gapCount = db.rawQuery("SELECT COUNT(*) FROM coverage_gaps", null).use { it.moveToFirst(); it.getInt(0) }
            while (gapCount > 1_024) {
                val oldest = db.rawQuery("SELECT id,start_ms,end_ms FROM coverage_gaps ORDER BY start_ms LIMIT 2", null).use { cursor ->
                    buildList { while (cursor.moveToNext()) add(Triple(cursor.getLong(0), cursor.getLong(1), cursor.getLong(2))) }
                }
                db.delete("coverage_gaps", "id IN (?,?)", oldest.map { it.first.toString() }.toTypedArray())
                db.execSQL("INSERT INTO coverage_gaps(start_ms,end_ms) VALUES(?,?)", arrayOf(oldest.minOf { it.second }, oldest.maxOf { it.third }))
                gapCount -= 1
            }
            db.execSQL("DELETE FROM observations WHERE id IN (SELECT id FROM observations " +
                "ORDER BY id DESC LIMIT -1 OFFSET $MOTION_MAX_ROWS)")
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        synchronized(pendingGaps) { pendingGaps.removeAll(gaps.toSet()) }
    }

    fun between(start: Long, end: Long): List<MotionObservation> = readableDatabase.rawQuery(
        "SELECT wall_ms,elapsed_ms,session_id,segment_id,kind,acc,speed,accuracy,age_ms,hacc,timezone " +
            "FROM observations WHERE wall_ms BETWEEN ? AND ? ORDER BY wall_ms,id LIMIT 4096",
        arrayOf((start - MOTION_MAX_GAP_MS).toString(), (end + MOTION_MAX_GAP_MS).toString()),
    ).use { cursor -> buildList {
        while (cursor.moveToNext()) add(MotionObservation(cursor.getLong(0), cursor.getLong(1),
            cursor.getString(2), cursor.getString(3), cursor.getString(4),
            if (cursor.isNull(5)) null else cursor.getInt(5) != 0,
            if (cursor.isNull(6)) null else cursor.getDouble(6),
            if (cursor.isNull(7)) null else cursor.getDouble(7),
            if (cursor.isNull(8)) null else cursor.getLong(8),
            if (cursor.isNull(9)) null else cursor.getDouble(9), cursor.getString(10)))
    } }

    fun bounds(): Pair<String?, String?> = readableDatabase.rawQuery(
        "SELECT MIN(wall_ms),MAX(wall_ms) FROM observations", null,
    ).use { if (!it.moveToFirst() || it.isNull(0)) null to null else
        Instant.ofEpochMilli(it.getLong(0)).toString() to Instant.ofEpochMilli(it.getLong(1)).toString() }

    fun latestAccWallMs(): Long? = readableDatabase.rawQuery(
        "SELECT MAX(wall_ms) FROM observations WHERE kind='acc'", null,
    ).use { if (!it.moveToFirst() || it.isNull(0)) null else it.getLong(0) }

    fun evidenceHealthy(start: Long, end: Long): Boolean {
        if (synchronized(pendingGaps) { pendingGaps.any { it.first <= end && it.second >= start } }) return false
        return runCatching {
            readableDatabase.rawQuery("SELECT 1 FROM coverage_gaps WHERE start_ms <= ? AND end_ms >= ? LIMIT 1",
                arrayOf(end.toString(), start.toString())).use { !it.moveToFirst() }
        }.getOrDefault(false)
    }

    fun stop() {
        pending.close()
        if (ownsScope) writer.invokeOnCompletion { runCatching { close() }; writerScope.cancel() }
        // Drain accepted observations independently of service cancellation, with a hard wait bound.
        writerScope.launch {
            withTimeoutOrNull(2_000) { writer.join() }
            writer.cancel()
        }
    }
}

internal fun movementHash(value: String): String = MessageDigest.getInstance("SHA-256")
    .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
