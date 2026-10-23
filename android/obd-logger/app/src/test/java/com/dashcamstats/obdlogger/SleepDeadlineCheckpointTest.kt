package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
class SleepDeadlineCheckpointTest {
    @get:Rule val temporary = TemporaryFolder()
    private val boot = SleepBootIdentity("01234567-1234-1234-1234-012345678901", 12)

    private fun file(name: String = "deadline.json") = File(temporary.root, name)
    private fun store(file: File = file()) = AtomicSleepDeadlineCheckpointStore(file)
    private fun SleepDeadlineTracker.read(at: Long, on: Boolean?, window: Int? = 1200,
                                         identity: SleepBootIdentity = boot) =
        observe(identity, at, at + 100, on, window)

    private fun seeded(file: File = file(), window: Int = 1200): SleepDeadlineEvidence {
        val tracker = SleepDeadlineTracker(store(file))
        tracker.read(1000, true, window)
        tracker.read(6000, false, window)
        assertTrue("private checkpoint round trip", store(file).read() != null)
        return checkNotNull(tracker.snapshot(6100))
    }

    @Test
    fun ordinaryProcessRestartAndUpdateRetainExactDeadlineWithFreshNativeOff() {
        val file = file()
        val original = seeded(file)
        var expectedObservation = original.observedElapsedMillis
        // A new tracker and newly opened private file model each replacement app process.
        repeat(3) { index ->
            val fresh = SleepDeadlineTracker(store(file))
            assertNull(fresh.snapshot(expectedObservation)) // Never publish without a live read.
            val at = 11_000L + index * 5_000L
            fresh.read(at, false)
            val restored = checkNotNull(fresh.snapshot(at + 100))
            assertEquals(original.copy(observedElapsedMillis = at + 100), restored)
            expectedObservation = restored.observedElapsedMillis
        }
    }

    @Test
    fun lastObservedPropertyAndLatchedWindowAreSeparateAcrossRestart() {
        val file = file()
        seeded(file)
        val beforeUpdate = SleepDeadlineTracker(store(file))
        beforeUpdate.read(11_000, false)
        beforeUpdate.read(16_000, false, 300) // App policy only; native OFF window stays1200.
        assertEquals(1200, beforeUpdate.snapshot(16_100)?.windowSeconds)
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(21_000, false, 300)
        val edge = checkNotNull(restarted.snapshot(21_100))
        assertEquals(1000L, edge.offLowerElapsedMillis)
        assertEquals(6100L, edge.offUpperElapsedMillis)
        assertEquals(1200, edge.windowSeconds)
        assertEquals(300, store(file).read()?.observedWindowSeconds)
    }

    @Test
    fun onSamplesAreNeverPersistedAndCannotSeedAnEdgeAcrossAnAppRestart() {
        val file = file()
        SleepDeadlineTracker(store(file)).read(1000, true, 300)
        assertNull(store(file).read())
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(6000, false, 300)
        assertNull(restarted.snapshot(6100)?.offLowerElapsedMillis)
    }

    @Test
    fun exactFifteenSecondCompletedObservationGapIsAcceptedButOneMillisecondMoreIsNot() {
        for (gap in listOf(15_000L, 15_001L)) {
            val file = file("gap-$gap.json")
            seeded(file)
            val restarted = SleepDeadlineTracker(store(file))
            restarted.read(6000 + gap, false)
            assertEquals(gap == 15_000L, restarted.snapshot(6100 + gap)?.windowSeconds != null)
        }
    }

    @Test
    fun mismatchedNativePropertyCannotRestoreOldEdgeOrReviveAfterLaterPropertyReversion() {
        val file = file()
        seeded(file)
        val changed = SleepDeadlineTracker(store(file))
        changed.read(11_000, false, 300)
        assertNull(changed.snapshot(11_100)?.windowSeconds)
        val reverted = SleepDeadlineTracker(store(file))
        reverted.read(16_000, false, 1200)
        assertNull(reverted.snapshot(16_100)?.windowSeconds)
    }

    @Test
    fun freshOnInvalidatesSavedOffBeforeAcceptingANewRealCycle() {
        val file = file()
        seeded(file)
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(11_000, true)
        assertTrue(checkNotNull(restarted.snapshot(11_100)).ignitionOn)
        assertNull(restarted.snapshot(11_100)?.offLowerElapsedMillis)
        val afterAnotherRestart = SleepDeadlineTracker(store(file))
        afterAnotherRestart.read(16_000, false)
        assertNull(afterAnotherRestart.snapshot(16_100)?.offLowerElapsedMillis)
        afterAnotherRestart.read(21_000, true)
        afterAnotherRestart.read(26_000, false)
        assertEquals(21_000L, afterAnotherRestart.snapshot(26_100)?.offLowerElapsedMillis)
        assertEquals(26_100L, afterAnotherRestart.snapshot(26_100)?.offUpperElapsedMillis)
    }

