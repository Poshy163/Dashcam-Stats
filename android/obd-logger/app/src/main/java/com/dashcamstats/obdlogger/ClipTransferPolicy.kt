package com.dashcamstats.obdlogger

import android.content.Context
import android.net.Uri
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.ExecutorCoroutineDispatcher
import kotlinx.coroutines.Job
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.launch
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

internal const val QUIET_FILTER_ENABLED = "parked_quiet_filter_enabled"
private const val GUARD_PADDING_MS = 122_000L // 120s ignition margin plus filename timestamp uncertainty.
internal const val QUIET_POLICY_VERSION = "parked_visual_quiet_v1"

internal data class PolicyClip(val clip: MovementClip, val body: JSONObject, val stable: Boolean,
    val freshFingerprint: Boolean, val uri: Uri, val startMs: Long?, val boundEndMs: Long?)

internal object ParkedGuard {
    fun assess(start: Long?, end: Long?, values: List<MotionObservation>, healthy: Boolean,
               timezone: String): JSONObject {
        val valid = start != null && end != null && end > start && end - start <= 86_400_000
        val lower = start ?: 0; val upper = if (valid) checkNotNull(end) else lower
        val window = upper - lower
        val relevant = values.filter { it.wallMs in lower..upper }
        val acc = values.filter { it.kind == "acc" }.sortedBy { it.wallMs }
        var off = 0L; var through = lower; var maxGap = 0L
        val segments = mutableSetOf<Pair<String, String>>()
        for ((a, b) in acc.zipWithNext()) {
            val lo = max(lower, a.wallMs); val hi = min(upper, b.wallMs)
            if (hi <= lo) continue
            segments += a.sessionId to a.segmentId; segments += b.sessionId to b.segmentId
            val elapsed = b.elapsedMs - a.elapsedMs
            val safe = healthy && a.acc == false && b.acc == false &&
                a.sessionId == b.sessionId && a.segmentId == b.segmentId &&
                a.timezone == timezone && b.timezone == timezone && elapsed in 1..7_500 &&
                abs(elapsed - (b.wallMs - a.wallMs)) <= 2_000
            if (lo > through) maxGap = max(maxGap, lo - through)
            if (safe) { off += (hi - max(through, lo)).coerceAtLeast(0); through = max(through, hi) }
            else maxGap = max(maxGap, hi - lo)
            maxGap = max(maxGap, elapsed.coerceAtLeast(0))
        }
        maxGap = max(maxGap, upper - through).coerceIn(0, 86_400_000)
        relevant.forEach { segments += it.sessionId to it.segmentId }
        val positive = relevant.count { it.kind == "obd" && (it.speed ?: 0.0) > 0 } +
            relevant.count { MovementClassifier.confidentGps(it) }
        val unknown = relevant.count { (it.kind == "acc" && it.acc == null) || it.timezone != timezone }
        return JSONObject().put("start_utc", if (valid) Instant.ofEpochMilli(lower).toString() else JSONObject.NULL)
            .put("end_utc", if (valid) Instant.ofEpochMilli(upper).toString() else JSONObject.NULL)
            .put("window_ms", window).put("acc_off_ms", off.coerceAtMost(window))
            .put("unknown_ms", (window - off).coerceIn(0, window)).put("max_gap_ms", maxGap)
            .put("observation_count", relevant.count { it.kind == "acc" })
            .put("on_samples", relevant.count { it.kind == "acc" && it.acc == true })
            .put("unknown_samples", unknown).put("segment_count", segments.size)
            .put("positive_speed_samples", positive)
    }

    fun valid(proof: JSONObject): Boolean = proof.optLong("window_ms") > 0 &&
        proof.optLong("acc_off_ms") == proof.optLong("window_ms") && proof.optLong("unknown_ms") == 0L &&
        proof.optLong("max_gap_ms") <= 7_500 && proof.optInt("observation_count") >= 2 &&
        proof.optInt("on_samples") == 0 && proof.optInt("unknown_samples") == 0 &&
        proof.optInt("segment_count") == 1 && proof.optInt("positive_speed_samples") == 0
}

/** Exclusions are a separate, opt-in transport policy. No file is modified, deleted or marked backed up.
 * A persisted visual result is reusable only after the exact live metadata and sampled fingerprint
 * are revalidated. A missing decision or uncertain peer always means upload. */
