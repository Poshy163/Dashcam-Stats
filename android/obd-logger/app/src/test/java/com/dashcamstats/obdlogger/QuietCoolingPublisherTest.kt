package com.dashcamstats.obdlogger

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.time.Duration.Companion.minutes
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.StandardTestDispatcher
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
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
        val analyzedPaths = mutableListOf<String>()
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
                    assertTrue(allowed()); analyses++; analyzedPaths += checkNotNull(uri.lastPathSegment); return response()
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

    @Test fun indexingAdmissionLossDuringDecodeCanRetryWithoutPersistingTerminalFailure() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = listOf(f.item(0), f.item(1))
            var indexingAllowed = true
            f.response = { indexingAllowed = false; quiet() }
            val policy = ClipTransferPolicy(f.context, f.journal, { f.root }, { f.allowed }, f.analyzer,
                { f.now }, backgroundScope, analysisDispatcher = StandardTestDispatcher(testScheduler),
                indexingAllowed = { indexingAllowed })
            policy.publish(items, f.card, "ready", false) { true }
            val deadline = f.report().getJSONArray("decisions").getJSONObject(0).getString("defer_until_utc")
            runCurrent()
            assertEquals(1, f.analyses)
            assertFalse(f.cache.exists())
            indexingAllowed = true
            f.response = { quiet() }
            repeat(2) {
                policy.publish(items, f.card, "ready", false) { true }
                runCurrent()
            }
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(3, f.analyses)
            assertEquals(2, policy.skipCount)
            assertEquals(Instant.ofEpochMilli(f.start + 30_000 + QUIET_TRANSFER_DEFER_MS).toString(), deadline)
            assertFalse(f.cache.readText().contains("analysis_storage_unavailable"))
            policy.stop()
        }
    }

    @Test fun transferExpiryReleasesUploadWhileCoolingContinuesOnlyWithinFixedAnalysisHorizon() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = listOf(f.item(0), f.item(1))
            f.response = { ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.CPU_TEMPERATURE_GUARD) }
            val policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(1, f.analyses); assertEquals(2, policy.deferCount)
            f.now = f.start + 390_000
            repeat(20) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(2, f.analyses)
            assertEquals(0, policy.deferCount); assertEquals(0, policy.skipCount); assertEquals(2, policy.uploadCount)
            val visual = f.report().getJSONArray("decisions").getJSONObject(0).getJSONObject("visual")
            assertEquals("resource_cooldown", visual.getString("reason"))
            f.now = f.start + 30_000 + QUIET_ANALYSIS_RETRY_MS
            repeat(20) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(2, f.analyses)
            assertEquals(0, policy.deferCount); assertEquals(2, policy.uploadCount)
            policy.stop()
        }
    }

    @Test fun coolingAfterTransferDeadlineCanCompleteFreshPairedProofWithoutRenewingAnyHold() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = listOf(f.item(0), f.item(1))
            f.response = { ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.SYSTEM_THERMAL_GUARD) }
            var policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            val originalDeadline = f.report().getJSONArray("decisions").getJSONObject(0).getString("defer_until_utc")
            f.now = f.start + 600_000
            policy.stop(); policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(2, f.analyses)
            assertEquals(0, policy.deferCount); assertEquals(0, policy.skipCount); assertEquals(2, policy.uploadCount)
            repeat(2) { assertTrue(f.report().getJSONArray("decisions").getJSONObject(it).isNull("defer_until_utc")) }
            f.now += 60_000
            f.response = { quiet() }
            repeat(2) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(4, f.analyses); assertEquals(2, policy.skipCount); assertEquals(0, policy.deferCount)
            val decisions = f.report().getJSONArray("decisions")
            repeat(2) {
                val row = decisions.getJSONObject(it)
                assertTrue(ClipTransferPolicy.validVisual(row.getJSONObject("visual")))
                assertTrue(row.isNull("defer_until_utc"))
            }
            assertEquals(Instant.ofEpochMilli(f.start + 390_000).toString(), originalDeadline)
            // Expiry did not authorize a changed peer or erase an actual activity decision.
            policy.publish(items, f.card, "ready", false) { it.relativePath != items[1].clip.relativePath }
            assertEquals(0, policy.skipCount); assertEquals(2, policy.uploadCount)
            policy.stop()
        }
    }

    @Test fun schedulerCompletesOppositeViewBeforeSpendingCoolOpportunityOnNewUnpairedClips() = runTest {
        Fixture(backgroundScope).use { f ->
            val first = f.item(0)
            val later = f.item(0, f.start + 60_000)
            val peer = f.item(1)
            val policy = f.policy()
            // Order deliberately places the opposite view behind another candidate.
            val items = listOf(first, later, peer)
            policy.publish(items, f.card, "ready", false) { true }
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(listOf(first.clip.name, peer.clip.name), f.analyzedPaths)
            assertEquals(2, policy.skipCount)
            policy.stop()
        }
    }

    @Test fun guardedParkedFingerprintGetsPriorityButLockedUnknownAndMovingClipsDoNot() = runTest {
        Fixture(backgroundScope).use { f ->
            val item = f.item(0)
            val policy = f.policy()
            fun eligible(clip: MovementClip = item.clip, start: Long? = item.startMs,
                         end: Long? = item.boundEndMs, classification: String = "acc_off_candidate") =
                policy.fingerprintCandidate(clip, start, end, classification, f.now)
            assertTrue(eligible())
            assertFalse(eligible(item.clip.copy(locked = true)))
            assertFalse(eligible(item.clip.copy(orphan = true)))
            assertFalse(eligible(start = null))
            assertFalse(eligible(classification = "moving_observed"))
            assertFalse(eligible(classification = "unknown"))
            f.now = f.start + 30_000 + QUIET_ANALYSIS_RETRY_MS
            assertFalse(eligible())
            policy.stop()
        }
    }

    @Test fun fullCacheEvictionCannotLoopDecodeButRemovedOrChangedIdentityIsReconsideredSafely() = runTest(timeout = 3.minutes) {
        Fixture(backgroundScope).use { f ->
            val items = (0..128).map { f.item(0, f.start + it * 1_000) }
            f.response = { ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.HARDWARE_DECODER_UNAVAILABLE) }
            val policy = f.policy()
            // Grow the real retained inventory so the first attempted identity remains present
            // while all 129 results enter the 128-entry cache. This avoids 129 redundant full-card
            // rewrites, without seeding cache internals or bypassing the publisher's eviction.
            for (count in 1..items.size) policy.publish(items.take(count), f.card, "ready", false) { true }
            assertEquals(129, f.analyses)
            assertEquals(128, JSONObject(f.cache.readText()).getJSONArray("entries").length())
            repeat(3) { policy.publish(items, f.card, "ready", false) { true } }
            assertEquals(129, f.analyses) // First terminal identity was evicted; it still has been attempted.
            assertEquals(0, policy.skipCount)
            policy.publish(items.drop(1), f.card, "ready", false) { true }
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(130, f.analyses) // Reappearing metadata is a new opportunity, with no inherited quiet proof.
            val original = items.first()
            val changed = original.copy(clip = original.clip.copy(size = original.clip.size + 1),
                body = JSONObject(original.body.toString()).put("size_bytes", original.clip.size + 1)
                    .put("fingerprint", JSONObject(original.body.getJSONObject("fingerprint").toString())
                        .put("digest", "c".repeat(64))))
            policy.publish(listOf(changed) + items.drop(1), f.card, "ready", false) { true }
            assertEquals(131, f.analyses)
            assertEquals(0, policy.skipCount)
            policy.stop()
        }
    }

    @Test fun fullCardDecoderOnlySpendsAnAttemptOnAClipRetainedInThePublishedReport() = runTest {
        Fixture(backgroundScope).use { f ->
            f.now = f.start + 500_000
            val items = (255 downTo 0).flatMap { index -> listOf(f.item(0, f.start + index * 1_000),
                f.item(1, f.start + index * 1_000)) }
            f.response = {
                val report = f.report()
                assertTrue(report.getBoolean("truncated"))
                val rows = report.getJSONArray("decisions")
                val paths = (0 until rows.length()).map {
                    rows.getJSONObject(it).getJSONObject("clip").getString("relative_path").substringAfterLast('/')
                }
                assertTrue(f.analyzedPaths.last() in paths)
                ClipTransferPolicy.emptyVisual("unavailable", QuietAnalysisReason.HARDWARE_DECODER_UNAVAILABLE)
            }
            val policy = f.policy()
            policy.publish(items, f.card, "ready", false) { true }
            assertEquals(1, f.analyses)
            assertEquals(0, policy.skipCount)
            policy.stop()
        }
    }

    @Test fun fourteenFormerSevereRefusalsCannotSuppressNewPolicyAnalysisOrInventProof() = runTest {
        Fixture(backgroundScope).use { f ->
            val items = (0..6).flatMap { index -> listOf(f.item(0, f.start + index * 30_000),
                f.item(1, f.start + index * 30_000)) }
            f.now = f.start + 350_000
            val former = items.map { item ->
                val oldKey = movementHash("$QUIET_POLICY_VERSION/$QUIET_ALGORITHM_VERSION/resources-3/${f.card}/" +
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