    @Test
    fun rebootChangedIdentityMissingKernelUuidAndElapsedRollbackCannotRestore() {
        val cases = listOf(
            boot.copy(bootId = "11234567-1234-1234-1234-012345678901") to 11_000L,
            boot.copy(bootCount = 13) to 11_000L,
            boot.copy(bootCount = null) to 11_000L,
            SleepBootIdentity(null, 12) to 11_000L,
            SleepBootIdentity(null, null) to 11_000L,
            boot to 5000L,
        )
        for ((index, value) in cases.withIndex()) {
            val file = file("identity-$index.json")
            seeded(file)
            val restarted = SleepDeadlineTracker(store(file))
            restarted.read(value.second, false, identity = value.first)
            assertNull(restarted.snapshot(value.second + 100)?.offLowerElapsedMillis)
        }
    }

    @Test
    fun bootCountOnlyEvidenceRemainsLiveButIsNeverTrustedAsDurableAcrossReboot() {
        val file = file()
        val live = SleepDeadlineTracker(store(file))
        val countOnly = SleepBootIdentity(null, 12)
        live.read(1000, true, identity = countOnly)
        live.read(6000, false, identity = countOnly)
        assertEquals(1000L, live.snapshot(6100)?.offLowerElapsedMillis)
        assertNull(store(file).read())
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(11_000, false, identity = countOnly)
        assertNull(restarted.snapshot(11_100)?.offLowerElapsedMillis)
    }

    @Test
    fun unknownAccUnknownWindowSlowCaptureAndInvalidTimesCommitAnInvalidation() {
        for (scenario in 0..4) {
            val file = file("invalid-$scenario.json")
            seeded(file)
            val tracker = SleepDeadlineTracker(store(file))
            when (scenario) {
                0 -> tracker.read(11_000, null)
                1 -> tracker.read(11_000, false, null)
                2 -> tracker.observe(boot, 11_000, 17_001, false, 1200)
                3 -> tracker.observe(boot, 11_000, 10_999, false, 1200)
                else -> tracker.observe(boot, -1, 0, false, 1200)
            }
            assertNull(store(file).read())
            val restarted = SleepDeadlineTracker(store(file))
            restarted.read(18_000, false)
            assertNull(restarted.snapshot(18_100)?.offLowerElapsedMillis)
        }
    }

    @Test
    fun staleSnapshotInvalidatesDiskSoRestartCannotResurrectPriorProof() {
        val file = file()
        val tracker = SleepDeadlineTracker(store(file))
        tracker.read(1000, true)
        tracker.read(6000, false)
        assertNull(tracker.snapshot(21_101))
        tracker.flushPendingInvalidation() // Existing controller IO loop drains queued revocation.
        assertNull(store(file).read())
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(22_000, false)
        assertNull(restarted.snapshot(22_100)?.windowSeconds)
    }

    @Test
    fun expiredOriginalDeadlineCannotBeRenewedByRestartEvenWithFreshCheckpoint() {
        val file = file()
        val tracker = SleepDeadlineTracker(store(file))
        tracker.read(1000, true, 20)
        tracker.read(6000, false, 20)
        tracker.read(16_000, false, 20)
        assertEquals(20, tracker.snapshot(16_100)?.windowSeconds)
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(21_000, false, 20)
        assertNull(restarted.snapshot(21_100)?.windowSeconds)
        assertNull(store(file).read()?.windowSeconds)
    }

    @Test
    fun corruptTruncatedOversizedAndAppendedRecordsAreRejectedWithoutExternalFallback() {
        val file = file()
        seeded(file)
        val good = file.readText()
        val invalid = listOf("", "{", good.take(good.length / 2), "x".repeat(2050),
            good + " trailing", good.replace("1200", "3200"))
        for ((index, text) in invalid.withIndex()) {
            val corrupt = file("corrupt-$index.json")
            corrupt.writeText(text)
            val restarted = SleepDeadlineTracker(store(corrupt))
            restarted.read(11_000, false)
            assertNull(restarted.snapshot(11_100)?.offLowerElapsedMillis)
            assertNull(store(corrupt).read()?.windowSeconds)
        }
    }