internal class ClipTransferPolicy(private val context: Context, private val journal: MotionJournal,
    private val root: () -> File?, private val allowed: () -> Boolean,
    private val analyzer: VisualQuietAnalyzer = VisualQuietAnalyzer(context),
    private val wallMs: () -> Long = System::currentTimeMillis,
    private val analysisScope: CoroutineScope? = null,
    private val afterAnalysis: () -> Unit = {},
    private val analysisDispatcher: CoroutineDispatcher? = null) {
    @Volatile var state = "disabled"; private set
    @Volatile var skipCount = 0; private set
    @Volatile var uploadCount = 0; private set
    val enabled: Boolean get() = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        .getBoolean(QUIET_FILTER_ENABLED, false)
    private val cacheFile = File(context.filesDir, "quiet-analysis-v2.json")
    private val cached = linkedMapOf<String, JSONObject>()
    private val cacheLock = Any()
    private val attempted = mutableMapOf<String, Long>()
    private var analysisJob: Job? = null
    private val ownedDecoderDispatcher = lazy { quietDecoderDispatcher() }
    @Volatile private var stopped = false
    private var sequence = 0L

    init {
        runCatching {
            if (cacheFile.length() in 1..MOVEMENT_REPORT_MAX_BYTES.toLong()) {
                val rows = JSONObject(cacheFile.readText()).getJSONArray("entries")
                for (i in 0 until min(rows.length(), 128)) {
                    val row = rows.getJSONObject(i)
                    if (row.getString("key").matches(Regex("[a-f0-9]{64}")))
                        cached[row.getString("key")] = row.getJSONObject("visual")
                }
            }
        }.onFailure { cached.clear() }
    }

    private fun key(item: PolicyClip, card: String): String = movementHash("$QUIET_POLICY_VERSION/$QUIET_ALGORITHM_VERSION/$card/" +
        "${item.clip.relativePath}/${item.clip.size}/${item.clip.mtime}/" +
        item.body.optJSONObject("fingerprint")?.optString("digest").orEmpty())

    @Volatile var deferCount = 0; private set

    private fun guard(item: PolicyClip, end: Long?, observedThrough: Long?): JSONObject {
        val lower = item.startMs?.minus(GUARD_PADDING_MS)
        val upper = end?.plus(GUARD_PADDING_MS)?.let { if (observedThrough == null) it else min(it, observedThrough) }
        return ParkedGuard.assess(lower, upper,
            if (lower != null && upper != null) journal.between(lower, upper) else emptyList(),
            lower != null && upper != null && journal.evidenceHealthy(lower, upper), ZoneId.systemDefault().id)
    }

    private fun evaluate(items: List<PolicyClip>, card: String?, now: Long): MutableList<JSONObject> {
        val active = enabled
        val observedThrough = journal.latestAccWallMs()?.coerceAtMost(now)
        return items.map { item ->
            val clip = item.clip
            val body = JSONObject(item.body.toString())
            val visual = card?.let { synchronized(cacheLock) { cached[key(item, it)] } } ?: emptyVisual()
            val completeVisual = validVisual(visual)
            val measured = visual.optLong("duration_ms")
            val end = if (completeVisual && item.startMs != null) item.startMs + measured else item.boundEndMs
            val knownTime = item.startMs != null && end != null && end - item.startMs in 30_000..120_000 &&
                item.boundEndMs != null && abs(end - item.boundEndMs) <= 2_000
            val assessment = if (knownTime) MovementClassifier.assess(item.startMs, end,
                journal.between(checkNotNull(item.startMs), checkNotNull(end)),
                evidenceHealthy = journal.evidenceHealthy(item.startMs, end)) else null
            if (knownTime && assessment != null) body
                .put("start_utc", Instant.ofEpochMilli(checkNotNull(item.startMs)).toString())
                .put("end_utc", Instant.ofEpochMilli(checkNotNull(end)).toString())
                .put("time_quality", "bounded").put("duration_ms", if (completeVisual) measured else JSONObject.NULL)
                .put("classification", assessment.classification).put("reasons", JSONArray(assessment.reasons))
                .put("coverage", assessment.coverage).put("evidence", assessment.evidence)
                .put("analysis_status", if (assessment.classification == "unknown") "unknown" else "complete")
            val past = if (active) guard(item, end, observedThrough)
                else ParkedGuard.assess(null, null, emptyList(), false, ZoneId.systemDefault().id)
            val full = if (active) guard(item, end, null) else past
            val deadline = end?.plus(360_000)
            val readable = !item.body.getJSONArray("reasons").toString().contains("file_unreadable")
            var decision = "upload"
            val reason = when {
                !active -> "policy_disabled"
                clip.locked -> "locked_clip"
                clip.orphan -> "orphan_partial"
                !clip.relativePath.startsWith("DCIM/Video/") || card == null -> "card_or_identity_unknown"
                !knownTime -> "timing_unknown"
                assessment?.classification == "unknown" -> "observation_gap"
                past.optInt("positive_speed_samples") > 0 -> "positive_movement"
                past.optInt("on_samples") > 0 -> "ignition_buffer"
                !ParkedGuard.valid(past) || past.isNull("end_utc") ||
                    Instant.parse(past.getString("end_utc")).toEpochMilli() < checkNotNull(end) -> "acc_not_fully_off"
                !readable -> "unstable_or_unreadable"
                visual.optString("status") == "activity" -> "visual_activity"
                visual.optString("status") == "error" -> "analysis_error"
                visual.optString("status") !in setOf("quiet", "not_analyzed") -> "visual_incomplete"
                visual.optString("status") == "quiet" && !completeVisual -> "visual_incomplete"
                !item.stable -> if (deadline != null && now < deadline) { decision = "defer"; "file_settling" } else "unstable_or_unreadable"
                completeVisual && !body.isNull("fingerprint") && ParkedGuard.valid(full) -> { decision = "skip"; "parked_visual_quiet" }
                deadline == null || now >= deadline -> "visual_required"
                !ParkedGuard.valid(full) -> { decision = "defer"; "awaiting_post_guard" }
                else -> { decision = "defer"; "analysis_pending" }
            }
            JSONObject().put("clip", body).put("decision", decision).put("reason", reason)
                .put("guard", if (decision == "skip") full else past).put("visual", visual)
                .put("defer_until_utc", if (decision == "defer") Instant.ofEpochMilli(checkNotNull(deadline)).toString() else JSONObject.NULL)
        }.toMutableList()
    }

    suspend fun publish(items: List<PolicyClip>, card: String?, reportState: String, truncated: Boolean,
                        unchanged: (MovementClip) -> Boolean) {
        sequence++
        val present = if (card == null) emptySet() else items.map { key(it, card) }.toSet()
        synchronized(cacheLock) { if (cached.keys.retainAll(present)) saveCache() }
        attempted.keys.retainAll(present)
        state = if (enabled) "waiting" else "disabled"
        var rows = evaluate(items, card, wallMs())
        fun recheckQuietIdentities() {
            for (row in rows) if (row.optString("decision") == "skip") {
                val item = items.singleOrNull { it.clip.relativePath == row.getJSONObject("clip").optString("relative_path") }
                if (item == null || !unchanged(item.clip)) row.put("decision", "upload")
                    .put("reason", "unstable_or_unreadable").put("defer_until_utc", JSONObject.NULL)
            }
        }
        recheckQuietIdentities()
        // Expose bounded pending decisions before a slow decoder starts. This closes the normal
        // backup race without holding arbitrary unknown footage or resetting its deadline.
        writeReport(rows, card, reportState, truncated)
        // Scheduling uses this clip's own evidence, before transfer pairing. Staggered
        // camera segments may need two opposite-view witnesses; a missing witness must
        // retain transfer without preventing the independent analysis from ever running.
        val candidate = if (stopped || !enabled || card == null || !allowed() || analysisJob?.isActive == true) null else items
            .filter { it.stable && it.freshFingerprint && it.clip.size <= 256L * 1024 * 1024 &&
                synchronized(cacheLock) { cached[key(it, card)] == null } && rows.any { row ->
                    row.optString("decision") == "defer" &&
                        row.getJSONObject("clip").optString("relative_path") == it.clip.relativePath } }
            .minByOrNull { attempted[key(it, card)] ?: 0L }
        if (candidate != null && card != null) {
            currentCoroutineContext().ensureActive()
            state = "scanning"
            val identity = key(candidate, card)
            attempted[identity] = sequence
            suspend fun analyzeCandidate() {
                val deadline = checkNotNull(candidate.boundEndMs) + 360_000
                var visual = try { analyzer.analyze(candidate.uri) {
                    !stopped && enabled && allowed() && wallMs() < deadline
                } } catch (error: Exception) {
                    if (error is CancellationException) throw error
                    emptyVisual("error")
                }
                if (!unchanged(candidate.clip)) visual = emptyVisual("incomplete")
                // Failure must upload rather than enter another defer after a process restart.
                synchronized(cacheLock) {
                    cached.remove(identity); cached[identity] = JSONObject(visual.toString())
                    while (cached.size > 128) cached.remove(cached.keys.first())
                    saveCache()
                }
            }
            if (analysisScope == null) {
                // Deterministic publisher harness uses the identical analysis path inline.
                analyzeCandidate()
                rows = evaluate(items, card, wallMs())
            } else analysisJob = analysisScope.launch(analysisDispatcher ?: ownedDecoderDispatcher.value) {
                fun failed() {
                    // Cache persistence and metadata checks are optional too. A result that could
                    // not be persisted must not remain quiet in memory or escape into the logger's
                    // uncaught-coroutine handler. Disk recovery never extends its defer deadline.
                    synchronized(cacheLock) {
                        cached.remove(identity); cached[identity] = emptyVisual("error")
                        while (cached.size > 128) cached.remove(cached.keys.first())
                    }
                    state = "unavailable"
                }
                try {
                    // Priority belongs to the dedicated thread, not a migrating IO coroutine.
                    // If the platform refused the priority change, preserve upload instead.
                    if (analysisDispatcher == null) check(android.os.Process.getThreadPriority(
                        android.os.Process.myTid()) >= android.os.Process.THREAD_PRIORITY_BACKGROUND)
                    analyzeCandidate()
                } catch (error: Exception) {
                    if (error is CancellationException) throw error
                    failed()
                } finally {
                    try { afterAnalysis() } catch (error: Exception) {
                        if (error is CancellationException) throw error
                        failed()
                    }
                }
            }
        }
        // Witnesses need the same current identity check as exclusion candidates.
        recheckQuietIdentities()
        writeReport(rows, card, reportState, truncated)
    }

    private fun writeReport(baseRows: List<JSONObject>, card: String?, reportState: String, truncated: Boolean) {
        val active = enabled
        // Keep own qualification immutable. Repeated publication/truncation must not
        // recursively turn a quiet boundary witness into missing evidence for its peer.
        val retained = baseRows.toMutableList()
        fun gatedRows(): MutableList<JSONObject> = retained.map { JSONObject(it.toString()) }.toMutableList().also { rows ->
            if (!active) rows.forEach { it.put("decision", "upload").put("reason", "policy_disabled")
                .put("defer_until_utc", JSONObject.NULL) }
            applyPairGate(rows, wallMs())
        }
        var rows = gatedRows()
        state = if (!active) "disabled" else if (reportState == "ready") "ready" else "unavailable"
        val output = JSONObject().put("schema_version", 1).put("source_id", journal.sourceId)
            .put("card_id", card ?: JSONObject.NULL).put("session_id", journal.sessionId).put("boot_id", journal.bootId)
            .put("generated_at_utc", Instant.ofEpochMilli(wallMs()).toString()).put("producer_version", BuildConfig.VERSION_NAME)
            .put("policy_version", QUIET_POLICY_VERSION).put("enabled", active).put("mode", "acc_off_visual_quiet")
            .put("status", reportState).put("truncated", truncated).put("decisions", JSONArray(rows))
        while (output.toString().toByteArray(Charsets.UTF_8).size > MOVEMENT_REPORT_MAX_BYTES && rows.isNotEmpty()) {
            // Trim a whole oversized tail using the already gated projection. Re-running
            // interval coverage for each removed row is needlessly expensive on a full card.
            output.put("truncated", true)
            var projectedBytes = output.toString().toByteArray(Charsets.UTF_8).size
            while (projectedBytes > MOVEMENT_REPORT_MAX_BYTES && rows.isNotEmpty()) {
                retained.removeAt(retained.lastIndex)
                val removed = rows.removeAt(rows.lastIndex)
                projectedBytes -= removed.toString().toByteArray(Charsets.UTF_8).size + if (rows.isEmpty()) 0 else 1
            }
            // A removed witness must not leave a surviving candidate authorized. Derive
            // again from the immutable retained prefix, then recheck the actual byte size.
            rows = gatedRows()
            output.put("truncated", true).put("decisions", JSONArray(rows))
        }
        skipCount = rows.count { it.optString("decision") == "skip" }
        deferCount = rows.count { it.optString("decision") == "defer" }
        uploadCount = rows.size - skipCount - deferCount
        root()?.let { atomicJson(File(it, "movement/transfer-policy.json"), output) }
    }

    private fun saveCache() = synchronized(cacheLock) { atomicJson(cacheFile,
        JSONObject().put("entries", JSONArray(cached.map { (key, visual) ->
            JSONObject().put("key", key).put("visual", visual) }))) }

    fun stop() {
        stopped = true
        val job = analysisJob
        job?.cancel()
        if (ownedDecoderDispatcher.isInitialized()) {
            if (job == null) ownedDecoderDispatcher.value.close()
            else job.invokeOnCompletion { ownedDecoderDispatcher.value.close() }
        }
    }

    companion object {
        fun emptyVisual(status: String = "not_analyzed"): JSONObject = QuietFrameTracker().proof(0, false, false, status)
            .put("analysis_completed_at_utc", JSONObject.NULL)

        fun validVisual(proof: JSONObject): Boolean {
            val first = proof.optLong("first_pts_us", -1); val last = proof.optLong("last_pts_us", -1)
            val duration = proof.optLong("duration_ms")
            return proof.optInt("algorithm_version") == QUIET_ALGORITHM_VERSION && proof.optString("status") == "quiet" &&
                proof.optBoolean("input_eos") && proof.optBoolean("decoder_eos") && first >= 0 && last > first &&
                proof.optInt("input_frames") == proof.optInt("decoded_frames") &&
                proof.optInt("sampled_frames") == proof.optInt("decoded_frames") && proof.optInt("decoded_frames") >= 20 &&
                duration in 30_000..120_000 && abs((last - first) / 1_000 - duration) <= 150 &&
                proof.optLong("covered_ms") >= duration - 150 && proof.optLong("max_gap_ms") <= 150 &&
                proof.optInt("motion_frames") == 0 && proof.optInt("max_coherent_pixels") < 16 &&
                proof.optInt("max_rolling_coherent_pixels") < 128 && proof.optInt("max_consecutive_coherent_frames") < 2 &&
                proof.optString("motion_metric") == QUIET_MOTION_METRIC && proof.optInt("rolling_frames") == 6 &&
                proof.optString("roi_profile") == QUIET_ROI_PROFILE && proof.optInt("roi_rows") == QUIET_ROI_ROWS &&
                proof.optDouble("min_mean_luma", -1.0) >= 40 && proof.optDouble("max_mean_luma", 256.0) <= 215 &&
                proof.optDouble("max_dark_fraction", 1.0) <= .55 && proof.optDouble("max_bright_fraction", 1.0) <= .4 &&
                proof.optInt("min_contrast_span") >= 24 &&
                proof.optInt("grid_width") == QUIET_GRID_WIDTH && proof.optInt("grid_height") == QUIET_GRID_HEIGHT
        }

        fun applyPairGate(rows: List<JSONObject>, now: Long = System.currentTimeMillis()) {
            fun bounds(row: JSONObject): Pair<Long, Long>? = runCatching {
                val clip = row.getJSONObject("clip")
                val start = Instant.parse(clip.getString("start_utc")).toEpochMilli()
                val end = Instant.parse(clip.getString("end_utc")).toEpochMilli()
                (start to end).takeIf { end > start }
            }.getOrNull()
            fun own(row: JSONObject): String = when {
                row.optString("decision") == "upload" && row.optString("reason") == "peer_coverage_required" -> "skip"
                row.optString("decision") == "upload" && row.optString("reason") == "peer_analysis_pending" -> "defer"
                row.optString("decision") == "defer" && row.optString("reason") == "analysis_pending" &&
                    row.optJSONObject("visual")?.let(::validVisual) == true &&
                    row.optJSONObject("guard")?.let(ParkedGuard::valid) == true -> "skip"
                else -> row.optString("decision")
            }
            // Snapshot before mutating dispositions: a retained boundary clip may still
            // independently prove its own view, without recursively needing its peers.
            val originals = rows.map { JSONObject(it.toString()) }
            for ((index, original) in originals.withIndex()) {
                val qualification = own(original)
                if (qualification !in setOf("skip", "defer")) continue
                val row = rows[index]
                val clip = original.getJSONObject("clip")
                val interval = bounds(original)
                fun retain() { row.put("decision", "upload")
                    .put("reason", if (qualification == "skip") "peer_coverage_required" else "peer_analysis_pending")
                    .put("defer_until_utc", JSONObject.NULL) }
                if (interval == null) { retain(); continue }
                val (start, end) = interval
                val allOthers = originals.filterIndexed { otherIndex, _ -> otherIndex != index }
                val others = allOthers.filter { it.getJSONObject("clip").optString("camera_key") != clip.optString("camera_key") }
                val localStart = runCatching { LocalDateTime.parse(clip.getString("local_start")) }.getOrNull()
                val localEnd = localStart?.plusNanos((end - start) * 1_000_000)
                // A timestamped opposite-view file with unknown bounds could overlap
                // for up to the maximum supported clip duration. Never infer it quiet.
                val unknown = allOthers.any { other -> bounds(other) == null && runCatching {
                    val peerStart = LocalDateTime.parse(other.getJSONObject("clip").getString("local_start"))
                    localStart != null && localEnd != null && !peerStart.isBefore(localStart.minusSeconds(120)) && peerStart.isBefore(localEnd)
                }.getOrDefault(true) }
                // An ambiguous duplicate on this same view is unsafe too. Qualified
                // adjacent segments may overlap a few decoded frames; unsafe ones may not.
                val unsafeSameView = allOthers.any { other -> other.getJSONObject("clip").optString("camera_key") == clip.optString("camera_key") &&
                    bounds(other)?.let { (lo, hi) -> lo < end && hi > start } == true && own(other) !in setOf("skip", "defer") }
                val peers = others.filter { bounds(it)?.let { (lo, hi) -> lo < end && hi > start } == true }
                val unique = peers.map { peer ->
                    val body = peer.getJSONObject("clip")
                    Triple(body.optString("camera_key"), body.optString("start_utc"), body.optString("end_utc"))
                }.toSet().size == peers.size && peers.map { it.getJSONObject("clip").optString("relative_path") }.toSet().size == peers.size
                var through = start
                var continuous = true
                for ((lo, hi) in peers.mapNotNull(::bounds).sortedBy { it.first }) {
                    if (lo > through) { continuous = false; break }
                    through = max(through, hi)
                }
                if (unknown || unsafeSameView || !unique || peers.isEmpty() || !continuous || through < end || peers.any { own(it) !in setOf("skip", "defer") }) {
                    retain(); continue
                }
                if (qualification == "defer" || peers.any { own(it) == "defer" }) {
                    val deadline = end + 360_000
                    if (now >= deadline) retain()
                    else row.put("decision", "defer").put("reason", if (qualification == "skip") "analysis_pending" else
                        original.optString("reason").takeUnless { it == "peer_analysis_pending" } ?: "analysis_pending")
                        .put("defer_until_utc", Instant.ofEpochMilli(deadline).toString())
                } else row.put("decision", "skip").put("reason", "parked_visual_quiet").put("defer_until_utc", JSONObject.NULL)
            }
        }
    }
}

internal fun atomicJson(file: File, body: JSONObject) {
    check(file.parentFile?.let { it.isDirectory || it.mkdirs() } == true)
    val partial = File(file.parentFile, file.name + ".partial")
    FileOutputStream(partial).use { it.write(body.toString().toByteArray(Charsets.UTF_8)); it.fd.sync() }
    Files.move(partial.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
}

/** A dedicated thread keeps grid analysis at background priority across coroutine suspensions.
 * It never changes or restores priority on Dispatchers.IO threads shared with OBD polling. */
internal fun quietDecoderDispatcher(): ExecutorCoroutineDispatcher = Executors.newSingleThreadExecutor { task ->
    Thread({
        runCatching { android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND) }
        task.run()
    }, "quiet-clip-analysis").apply { isDaemon = true }
}.asCoroutineDispatcher()
