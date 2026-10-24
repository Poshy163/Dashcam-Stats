package com.dashcamstats.obdlogger

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.provider.DocumentsContract
import android.os.ParcelFileDescriptor
import android.os.CancellationSignal
import android.database.Cursor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.math.max
import kotlin.math.min

internal const val MOVEMENT_REPORT_MAX_BYTES = 512 * 1024
internal const val MOVEMENT_REPORT_MAX_CLIPS = 512
internal const val MOVEMENT_TREE_PREFERENCES = "clip_movement"
internal const val MOVEMENT_TREE_URI = "dcim_tree_uri"
internal const val MOVEMENT_GPS_ENABLED = "passive_gps_enabled"

internal data class MotionAssessment(val classification: String, val reasons: List<String>,
    val coverage: JSONObject, val evidence: JSONObject)

/** Intervals require two actual observations. No interpolation across a restart or a clock jump. */
internal object MovementClassifier {
    fun assess(start: Long?, end: Long?, observations: List<MotionObservation>,
               locked: Boolean = false, orphan: Boolean = false, evidenceHealthy: Boolean = true): MotionAssessment {
        val reasons = mutableListOf<String>()
        if (locked) reasons += "locked_clip"
        if (orphan) reasons += "orphan_partial"
        val validBounds = start != null && end != null && end > start && end - start <= 86_400_000
        val lower = start ?: 0
        val upper = if (validBounds) checkNotNull(end) else lower
        val window = upper - lower
        val points = if (validBounds && !orphan && evidenceHealthy) observations else emptyList()
        val inside = points.filter { it.wallMs in lower..upper }
        val intervals = mutableListOf<Pair<Long, Long>>()
        var accMs = 0L; var speedMs = 0L; var gpsMs = 0L
        var onMs = 0L; var offMs = 0L; var zeroMs = 0L; var gpsMoving = 0
        var maxGap = 0L
        for ((kind, rows) in points.groupBy { it.kind }) {
            val ordered = rows.sortedBy { it.wallMs }
            for ((a, b) in ordered.zipWithNext()) {
                val lo = max(lower, a.wallMs); val hi = min(upper, b.wallMs)
                if (hi <= lo) continue
                val gap = b.elapsedMs - a.elapsedMs
                if (a.sessionId != b.sessionId || a.segmentId != b.segmentId ||
                    gap <= 0 || kotlin.math.abs(gap - (b.wallMs - a.wallMs)) > 2_000 ||
                    gap > if (kind == "acc") 7_500L else MOTION_MAX_GAP_MS) {
                    maxGap = max(maxGap, hi - lo)
                    continue
                }
                val usable = when (kind) {
                    "acc" -> a.acc != null && a.acc == b.acc
                    "obd", "gps" -> a.speed != null && b.speed != null
                    else -> false
                }
                if (!usable) { maxGap = max(maxGap, hi - lo); continue }
                intervals += lo to hi
                when (kind) {
                    "acc" -> { accMs += hi - lo; if (a.acc == true) onMs += hi - lo else offMs += hi - lo }
                    "obd" -> { speedMs += hi - lo; if (a.speed == 0.0 && b.speed == 0.0) zeroMs += hi - lo }
                    "gps" -> { gpsMs += hi - lo; if (a.wallMs in lower..upper && b.wallMs in lower..upper &&
                        confidentGps(a) && confidentGps(b)) gpsMoving += 1 }
                }
            }
        }
        // The union describes observed source coverage; it is not a claim of continuous vehicle motion.
        var through = lower; var union = 0L; var unionGap = 0L
        for ((lo, hi) in intervals.sortedBy { it.first }) {
            if (lo > through) unionGap = max(unionGap, lo - through)
            if (hi > through) { union += hi - max(through, lo); through = hi }
        }
        unionGap = max(unionGap, upper - through)
        maxGap = max(maxGap, unionGap).coerceIn(0, 86_400_000)
        val speed = inside.filter { it.kind == "obd" && it.speed != null }
        val gps = inside.filter { it.kind == "gps" && it.speed != null }
        val moving = speed.count { checkNotNull(it.speed) > 0.0 }
        val ambiguous = points.map { it.sessionId to it.segmentId }.distinct().size > 1
        val classification = when {
            !validBounds || orphan -> { reasons += "incomplete_time_bounds"; "unknown" }
            !evidenceHealthy -> { reasons += "observation_gap"; "unknown" }
            ambiguous -> { reasons += "clock_ambiguous"; "unknown" }
            moving > 0 -> { reasons += "validated_speed_positive"; "moving_observed" }
            gpsMoving > 0 -> { reasons += "gps_confident_movement"; "moving_observed" }
            window > 0 && zeroMs >= window * 0.9 -> { reasons += "zero_speed_samples"; "sampled_stationary" }
            window > 0 && offMs >= window * 0.9 -> { reasons += "acc_off_observed"; "acc_off_candidate" }
            else -> { reasons += if (inside.isEmpty()) "no_observation_coverage" else "observation_gap"; "unknown" }
        }
        reasons += "report_only"
        return MotionAssessment(classification, reasons,
            JSONObject().put("window_ms", window).put("acc_ms", accMs.coerceAtMost(window))
                .put("speed_ms", speedMs.coerceAtMost(window)).put("gps_ms", gpsMs.coerceAtMost(window))
                .put("unknown_ms", (window - union).coerceIn(0, window)).put("max_gap_ms", maxGap),
            JSONObject().put("acc_on_ms", onMs.coerceAtMost(window)).put("acc_off_ms", offMs.coerceAtMost(window))
                .put("valid_speed_samples", speed.size).put("moving_samples", moving)
                .put("max_speed_kmh", speed.mapNotNull { it.speed }.maxOrNull() ?: JSONObject.NULL)
                .put("gps_speed_samples", gps.size).put("gps_moving_samples", gpsMoving)
                .put("max_gps_speed_m_s", gps.mapNotNull { it.speed }.maxOrNull() ?: JSONObject.NULL))
    }

