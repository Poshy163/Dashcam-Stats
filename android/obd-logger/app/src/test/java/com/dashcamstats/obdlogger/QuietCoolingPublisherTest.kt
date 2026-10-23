package com.dashcamstats.obdlogger

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.test.runTest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class QuietCoolingPublisherTest {
    private class Fixture(scope: CoroutineScope) : AutoCloseable {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val zone = ZoneId.systemDefault().id
        val start = System.currentTimeMillis() / 60_000 * 60_000 - 400_000
        var now = start + 170_000
        val card = "b".repeat(64)
        val root = File(context.cacheDir, "policy-cooling").apply { mkdirs() }
        val cache = File(context.filesDir, "quiet-analysis-v2.json")
        val journal: MotionJournal
        var allowed = true
        var analyses = 0
        var response: () -> JSONObject = { quiet() }
        val analyzer: VisualQuietAnalyzer

        init {
            context.deleteDatabase("clip-movement.db")
            cache.delete()
            context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
                .edit().putBoolean(QUIET_FILTER_ENABLED, true).commit()
            journal = MotionJournal(context, scope)
            for (offset in -130_000L..500_000L step 5_000) journal.writableDatabase.execSQL(
                "INSERT INTO observations(wall_ms,elapsed_ms,session_id,segment_id,kind,acc,timezone) VALUES(?,?,?,?,?,?,?)",
                arrayOf(start + offset, start + offset, journal.sessionId, "one", "acc", 0, zone))
            analyzer = object : VisualQuietAnalyzer(context) {
                override suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject {
                    assertTrue(allowed()); analyses++; return response()
                }
            }
        }

        fun item(camera: Int, at: Long = start): PolicyClip {
            val local = Instant.ofEpochMilli(at).atZone(ZoneId.systemDefault()).toLocalDateTime()
            val name = local.format(DateTimeFormatter.ofPattern("uuuuMMddHHmmss")) + "_camera_$camera.ts"
            val clip = MovementClip(name, "DCIM/Video/$name", name, "camera_$camera", local,
                10_000, at + 30_000, false, false)
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

        fun policy() = ClipTransferPolicy(context, journal, { root }, { allowed }, analyzer, { now })
        fun report() = JSONObject(File(root, "movement/transfer-policy.json").readText())
        override fun close() {
            journal.stop(); journal.close(); cache.delete()
            context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
                .edit().putBoolean(QUIET_FILTER_ENABLED, false).commit()
        }
    }

    @Test fun transientRefusalCoolsAcrossRestartThenCompletesBothViewsWithoutAHotLoop() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = listOf(f.item(0), f.item(1))
            f.response = { if (f.analyses == 1) ClipTransferPolicy.emptyVisual("unavailable",
                QuietAnalysisReason.SYSTEM_THERMAL_GUARD) else quiet() }
            var policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(1, f.analyses)
            assertEquals(2, policy.deferCount)
            val cooling = f.report().getJSONArray("decisions").getJSONObject(0)
            assertEquals("resource_cooldown", cooling.getJSONObject("visual").getString("reason"))
            assertEquals(0, cooling.getJSONObject("visual").getInt("decoded_frames"))
            assertFalse(cooling.getJSONObject("visual").getBoolean("decoder_eos"))
            val deadline = cooling.getString("defer_until_utc")
            policy.stop(); policy = f.policy()
            repeat(20) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(1, f.analyses)
            assertEquals(deadline, f.report().getJSONArray("decisions").getJSONObject(0).getString("defer_until_utc"))
            f.now += 30_000
            f.allowed = false
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(1, f.analyses)
            f.allowed = true
            repeat(2) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(3, f.analyses)
            assertEquals(2, policy.skipCount)
            assertEquals(0, policy.deferCount)
            val rows = f.report().getJSONArray("decisions")
            repeat(rows.length()) { assertTrue(ClipTransferPolicy.validVisual(rows.getJSONObject(it).getJSONObject("visual"))) }
            policy.publish(items, f.card, "ready", false) { it.camera == "camera_0" }
            assertEquals(0, policy.skipCount)
            policy.stop()
        }
    }

    @Test fun expiryReleasesCoolingAndNoFurtherAnalysisRunsAfterFixedDeadline() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = listOf(f.item(0), f.item(1))
            f.response = { ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.CPU_TEMPERATURE_GUARD) }
            val policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(1, f.analyses); assertEquals(2, policy.deferCount)
            f.now = f.start + 390_000
            repeat(20) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(1, f.analyses)
            assertEquals(0, policy.deferCount); assertEquals(0, policy.skipCount); assertEquals(2, policy.uploadCount)
            val visual = f.report().getJSONArray("decisions").getJSONObject(0).getJSONObject("visual")
            assertEquals("cpu_temperature_guard", visual.getString("reason"))
            policy.stop()
        }
    }

    @Test fun fourteenFormerSevereRefusalsCannotSuppressNewPolicyAnalysisOrInventProof() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = (0..6).flatMap { index -> listOf(f.item(0, f.start + index * 30_000),
                f.item(1, f.start + index * 30_000)) }
            f.now = f.start + 350_000
            val former = items.map { item ->
                val oldKey = movementHash("$QUIET_POLICY_VERSION/$QUIET_ALGORITHM_VERSION/resources-2/${f.card}/" +
                    "${item.clip.relativePath}/${item.clip.size}/${item.clip.mtime}/" +
                    item.body.getJSONObject("fingerprint").getString("digest"))
                JSONObject().put("key", oldKey).put("visual", ClipTransferPolicy.emptyVisual(
                    "unavailable", QuietAnalysisReason.SYSTEM_THERMAL_GUARD))
            }
            f.cache.writeText(JSONObject().put("entries", JSONArray(former)).toString())
            val policy = f.policy()
            repeat(14) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(14, f.analyses); assertEquals(14, policy.skipCount)
            policy.stop()
        }
    }

    companion object {
        private fun quiet(): JSONObject {
            val frame = IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT) { if (it % QUIET_GRID_WIDTH < 80) 50 else 100 }
            val tracker = QuietFrameTracker()
            repeat(301) { tracker.accept(frame, it * 100_000L) }
            return tracker.proof(301, true, true)
        }
    }
}