    @Test
    fun checksumCorrectButInvalidTypedOrLogicalPayloadCannotBecomeEvidence() {
        val file = file()
        seeded(file)
        val original = JSONObject(file.readText()).getString("payload")
        val payloads = listOf(
            JSONObject(original).put("schema_version", 2),
            JSONObject(original).put("finished_elapsed_ms", "6100"),
            JSONObject(original).put("finished_elapsed_ms", 6100.5),
            JSONObject(original).put("started_elapsed_ms", 6101),
            JSONObject(original).put("off_upper_elapsed_ms", 6101),
            JSONObject(original).put("off_lower_elapsed_ms", 6200),
            JSONObject(original).put("window_s", 3601),
            JSONObject(original).put("window_s", JSONObject.NULL),
            JSONObject(original).put("off_lower_elapsed_ms", JSONObject.NULL)
                .put("off_upper_elapsed_ms", JSONObject.NULL).put("window_s", JSONObject.NULL),
            JSONObject(original).put("observed_window_s", 0),
            JSONObject(original).put("ignition_on", "false"),
            JSONObject(original).put("ignition_on", true),
            JSONObject(original).put("boot_id", JSONObject.NULL),
            JSONObject(original).put("boot_count", -1),
            JSONObject(original).put("unexpected", 1),
        )
        for ((index, payload) in payloads.withIndex()) {
            val bad = file("logical-$index.json")
            val value = payload.toString()
            val checksum = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
                .joinToString("") { "%02x".format(it) }
            bad.writeText(JSONObject().put("payload", value).put("sha256", checksum).toString())
            assertNull("invalid case$index", store(bad).read())
        }
    }

    @Test
    fun interruptedAtomicWriteRetainsOnlyCommittedEdgeAndCommittedTombstoneNeverResurrects() {
        val file = file()
        val original = seeded(file)
        File(file.path + ".new").writeText("{broken") // Process death before atomic replacement.
        val restarted = SleepDeadlineTracker(store(file))
        restarted.read(11_000, false)
        assertEquals(original.copy(observedElapsedMillis = 11_100), restarted.snapshot(11_100))

        restarted.read(16_000, null) // Committed invalidation must supersede the old edge.
        File(file.path + ".new").writeText("{broken again")
        val afterSecondCrash = SleepDeadlineTracker(store(file))
        afterSecondCrash.read(18_000, false)
        assertNull(afterSecondCrash.snapshot(18_100)?.offLowerElapsedMillis)
    }

    @Test
    fun unavailableCheckpointIoCannotCrashPollingOrInventAnEdge() {
        val broken = object : SleepDeadlineCheckpointStore {
            override fun read(): SleepDeadlineCheckpoint? = error("disk unavailable")
            override fun write(checkpoint: SleepDeadlineCheckpoint?) { error("disk unavailable") }
        }
        val tracker = SleepDeadlineTracker(broken)
        tracker.read(1000, false)
        assertNull(tracker.snapshot(1100)?.windowSeconds)
        tracker.read(6000, true)
        tracker.read(11_000, false)
        assertNull(tracker.snapshot(11_100))
        tracker.read(16_000, null)
        assertNull(tracker.snapshot(16_100))
    }

    @Test
    fun failedHeartbeatRevokesThePreviouslyReadableCheckpointAndClearsRestoredEvidence() {
        val file = file()
        seeded(file)
        val committed = store(file)
        var refused = false
        val failingHeartbeat = object : SleepDeadlineCheckpointStore {
            override fun read(): SleepDeadlineCheckpoint? = committed.read()
            override fun write(checkpoint: SleepDeadlineCheckpoint?) {
                if (checkpoint != null && !refused) {
                    refused = true
                    assertTrue(committed.read() != null) // Atomic rollback left an older readable edge.
                    error("simulated heartbeat commit failure")
                }
                committed.write(checkpoint)
            }
        }
        val restarted = SleepDeadlineTracker(failingHeartbeat)
        restarted.read(11_000, false)
        assertTrue(refused)
        assertNull(restarted.snapshot(11_100))
        assertNull(committed.read())
        val nextProcess = SleepDeadlineTracker(store(file))
        nextProcess.read(16_000, false)
        assertNull(nextProcess.snapshot(16_100)?.offLowerElapsedMillis)
    }

    @Test
    fun snapshotDoesNotWaitForFsyncAndNewerInvalidationWinsAnInFlightHeartbeat() {
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val saved = AtomicReference<SleepDeadlineCheckpoint?>(null)
        val blocking = object : SleepDeadlineCheckpointStore {
            override fun read(): SleepDeadlineCheckpoint? = saved.get()
            override fun write(checkpoint: SleepDeadlineCheckpoint?) {
                if (checkpoint != null) {
                    started.countDown()
                    check(release.await(5, TimeUnit.SECONDS))
                }
                saved.set(checkpoint)
            }
        }
        val tracker = SleepDeadlineTracker(blocking)
        tracker.read(1000, true)
        val pool = Executors.newFixedThreadPool(3)
        try {
            val writing = pool.submit { tracker.read(6000, false) }
            assertTrue(started.await(3, TimeUnit.SECONDS))
            val duringWrite = pool.submit<SleepDeadlineEvidence?> { tracker.snapshot(6100) }
            assertEquals(1000L, duringWrite.get(1, TimeUnit.SECONDS)?.offLowerElapsedMillis)
            val invalidating = pool.submit { tracker.read(11_000, null) }
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(1)
            while (tracker.snapshot(11_100) != null && System.nanoTime() < deadline) Thread.yield()
            assertNull(tracker.snapshot(11_100))
            release.countDown()
            writing.get(3, TimeUnit.SECONDS)
            invalidating.get(3, TimeUnit.SECONDS)
            assertNull(saved.get())
            val restarted = SleepDeadlineTracker(blocking)
            restarted.read(16_000, false)
            assertNull(restarted.snapshot(16_100)?.windowSeconds)
        } finally {
            release.countDown()
            pool.shutdownNow()
        }
    }