    internal fun confidentGps(value: MotionObservation): Boolean = value.kind == "gps" &&
        value.speed != null && value.accuracy != null && value.ageMs != null &&
        value.horizontalAccuracy != null && value.ageMs in 0..5_000 &&
        value.horizontalAccuracy in 0.0..15.0 && value.accuracy in 0.0..2.0 &&
        value.speed - 2 * value.accuracy > 1.5
}

internal data class MovementClip(val id: String, val relativePath: String, val name: String,
    val camera: String, val localStart: LocalDateTime, val size: Long, val mtime: Long,
    val locked: Boolean, val orphan: Boolean)

internal object MovementTreeAccess {
    fun selected(context: Context): Uri? = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES,
        Context.MODE_PRIVATE).getString(MOVEMENT_TREE_URI, null)?.let(Uri::parse)

    fun save(context: Context, uri: Uri, resultFlags: Int) {
        require(resultFlags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
        val id = DocumentsContract.getTreeDocumentId(uri)
        require(id.substringAfter(':', "").trimEnd('/').equals("DCIM", ignoreCase = true)) {
            "Select the recorder's DCIM folder"
        }
        // Take only read permission even when the system picker also offers write access.
        context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        val old = selected(context)
        check(context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE).edit()
            .putString(MOVEMENT_TREE_URI, uri.toString()).commit())
        if (old != null && old != uri) runCatching {
            context.contentResolver.releasePersistableUriPermission(old, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    fun revoke(context: Context) {
        selected(context)?.let { runCatching { context.contentResolver.releasePersistableUriPermission(
            it, Intent.FLAG_GRANT_READ_URI_PERMISSION) } }
        context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE).edit()
            .remove(MOVEMENT_TREE_URI).apply()
    }
}

/** Advisory metadata reporting plus a separate opt-in quiet-footage transport policy. */
internal class ClosedClipIndexer(private val context: Context, private val journal: MotionJournal,
                                 scope: CoroutineScope,
                                 canAnalyze: () -> Boolean = { false },
                                 private val reportRoot: () -> File? = { DeviceFiles.removableRootOrNull(context) }) {
    @Volatile var state = "directory_required"
        private set
    @Volatile var clipCount = 0
        private set
    private val wake = Channel<Unit>(Channel.CONFLATED)
    val transferPolicy = ClipTransferPolicy(context, journal, reportRoot, canAnalyze,
        analysisScope = scope, afterAnalysis = { wake.trySend(Unit) })
    private val seen = mutableMapOf<String, Pair<Long, Long>>()
    private val fingerprints = mutableMapOf<String, JSONObject>()
    private val fingerprintScan = mutableMapOf<String, Long>()
    private val unreadable = mutableSetOf<String>()
    private var scanNumber = 0L
    private var directoryTruncated = false
    private val queryCancellations = ConcurrentHashMap.newKeySet<CancellationSignal>()
    @Volatile private var stopping = false
    private val preferences = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
    private val preferenceListener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == QUIET_FILTER_ENABLED || key == MOVEMENT_TREE_URI) wake.trySend(Unit)
    }
    init { preferences.registerOnSharedPreferenceChangeListener(preferenceListener) }
    private val worker: Job = scope.launch {
        delay(5_000)
        while (isActive) {
            val priority = android.os.Process.getThreadPriority(android.os.Process.myTid())
            try {
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND)
                try { scan() } catch (error: Exception) {
                    if (error is CancellationException) throw error
                    state = "scan_failed"; runCatching { publish(emptyList(), null, true) }
                }
            } finally { android.os.Process.setThreadPriority(priority) }
            withTimeoutOrNull(if (transferPolicy.enabled) 5_000L else 60_000L) { wake.receive() }
        }
    }

    private fun children(tree: Uri, parentId: String): List<Array<Any>> {
        val uri = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parentId)
        return query(uri, arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED,
            DocumentsContract.Document.COLUMN_MIME_TYPE)) { cursor -> buildList {
            while (cursor.moveToNext() && size < 4096) add(arrayOf(cursor.getString(0),
                cursor.getString(1), cursor.getLong(2), cursor.getLong(3), cursor.getString(4)))
            if (size >= 4096) directoryTruncated = true
        } }
    }

    private fun <T> query(uri: Uri, columns: Array<String>, read: (Cursor) -> T): T {
        if (stopping) throw CancellationException("Clip indexer stopped")
        val cancellation = CancellationSignal()
        queryCancellations.add(cancellation)
        if (stopping) cancellation.cancel()
        try {
            return checkNotNull(context.contentResolver.query(uri, columns, null, null, null, cancellation))
                .use(read)
        } finally { queryCancellations.remove(cancellation) }
    }

    internal suspend fun scan() {
        val tree = MovementTreeAccess.selected(context)
        if (tree == null) { state = "directory_required"; publish(emptyList(), null, false); return }
        val treeId = DocumentsContract.getTreeDocumentId(tree)
        directoryTruncated = false
        val cardId = movementHash(treeId.substringBefore(':'))
        val entries = mutableListOf<MovementClip>()
        try {
            val root = children(tree, treeId)
            for (row in root) {
                val name = row[1] as String
                if (name in listOf("Video", "LockVideo") && row[4] == DocumentsContract.Document.MIME_TYPE_DIR) {
                    for (child in children(tree, row[0] as String)) parseClip(child, "DCIM/$name/", name == "LockVideo")?.let(entries::add)
                } else parseClip(row, "DCIM/", false)?.takeIf { it.orphan }?.let(entries::add)
            }
        } catch (_: SecurityException) {
            state = "directory_unavailable"; publish(emptyList(), cardId, false); return
        }
        val sorted = entries.sortedByDescending { it.localStart }.take(MOVEMENT_REPORT_MAX_CLIPS)
        val zone = ZoneId.systemDefault()
        val output = mutableListOf<JSONObject>()
        val policyClips = mutableListOf<PolicyClip>()
        scanNumber += 1
        data class Inspection(val clip: MovementClip, val stable: Boolean, val start: Long?, val end: Long?,
                              val bounded: Boolean, val timeSafe: Boolean, val assessment: MotionAssessment,
                              val fingerprintEligible: Boolean)
        val inspectedAt = System.currentTimeMillis()
        val inspections = sorted.map { clip ->
            currentCoroutineContext().ensureActive()
            val key = "$cardId/${clip.relativePath}"
            val stable = seen[key] == (clip.size to clip.mtime)
            val next = entries.asSequence().filter { it.camera == clip.camera && it.localStart > clip.localStart }
                .minByOrNull { it.localStart }
            val offsets = zone.rules.getValidOffsets(clip.localStart)
            val nextOffsets = next?.let { zone.rules.getValidOffsets(it.localStart) }.orEmpty()
            val start = if (offsets.size == 1) clip.localStart.toInstant(offsets.single()).toEpochMilli() else null
            val end = if (next != null && nextOffsets.size == 1) next.localStart.toInstant(nextOffsets.single()).toEpochMilli() else null
            val bounded = stable && !clip.orphan && start != null && end != null && end - start in 1..120_000
            val observations = if (bounded) journal.between(checkNotNull(start), checkNotNull(end)) else emptyList()
            val timeSafe = bounded && observations.all { it.timezone == zone.id }
            val evidenceHealthy = bounded && journal.evidenceHealthy(checkNotNull(start), checkNotNull(end))
            val assessment = MovementClassifier.assess(start.takeIf { timeSafe }, end.takeIf { timeSafe },
                observations, clip.locked, clip.orphan, evidenceHealthy)
            Inspection(clip, stable, start, end, bounded, timeSafe, assessment,
                stable && transferPolicy.fingerprintCandidate(clip, start, end, assessment.classification, inspectedAt))
        }
        // A full card used to spend nearly the whole six-minute opportunity refreshing
        // locked, moving and old files. Keep fair refreshes, with guarded parked clips first.
        val fingerprintCandidates = inspections.filter { it.stable }
            .sortedWith(compareBy<Inspection> { when {
                !it.fingerprintEligible -> 2
                inspectedAt < checkNotNull(it.end) + QUIET_TRANSFER_DEFER_MS -> 0
                else -> 1
            } }
                .thenBy { fingerprintScan["$cardId/${it.clip.relativePath}/${it.clip.size}/${it.clip.mtime}"] ?: 0L })
            .take(8).map { "$cardId/${it.clip.relativePath}/${it.clip.size}/${it.clip.mtime}" }.toSet()
        for (inspection in inspections) {
            currentCoroutineContext().ensureActive()
            val (clip, stable, start, end, bounded, timeSafe, assessment) = inspection
            val key = "$cardId/${clip.relativePath}"
            val reasons = assessment.reasons.toMutableList()
            if (!stable) reasons += "file_unstable"
            if (bounded && !timeSafe) reasons += "clock_ambiguous"
            val fingerprintKey = "$key/${clip.size}/${clip.mtime}"
            var fingerprint = fingerprints[fingerprintKey]
            if (stable && fingerprintKey in fingerprintCandidates) {
                fingerprint = runCatching { fingerprint(tree, clip) }.getOrNull()
                fingerprintScan[fingerprintKey] = scanNumber
                if (fingerprint != null) { fingerprints[fingerprintKey] = fingerprint; unreadable.remove(fingerprintKey) }
                else { fingerprints.remove(fingerprintKey); unreadable.add(fingerprintKey) }
            }
            if (fingerprintKey in unreadable) reasons += "file_unreadable"
            val body = JSONObject().put("clip_id", UUID.nameUUIDFromBytes(key.plus("/${clip.size}/${clip.mtime}/${fingerprint?.optString("digest").orEmpty()}").toByteArray()).toString())
                .put("relative_path", clip.relativePath).put("camera_key", clip.camera).put("locked", clip.locked)
                .put("size_bytes", clip.size).put("mtime_ms", clip.mtime).put("fingerprint", fingerprint ?: JSONObject.NULL)
                .put("local_start", clip.localStart.format(DateTimeFormatter.ISO_LOCAL_DATE_TIME))
                .put("timezone", zone.id).put("start_utc", if (timeSafe) Instant.ofEpochMilli(checkNotNull(start)).toString() else JSONObject.NULL)
                .put("end_utc", if (timeSafe) Instant.ofEpochMilli(checkNotNull(end)).toString() else JSONObject.NULL)
                // Adjacent filenames bound an analysis window; they do not establish media duration.
                .put("duration_ms", JSONObject.NULL).put("time_quality", if (timeSafe) "bounded" else "unknown")
                .put("classification", if ("file_unreadable" in reasons) "unknown" else assessment.classification).put("reasons", JSONArray(reasons.distinct()))
                .put("analysis_status", if (assessment.classification == "unknown" || "file_unreadable" in reasons) "unknown" else "complete")
                .put("coverage", assessment.coverage).put("evidence", assessment.evidence)
            output += body
            policyClips += PolicyClip(clip, body, stable,
                fingerprint != null && fingerprintScan[fingerprintKey] == scanNumber && fingerprintKey !in unreadable,
                DocumentsContract.buildDocumentUriUsingTree(tree, clip.id),
                start, end)
        }
        seen.clear(); entries.forEach { seen["$cardId/${it.relativePath}"] = it.size to it.mtime }
        val present = entries.map { "$cardId/${it.relativePath}/${it.size}/${it.mtime}" }.toSet()
        fingerprints.keys.retainAll(present); fingerprintScan.keys.retainAll(present); unreadable.retainAll(present)
        state = "ready"; clipCount = output.size
        publish(output, cardId, directoryTruncated || entries.size > output.size, policyClips)
    }

    private fun unchanged(tree: Uri, clip: MovementClip): Boolean {
        val uri = DocumentsContract.buildDocumentUriUsingTree(tree, clip.id)
        return query(uri, arrayOf(
            DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED)) {
            it.moveToFirst() && it.getLong(0) == clip.size && it.getLong(1) == clip.mtime }
    }

    private fun fingerprint(tree: Uri, clip: MovementClip): JSONObject {
        val uri = DocumentsContract.buildDocumentUriUsingTree(tree, clip.id)
        check(unchanged(tree, clip))
        val digest = MessageDigest.getInstance("SHA-256")
        digest.update("sha256-sampled-v1\u0000${clip.size}\u0000".toByteArray(Charsets.UTF_8))
        val descriptor = checkNotNull(context.contentResolver.openFileDescriptor(uri, "r"))
        ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { input ->
            val channel = input.channel
            for (offset in listOf(0L, ((clip.size - 16_384) / 2).coerceAtLeast(0), (clip.size - 16_384).coerceAtLeast(0)).distinct()) {
                channel.position(offset)
                val buffer = ByteBuffer.allocate(min(16_384L, clip.size - offset).toInt())
                while (buffer.hasRemaining()) check(channel.read(buffer) > 0)
                digest.update(buffer.array())
            }
        }
        check(unchanged(tree, clip))
        return JSONObject().put("algorithm", "sha256-sampled-v1").put("sample_bytes", 16_384)
            .put("digest", digest.digest().joinToString("") { "%02x".format(it) })
    }

    private fun parseClip(row: Array<Any>, prefix: String, locked: Boolean): MovementClip? {
        val name = row[1] as String
        val match = Regex("^(pre_)?(\\d{14})_(camera_[01])\\.ts$").matchEntire(name) ?: return null
        val size = row[2] as Long; val mtime = row[3] as Long
        if (size <= 0 || size > 1L.shl(40) || mtime !in 0..4_102_444_800_000L) return null
        val start = runCatching { LocalDateTime.parse(match.groupValues[2],
            DateTimeFormatter.ofPattern("uuuuMMddHHmmss").withResolverStyle(ResolverStyle.STRICT)) }.getOrNull() ?: return null
        return MovementClip(row[0] as String, prefix + name, name, match.groupValues[3], start,
            size, mtime, locked, match.groupValues[1].isNotEmpty())
    }

    private suspend fun publish(clips: List<JSONObject>, cardId: String?, truncated: Boolean,
                                policyClips: List<PolicyClip> = emptyList()) {
        val root = reportRoot()
        if (root == null) { state = "storage_unavailable"; clipCount = 0; return }
        val bounds = runCatching { journal.bounds() }.getOrDefault(null to null)
        val report = JSONObject().put("schema_version", 1).put("source_id", journal.sourceId)
            .put("card_id", cardId ?: JSONObject.NULL).put("session_id", journal.sessionId).put("boot_id", journal.bootId)
            .put("generated_at_utc", Instant.now().toString()).put("producer_version", BuildConfig.VERSION_NAME)
            .put("classifier_version", 1).put("report_only", true).put("truncated", truncated)
            .put("coverage_start_utc", bounds.first ?: JSONObject.NULL).put("coverage_end_utc", bounds.second ?: JSONObject.NULL)
            .put("status", state).put("clips", JSONArray(clips))
        while (report.toString().toByteArray().size > MOVEMENT_REPORT_MAX_BYTES && report.getJSONArray("clips").length() > 0) {
            val array = report.getJSONArray("clips"); array.remove(array.length() - 1); report.put("truncated", true)
        }
        clipCount = report.getJSONArray("clips").length()
        val directory = File(root, "movement").apply { mkdirs() }
        val partial = File(directory, "report.json.partial")
        FileOutputStream(partial).use { it.write(report.toString().toByteArray(Charsets.UTF_8)); it.fd.sync() }
        Files.move(partial.toPath(), File(directory, "report.json").toPath(),
            StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        val tree = MovementTreeAccess.selected(context)
        transferPolicy.publish(policyClips, cardId, state, report.getBoolean("truncated")) { clip ->
            tree != null && runCatching { unchanged(tree, clip) }.getOrDefault(false)
        }
    }

    fun stop() {
        stopping = true
        preferences.unregisterOnSharedPreferenceChangeListener(preferenceListener)
        worker.cancel(); transferPolicy.stop(); queryCancellations.forEach { it.cancel() }; wake.close()
    }
}
