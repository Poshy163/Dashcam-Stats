package com.dashcamstats.obdlogger

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import java.io.File
import java.time.Instant
import java.time.ZoneId
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.yield
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
@OptIn(ExperimentalCoroutinesApi::class)
class ClipTransferPolicyTest {
    private val zone = ZoneId.systemDefault().id
    private fun point(at: Long, acc: Boolean? = false, segment: String = "one") =
        MotionObservation(at, at, "session", segment, "acc", acc = acc, timezone = zone)
    private fun guard(rows: List<MotionObservation>, healthy: Boolean = true) = ParkedGuard.assess(0, 300_000, rows, healthy, zone)
    private fun off() = (-5_000L..305_000L step 5_000).map { point(it) }
    private fun quiet(): JSONObject {
        val tracker = QuietFrameTracker()
        val frame = scene()
        repeat(301) { tracker.accept(frame, it * 100_000L) }
        return tracker.proof(301, true, true)
    }
    private fun scene() = IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT) { if (it % QUIET_GRID_WIDTH < 80) 50 else 100 }

    @Test fun completeOffCoveragePassesButMissingNullOnRestartAndJournalGapsNeverDo() {
        assertTrue(ParkedGuard.valid(guard(off())))
        assertFalse(ParkedGuard.valid(guard(off().filter { it.wallMs != 100_000L })))
        assertFalse(ParkedGuard.valid(guard(off().map { if (it.wallMs == 100_000L) it.copy(acc = null) else it })))
        assertFalse(ParkedGuard.valid(guard(off().map { if (it.wallMs == 100_000L) it.copy(acc = true) else it })))
        assertFalse(ParkedGuard.valid(guard(off().map { if (it.wallMs >= 100_000L) it.copy(segmentId = "restart") else it })))
        assertFalse(ParkedGuard.valid(guard(off(), false)))
        assertFalse(ParkedGuard.valid(guard(off().map { it.copy(timezone = "wrong") })))
    }

    @Test fun eitherValidatedSpeedOrConfidentGpsProtectsAnAccOffClip() {
        val obd = point(100_000).copy(kind = "obd", speed = 1.0)
        assertFalse(ParkedGuard.valid(guard(off() + obd)))
        val gps = obd.copy(kind = "gps", speed = 4.0, accuracy = .2, ageMs = 100, horizontalAccuracy = 2.0)
        assertFalse(ParkedGuard.valid(guard(off() + gps)))
        assertTrue(ParkedGuard.valid(guard(off() + gps.copy(speed = .297, accuracy = .36))))
    }

    @Test fun streamingAnalysisDetectsBriefActivityAndRollingChange() {
        assertTrue(ClipTransferPolicy.validVisual(quiet()))
        val tracker = QuietFrameTracker()
        val frame = scene()
        repeat(301) { index ->
            val next = frame.copyOf()
            if (index == 150) for (y in 20..26) for (x in 20..26) next[y * QUIET_GRID_WIDTH + x] += 12
            tracker.accept(next, index * 100_000L)
        }
        assertEquals("activity", tracker.proof(301, true, true).getString("status"))
        val slow = QuietFrameTracker()
        repeat(301) { index ->
            val next = scene()
            for (y in 20..40) for (x in 20..40) next[y * QUIET_GRID_WIDTH + x] += minOf(index, 10) * 3
            slow.accept(next, index * 100_000L)
        }
        assertTrue(slow.motionFrames > 0)
    }

    @Test fun eosFrameCountPtsCoverageAndGridAreMandatory() {
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("input_eos", false)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("decoder_eos", false)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("sampled_frames", 3)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("max_gap_ms", 151)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("covered_ms", 1_000)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("grid_width", 16)))
        val tracker = QuietFrameTracker()
        val frame = scene()
        repeat(301) { tracker.accept(frame, if (it == 150) 149 * 100_000L else it * 100_000L) }
        assertEquals("incomplete", tracker.proof(301, true, true).getString("status"))
    }

    @Test fun obscuredDarkBrightOrLowContrastFramesCannotBecomeQuiet() {
        for (level in listOf(0, 50, 250)) {
            val tracker = QuietFrameTracker()
            tracker.accept(IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT) { level }, 0)
            assertFalse(tracker.qualityValid)
            assertEquals("unavailable", tracker.proof(1, false, false).getString("status"))
        }
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("min_mean_luma", 39.99)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("max_dark_fraction", .551)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("max_bright_fraction", .401)))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("min_contrast_span", 23)))
    }

    @Test fun onlyKnownBottomOverlayIsMaskedAndSmallPersistentSceneMotionStillCounts() {
        val tracker = QuietFrameTracker()
        repeat(301) { index ->
            val next = scene()
            for (i in QUIET_GRID_WIDTH * QUIET_ROI_ROWS until next.size) next[i] = if (index % 2 == 0) 0 else 255
            tracker.accept(next, index * 100_000L)
        }
        assertTrue(ClipTransferPolicy.validVisual(tracker.proof(301, true, true)))
        val small = QuietFrameTracker()
        repeat(4) { index ->
            val next = scene()
            if (index % 2 != 0) for (y in 20..22) for (x in 20..22) next[y * QUIET_GRID_WIDTH + x] += 12
            small.accept(next, index * 40_000L)
        }
        assertTrue(small.maxConsecutiveCoherentFrames >= 2)
        assertTrue(small.motionFrames > 0)
    }

    @Test fun cpuThermalGuardUsesMeasuredSocSensorAndNeverAcceptsUnknownOrHotReadings() {
        assertTrue(VisualQuietAnalyzer.validCpuTemperature("soc-thmzone", 83_560))
        assertTrue(VisualQuietAnalyzer.validCpuTemperature("soc-thmzone", 85_000))
        assertTrue(VisualQuietAnalyzer.validCpuTemperature("soc-thmzone", 94_015))
        assertFalse(VisualQuietAnalyzer.validCpuTemperature("soc-thmzone", 95_000))
        assertFalse(VisualQuietAnalyzer.validCpuTemperature("battery", 30_000))
        assertFalse(VisualQuietAnalyzer.validCpuTemperature(null, null))
        assertFalse(VisualQuietAnalyzer.validCpuTemperature("soc-thmzone", 79))
    }

    @Test fun resourceRefusalsExplainAdmissionAndPreserveBatteryLimit() {
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_GUARD,
            VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", 97_800))
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_GUARD,
            VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", 95_000))
        assertNull(VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", 94_999))
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE,
            VisualQuietAnalyzer.cpuTemperatureRefusal("battery", 30_000))
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE,
            VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", null))
        assertNull(VisualQuietAnalyzer.batteryTemperatureRefusal(419))
        assertEquals(QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD,
            VisualQuietAnalyzer.batteryTemperatureRefusal(420))
        assertEquals(QuietAnalysisReason.BATTERY_TEMPERATURE_UNAVAILABLE,
            VisualQuietAnalyzer.batteryTemperatureRefusal(null))
    }

    @Test fun guardedAnalysisReportsItsReasonBeforeOpeningOrDecodingVideo() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val analyzer = object : VisualQuietAnalyzer(context) {
            override fun resourceRefusal(phase: QuietThermalPhase) = QuietAnalysisReason.CPU_TEMPERATURE_GUARD
        }
        // This intentionally invalid URI must never be opened when a resource guard refuses.
        val report = analyzer.analyze(Uri.parse("content://invalid/private-test.ts")) { true }
        assertEquals("unavailable", report.getString("status"))
        assertEquals("cpu_temperature_guard", report.getString("reason"))
        assertEquals(0, report.getInt("decoded_frames"))
        assertFalse(ClipTransferPolicy.validVisual(report))
        val stopped = analyzer.analyze(Uri.parse("content://invalid/private-test.ts")) { false }
        assertEquals("policy_not_permitted", stopped.getString("reason"))
        assertFalse(stopped.toString().contains("private-test"))
    }

    @Test fun visualReasonsDistinguishImageQualityCoverageAndDecoderFailure() {
        assertTrue(quiet().isNull("reason"))
        assertFalse(ClipTransferPolicy.validVisual(quiet().put("reason", "cpu_temperature_guard")))
        val dark = QuietFrameTracker().apply { accept(IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT), 0) }
        assertEquals("frame_quality", dark.proof(1, false, false).getString("reason"))
        assertEquals("coverage_incomplete", QuietFrameTracker().proof(0, false, false).getString("reason"))
        assertEquals("decoder_failure", ClipTransferPolicy.emptyVisual("error").getString("reason"))
        assertEquals("analysis_pending", ClipTransferPolicy.emptyVisual().getString("reason"))
    }

    @Test fun productionDecoderUsesItsOwnBackgroundThreadAcrossSuspensions() = runBlocking {
        val dispatcher = quietDecoderDispatcher()
        try {
            withContext(dispatcher) {
                assertTrue(Thread.currentThread().name.startsWith("quiet-clip-analysis"))
                val thread = Thread.currentThread().id
                assertEquals(android.os.Process.THREAD_PRIORITY_BACKGROUND,
                    android.os.Process.getThreadPriority(android.os.Process.myTid()))
                yield()
                assertEquals(thread, Thread.currentThread().id)
                assertEquals(android.os.Process.THREAD_PRIORITY_BACKGROUND,
                    android.os.Process.getThreadPriority(android.os.Process.myTid()))
            }
        } finally { dispatcher.close() }
    }

    private fun pair(camera: Int, start: Long = 1_000_000, decision: String = "skip", duration: Long = 30_000): JSONObject = JSONObject()
        .put("clip", JSONObject().put("camera_key", "camera_$camera")
            .put("relative_path", "DCIM/Video/example_${start}_camera_$camera.ts")
            .put("local_start", Instant.ofEpochMilli(start).atZone(ZoneId.systemDefault()).toLocalDateTime().toString())
            .put("start_utc", Instant.ofEpochMilli(start).toString())
            .put("end_utc", Instant.ofEpochMilli(start + duration).toString()))
        .put("decision", decision).put("reason", "parked_visual_quiet")

    @Test fun missingActivityUnknownOrAmbiguousPeerAlwaysKeepsBothLenses() {
        val lone = pair(0); ClipTransferPolicy.applyPairGate(listOf(lone)); assertEquals("upload", lone.getString("decision"))
        val first = pair(0); val second = pair(1, decision = "upload")
        ClipTransferPolicy.applyPairGate(listOf(first, second)); assertEquals("upload", first.getString("decision"))
        val a = pair(0); val b = pair(1)
        ClipTransferPolicy.applyPairGate(listOf(a, b)); assertEquals("skip", a.getString("decision")); assertEquals("skip", b.getString("decision"))
        val c = pair(0); val d = pair(1, 1_002_000)
        ClipTransferPolicy.applyPairGate(listOf(c, d)); assertEquals("upload", c.getString("decision")); assertEquals("upload", d.getString("decision"))
        val duplicate = listOf(pair(0), pair(1), pair(1))
        ClipTransferPolicy.applyPairGate(duplicate)
        assertEquals("upload", duplicate[0].getString("decision"))
    }

    @Test fun staggeredThreeSecondViewsNeedAllOverlapsWithoutRecursiveBoundaryStarvation() {
        val rows = listOf(pair(0, duration = 60_000), pair(1, 943_000, duration = 60_000),
            pair(1, 1_003_000, duration = 60_000))
        ClipTransferPolicy.applyPairGate(rows)
        assertEquals("skip", rows[0].getString("decision"))
        assertTrue(rows.drop(1).all { it.getString("reason") == "peer_coverage_required" })
        val first = rows.map { it.toString() }
        ClipTransferPolicy.applyPairGate(rows)
        assertEquals(first, rows.map { it.toString() })
        val reversed = listOf(pair(1, 1_003_000, duration = 60_000), pair(1, 943_000, duration = 60_000),
            pair(0, duration = 60_000))
        ClipTransferPolicy.applyPairGate(reversed)
        assertEquals("skip", reversed.last().getString("decision"))
    }

    @Test fun previousOverlapActivityLockedUnknownAndOneMillisecondGapCannotBeBridged() {
        for (unsafe in listOf("visual_activity", "locked_clip", "visual_incomplete")) {
            val front = pair(0, duration = 60_000)
            val previous = pair(1, 943_000, "upload", 60_000).put("reason", unsafe)
            ClipTransferPolicy.applyPairGate(listOf(front, previous, pair(1, 1_003_000, duration = 60_000)))
            assertEquals("upload", front.getString("decision"))
        }
        val front = pair(0, duration = 60_000)
        val unknown = pair(1, 943_000).also { it.getJSONObject("clip")
            .put("start_utc", JSONObject.NULL).put("end_utc", JSONObject.NULL) }
        ClipTransferPolicy.applyPairGate(listOf(front, unknown, pair(1, 1_000_000, duration = 60_000)))
        assertEquals("upload", front.getString("decision"))
        val sameView = pair(0, duration = 60_000)
        val ambiguous = pair(0, 1_030_000, decision = "upload").also { it.getJSONObject("clip")
            .put("start_utc", JSONObject.NULL).put("end_utc", JSONObject.NULL) }
        ClipTransferPolicy.applyPairGate(listOf(sameView, ambiguous, pair(1, duration = 60_000)))
        assertEquals("upload", sameView.getString("decision"))
        val gap = pair(0, duration = 60_000)
        ClipTransferPolicy.applyPairGate(listOf(gap, pair(1, 943_000, duration = 59_999),
            pair(1, 1_003_000, duration = 60_000)))
        assertEquals("upload", gap.getString("decision"))
    }

    @Test fun pendingCoverageUsesOwnFixedDeadlineAndRetainedWitnessReason() {
        val front = pair(0, decision = "defer", duration = 60_000).put("reason", "analysis_pending")
        val previous = pair(1, 943_000, duration = 60_000)
        val current = pair(1, 1_003_000, decision = "defer", duration = 60_000).put("reason", "analysis_pending")
        ClipTransferPolicy.applyPairGate(listOf(front, previous, current), 1_100_000)
        assertEquals("defer", front.getString("decision"))
        assertEquals(Instant.ofEpochMilli(1_420_000).toString(), front.getString("defer_until_utc"))
        assertEquals("peer_analysis_pending", current.getString("reason"))
        ClipTransferPolicy.applyPairGate(listOf(front, previous, current), 1_420_000)
        assertEquals("upload", front.getString("decision"))
        assertEquals("peer_analysis_pending", front.getString("reason"))
        assertTrue(front.isNull("defer_until_utc"))
    }

    @Test fun fullCardReportStaysWithinByteCapAndCannotKeepAnExclusionAfterItsWitnessIsTrimmed() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        val prefs = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, true).commit()
        val journal = MotionJournal(context, backgroundScope)
        val root = File(context.cacheDir, "policy-full-card").apply { mkdirs() }
        val policy = ClipTransferPolicy(context, journal, { root }, { false })
        val unknown = MovementClassifier.assess(null, null, emptyList())
        val absentGuard = ParkedGuard.assess(null, null, emptyList(), false, zone)
        fun reportRow(camera: Int, at: Long, decision: String): JSONObject = pair(camera, at, decision).also { row ->
            row.put("visual", ClipTransferPolicy.emptyVisual()).put("guard", JSONObject(absentGuard.toString()))
                .put("defer_until_utc", JSONObject.NULL)
            row.getJSONObject("clip").put("classification", "unknown").put("analysis_status", "unknown")
                .put("reasons", JSONArray(unknown.reasons)).put("coverage", unknown.coverage).put("evidence", unknown.evidence)
        }
        val target = reportRow(0, 1_000_000, "skip")
        val witness = reportRow(1, 1_000_000, "skip")
        val rows = mutableListOf(target)
        repeat(510) { rows += reportRow(0, 2_000_000 + it * 60_000L, "upload").put("reason", "observation_gap") }
        rows += witness
        assertEquals(MOVEMENT_REPORT_MAX_CLIPS, rows.size)
        // Exercise the actual report writer with a full-card projection. Only output
        // bounds and witness safety are asserted, independent of its trimming strategy.
        val writer = ClipTransferPolicy::class.java.getDeclaredMethod("writeReport", List::class.java,
            String::class.java, String::class.java, Boolean::class.javaPrimitiveType)
        writer.isAccessible = true
        writer.invoke(policy, rows, "b".repeat(64), "ready", false)
        val file = File(root, "movement/transfer-policy.json")
        val report = JSONObject(file.readText())
        assertTrue(file.length() <= MOVEMENT_REPORT_MAX_BYTES)
        assertTrue(report.getBoolean("truncated"))
        val kept = report.getJSONArray("decisions")
        assertTrue(kept.length() in 1 until MOVEMENT_REPORT_MAX_CLIPS)
        assertEquals("upload", kept.getJSONObject(0).getString("decision"))
        assertEquals("peer_coverage_required", kept.getJSONObject(0).getString("reason"))
        assertTrue((0 until kept.length()).none { kept.getJSONObject(it).getJSONObject("clip").getString("camera_key") == "camera_1" })
        assertEquals(0, policy.skipCount)
        assertEquals("skip", target.getString("decision"))
        policy.stop(); journal.stop(); journal.close()
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, false).commit()
    }

    @Test fun defaultPolicyPublishesBoundedDisabledDecisionsWithoutDecoding() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE).edit().remove(QUIET_FILTER_ENABLED).commit()
        context.deleteDatabase("clip-movement.db")
        val journal = MotionJournal(context, backgroundScope)
        val root = File(context.cacheDir, "policy-default").apply { mkdirs() }
        var analyses = 0
        val fake = object : VisualQuietAnalyzer(context) {
            override suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject { analyses++; return quiet() }
        }
        val policy = ClipTransferPolicy(context, journal, { root }, { true }, fake)
        policy.publish(emptyList(), null, "directory_required", false) { true }
        val report = JSONObject(File(root, "movement/transfer-policy.json").readText())
        assertFalse(report.getBoolean("enabled")); assertEquals("directory_required", report.getString("status"))
        assertEquals("disabled", policy.state); assertEquals(0, analyses)
        assertFalse(File(root, "movement/transfer-policy.json.partial").exists())
        journal.stop(); journal.close()
    }

    @Test fun capabilityAndStatusExposeDisabledPolicyBeforeAnyFolderGrant() {
        val report = StatusPublisher.buildStatusJson(PublicStatus("parked", true), 0)
        assertTrue(report.getJSONArray("capabilities").toString().contains("clip_transfer_policy_v1"))
        assertFalse(report.getBoolean("transfer_policy_enabled"))
        assertEquals("disabled", report.getString("transfer_policy_state"))
    }

    @Test fun actualPublisherAnalyzesStaggeredClipsEvenWhenTheirFinalPeerGateRetainsThem() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        File(context.filesDir, "quiet-analysis-v2.json").delete()
        val prefs = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, true).commit()
        val journal = MotionJournal(context, backgroundScope)
        val now = System.currentTimeMillis()
        val start = now / 60_000 * 60_000 - 180_000
        for (offset in -200_000L..200_000L step 5_000) journal.writableDatabase.execSQL(
            "INSERT INTO observations(wall_ms,elapsed_ms,session_id,segment_id,kind,acc,timezone) VALUES(?,?,?,?,?,?,?)",
            arrayOf(start + offset, start + offset, journal.sessionId, "one", "acc", 0, zone))
        fun item(camera: Int, at: Long): PolicyClip {
            val local = Instant.ofEpochMilli(at).atZone(ZoneId.systemDefault()).toLocalDateTime()
            val name = local.format(java.time.format.DateTimeFormatter.ofPattern("uuuuMMddHHmmss")) + "_camera_$camera.ts"
            val clip = MovementClip(name, "DCIM/Video/$name", name, "camera_$camera", local, 10_000, at + 30_000, false, false)
            val assessment = MovementClassifier.assess(at, at + 30_000, journal.between(at, at + 30_000))
            val body = JSONObject().put("clip_id", java.util.UUID.nameUUIDFromBytes(name.toByteArray()).toString())
                .put("relative_path", clip.relativePath).put("camera_key", clip.camera).put("locked", false)
                .put("size_bytes", clip.size).put("mtime_ms", clip.mtime)
                .put("fingerprint", JSONObject().put("algorithm", "sha256-sampled-v1").put("sample_bytes", 16_384)
                    .put("digest", "a".repeat(64)))
                .put("local_start", local.toString()).put("timezone", zone)
                .put("start_utc", Instant.ofEpochMilli(at).toString()).put("end_utc", Instant.ofEpochMilli(at + 30_000).toString())
                .put("duration_ms", JSONObject.NULL).put("time_quality", "bounded")
                .put("classification", assessment.classification).put("reasons", JSONArray(assessment.reasons))
                .put("analysis_status", "complete").put("coverage", assessment.coverage).put("evidence", assessment.evidence)
            return PolicyClip(clip, body, true, true, Uri.parse("content://test/$name"), at, at + 30_000)
        }
        val items = listOf(item(0, start), item(1, start - 27_000), item(1, start + 3_000))
        val root = File(context.cacheDir, "policy-staggered").apply { mkdirs() }
        var analyses = 0
        val fake = object : VisualQuietAnalyzer(context) {
            override suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject {
                assertTrue(allowed()); analyses++; return quiet()
            }
        }
        val policy = ClipTransferPolicy(context, journal, { root }, { true }, fake, System::currentTimeMillis)
        repeat(3) { policy.publish(items, "b".repeat(64), "ready", false) { true } }
        assertEquals(3, analyses)
        assertEquals(1, policy.skipCount)
        assertEquals(2, policy.uploadCount)
        val report = JSONObject(File(root, "movement/transfer-policy.json").readText())
        val rows = report.getJSONArray("decisions")
        assertEquals("skip", rows.getJSONObject(0).getString("decision"))
        assertEquals("peer_coverage_required", rows.getJSONObject(1).getString("reason"))
        assertEquals("peer_coverage_required", rows.getJSONObject(2).getString("reason"))
        File(System.getProperty("java.io.tmpdir"), "transfer-policy-staggered-v1-android.json").writeText(report.toString(2))
        // A changed independently quiet witness invalidates the candidate before publication.
        policy.publish(items, "b".repeat(64), "ready", false) { it.relativePath != items[1].clip.relativePath }
        assertEquals(0, policy.skipCount)
        policy.stop(); journal.stop(); journal.close()
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, false).commit()
    }

    @Test fun actualPublisherRequiresBothAnalysesFreshIdentityAndPersistentCoverageAndDisablesImmediately() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        File(context.filesDir, "quiet-analysis-v2.json").delete()
        val prefs = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, true).commit()
        val journal = MotionJournal(context, backgroundScope)
        val start = System.currentTimeMillis() / 60_000 * 60_000 - 180_000
        val db = journal.writableDatabase
        for (offset in -130_000L..160_000L step 5_000) db.execSQL("INSERT INTO observations(" +
            "wall_ms,elapsed_ms,session_id,segment_id,kind,acc,timezone) VALUES(?,?,?,?,?,?,?)",
            arrayOf(start + offset, start + offset, journal.sessionId, "one", "acc", 0, zone))
        fun item(camera: Int): PolicyClip {
            val local = Instant.ofEpochMilli(start).atZone(ZoneId.systemDefault()).toLocalDateTime()
            val clip = MovementClip("id$camera", "DCIM/Video/" + local.format(java.time.format.DateTimeFormatter.ofPattern("uuuuMMddHHmmss")) +
                "_camera_$camera.ts", "name", "camera_$camera", local, 10_000, start + 30_000, false, false)
            val assessment = MovementClassifier.assess(start, start + 30_000, journal.between(start, start + 30_000))
            val body = JSONObject().put("clip_id", java.util.UUID.randomUUID().toString())
                .put("relative_path", clip.relativePath).put("camera_key", clip.camera).put("locked", false)
                .put("size_bytes", clip.size).put("mtime_ms", clip.mtime)
                .put("fingerprint", JSONObject().put("algorithm", "sha256-sampled-v1").put("sample_bytes", 16_384)
                    .put("digest", "a".repeat(64)))
                .put("local_start", local.toString()).put("timezone", zone)
                .put("start_utc", Instant.ofEpochMilli(start).toString()).put("end_utc", Instant.ofEpochMilli(start + 30_000).toString())
                .put("duration_ms", JSONObject.NULL).put("time_quality", "bounded")
                .put("classification", assessment.classification).put("reasons", JSONArray(assessment.reasons))
                .put("analysis_status", "complete").put("coverage", assessment.coverage).put("evidence", assessment.evidence)
            return PolicyClip(clip, body, true, true, Uri.parse("content://test/$camera"), start, start + 30_000)
        }
        val items = listOf(item(0), item(1))
        val root = File(context.cacheDir, "policy-active").apply { mkdirs() }
        var analyses = 0
        val fake = object : VisualQuietAnalyzer(context) {
            override suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject {
                val preliminary = JSONObject(File(root, "movement/transfer-policy.json").readText())
                val decisions = preliminary.getJSONArray("decisions")
                assertTrue((0 until decisions.length()).all { decisions.getJSONObject(it).getString("decision") == "defer" })
                analyses++; return quiet()
            }
        }
        val clock: () -> Long = System::currentTimeMillis
        // A refusal cached by the former 85C/MODERATE resource policy must be
        // re-evaluated under the new policy, never inherited forever by this identity.
        val legacyEntries = items.map { item ->
            val key = movementHash("$QUIET_POLICY_VERSION/$QUIET_ALGORITHM_VERSION/${"b".repeat(64)}/" +
                "${item.clip.relativePath}/${item.clip.size}/${item.clip.mtime}/" +
                item.body.getJSONObject("fingerprint").getString("digest"))
            JSONObject().put("key", key).put("visual", ClipTransferPolicy.emptyVisual(
                "unavailable", QuietAnalysisReason.CPU_TEMPERATURE_GUARD))
        }
        File(context.filesDir, "quiet-analysis-v2.json").writeText(
            JSONObject().put("entries", JSONArray(legacyEntries)).toString())
        val policy = ClipTransferPolicy(context, journal, { root }, { true }, fake, clock)
        policy.publish(items.map { it.copy(stable = false, freshFingerprint = false) }, "b".repeat(64), "ready", false) { true }
        assertEquals(0, analyses); assertEquals(2, policy.deferCount)
        File(System.getProperty("java.io.tmpdir"), "transfer-policy-defer-v1-android.json")
            .writeText(JSONObject(File(root, "movement/transfer-policy.json").readText()).toString(2))
        policy.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals(1, analyses); assertEquals(0, policy.skipCount); assertEquals(2, policy.deferCount)
        policy.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals(2, analyses); assertEquals(2, policy.skipCount)
        val report = File(root, "movement/transfer-policy.json")
        assertTrue(report.length() <= MOVEMENT_REPORT_MAX_BYTES)
        // Actual Android output is shared with the backend parser contract test.
        File(System.getProperty("java.io.tmpdir"), "transfer-policy-v1-android.json").writeText(JSONObject(report.readText()).toString(2))
        File(context.filesDir, "quiet-analysis-v2.json").delete()
        val gate = CompletableDeferred<JSONObject>()
        var asynchronousCalls = 0
        val delayed = object : VisualQuietAnalyzer(context) {
            override suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject { asynchronousCalls++; return gate.await() }
        }
        val asynchronous = ClipTransferPolicy(context, journal, { root }, { true }, delayed, clock,
            backgroundScope, afterAnalysis = { throw IllegalStateException("synthetic wake callback failure") },
            analysisDispatcher = StandardTestDispatcher(testScheduler))
        asynchronous.publish(items, "b".repeat(64), "ready", false) { true }
        runCurrent()
        assertEquals(1, asynchronousCalls)
        // A slow decoder never blocks metadata/pending publication and never launches a second decoder.
        repeat(3) { asynchronous.publish(items, "b".repeat(64), "ready", false) { true } }
        assertEquals(1, asynchronousCalls); assertEquals(2, asynchronous.deferCount)
        // Make the real atomic cache rename fail after the decoder produced a valid quiet result.
        // The actor and its wake callback must not crash the logger or retain an in-memory skip.
        val blockedCache = File(context.filesDir, "quiet-analysis-v2.json")
        assertTrue(blockedCache.mkdir())
        val blocker = File(blockedCache, "prevent-replacement").apply { writeText("synthetic cache obstruction") }
        gate.complete(quiet()); runCurrent()
        assertEquals("unavailable", asynchronous.state)
        asynchronous.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals(0, asynchronous.skipCount); assertEquals(0, asynchronous.deferCount)
        assertEquals(2, asynchronous.uploadCount); assertEquals(1, asynchronousCalls)
        assertTrue(blocker.delete()); assertTrue(blockedCache.delete())
        File(context.filesDir, "quiet-analysis-v2.json.partial").delete()
        asynchronous.stop()
        File(context.filesDir, "quiet-analysis-v2.json").delete()
        val expired = ClipTransferPolicy(context, journal, { root }, { true }, fake, { clock() + 600_000 })
        expired.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals(0, expired.deferCount); assertEquals(0, expired.skipCount); assertEquals(2, analyses)
        policy.publish(items.map { it.copy(freshFingerprint = false) }, "b".repeat(64), "ready", false) { true }
        // Cached content proof remains advisory until the server recomputes the current sampled hash.
        assertEquals(2, policy.skipCount)
        policy.publish(items, "b".repeat(64), "ready", false) { false }
        assertEquals(0, policy.skipCount)
        db.execSQL("INSERT INTO coverage_gaps(start_ms,end_ms) VALUES(?,?)", arrayOf(start + 10_000, start + 20_000))
        val reopened = ClipTransferPolicy(context, journal, { root }, { true }, fake)
        reopened.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals(0, reopened.skipCount); assertEquals(2, analyses)
        prefs.edit().putBoolean(QUIET_FILTER_ENABLED, false).commit()
        reopened.publish(items, "b".repeat(64), "ready", false) { true }
        assertEquals("disabled", reopened.state)
        assertFalse(JSONObject(report.readText()).getBoolean("enabled"))
        journal.stop(); journal.close()
    }
}