    @Test
    fun staleFirstSnapshotReturnsImmediatelyWhileFsyncIsBlockedAndIoLaterRevokesDisk() {
        val initial = SleepDeadlineCheckpoint(boot, 6000, 6100, false, 1200, 1000, 6100, 1200)
        val saved = AtomicReference<SleepDeadlineCheckpoint?>(initial)
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val blocking = object : SleepDeadlineCheckpointStore {
            override fun read(): SleepDeadlineCheckpoint? = saved.get()
            override fun write(checkpoint: SleepDeadlineCheckpoint?) {
                if (checkpoint != null) {
                    started.countDown()
                    check(release.await(5, TimeUnit.SECONDS))
                }
                saved.set(checkpoint)
            }
        }
        val tracker = SleepDeadlineTracker(blocking)
        val pool = Executors.newFixedThreadPool(2)
        try {
            val writing = pool.submit { tracker.read(11_000, false) }
            assertTrue(started.await(3, TimeUnit.SECONDS))
            val stale = pool.submit<SleepDeadlineEvidence?> { tracker.snapshot(26_101) }
            assertNull(stale.get(1, TimeUnit.SECONDS))
            assertEquals(initial, saved.get())
            release.countDown()
            writing.get(3, TimeUnit.SECONDS)
            tracker.flushPendingInvalidation()
            assertNull(saved.get())
            val restarted = SleepDeadlineTracker(blocking)
            restarted.read(27_000, false)
            assertNull(restarted.snapshot(27_100)?.offLowerElapsedMillis)
        } finally {
            release.countDown()
            pool.shutdownNow()
        }
    }

    @Test
    fun repeatedDrivingAndUnprovenOffSamplesDoNotFsyncOnEveryPoll() {
        val writes = mutableListOf<SleepDeadlineCheckpoint?>()
        val recording = object : SleepDeadlineCheckpointStore {
            override fun read(): SleepDeadlineCheckpoint? = null
            override fun write(checkpoint: SleepDeadlineCheckpoint?) { writes += checkpoint }
        }
        val tracker = SleepDeadlineTracker(recording)
        repeat(5) { tracker.read(1000L + it * 5000, true) }
        assertEquals(listOf<SleepDeadlineCheckpoint?>(null), writes)
        tracker.read(26_000, false)
        assertEquals(2, writes.size)
        tracker.read(31_000, true)
        repeat(5) { tracker.read(36_000L + it * 5000, true) }
        assertEquals(3, writes.size)
        assertNull(writes.last())
    }

    @Test
    fun controllerRestoresPrivateProofBeforeAnyStartupPolicyWrite() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val file = file()
        var on: Boolean? = true
        var seconds = 1200
        val operations = mutableListOf<String>()
        val property = object : SleepWindowPropertyAccessor {
            override fun readSeconds(): Int = seconds.also { operations += "read:$it" }
            override fun writeSeconds(value: Int): Boolean {
                operations += "write:$value"
                seconds = value
                return true
            }
        }
        fun controller() = AdaptiveSleepWindowController(
            context, this, property,
            object : AccStateAccessor { override fun readAccOn(): Boolean? = on },
            elapsedMillis = { testScheduler.currentTime }, bootIdentity = { boot }, deadlineStore = store(file),
        )
        val first = controller()
        first.start()
        first.setIngestionRequestActive(true)
        runCurrent()
        advanceTimeBy(5000)
        on = false
        runCurrent()
        val original = checkNotNull(first.snapshot().deadline)
        assertEquals(1200, original.windowSeconds)
        first.close()
        runCurrent()
        advanceTimeBy(2000)
        operations.clear()
        val replacement = controller()
        try {
            replacement.start()
            runCurrent()
            val restored = checkNotNull(replacement.snapshot().deadline)
            assertEquals(original.copy(observedElapsedMillis = testScheduler.currentTime), restored)
            assertTrue(operations.first().startsWith("read:"))
            assertFalse(operations.any { it.startsWith("write:") })
            on = null
            advanceTimeBy(5000)
            runCurrent()
            assertNull(replacement.snapshot().deadline)
            assertNull(store(file).read())
        } finally {
            replacement.close()
            runCurrent()
        }
    }
}
