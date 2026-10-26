package com.dashcamstats.obdlogger

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.sync.Mutex
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.time.Instant

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
class TemperatureCaptureTest {
    @get:Rule val temporary = TemporaryFolder()
    private val epoch = Instant.parse("2026-10-26T00:00:00Z")
    private val session = "1".repeat(32)
    private fun privateRoot() = File(temporary.root, "private")
    private fun mirror() = File(temporary.root, "public")
    private fun journal(size: Int = 256, count: Int = 32, write: (File, ByteArray) -> Unit = ::atomicTemperatureWrite) =
        TemperatureJournal(privateRoot(), ::mirror, write, size, count)
    private fun TemperatureJournal.append(index: Int = 0, sessionId: String = session, elapsedMs: Long = 1_000 + index * 30_000L, value: Int? = 75_400) =
        append(sessionId, epoch.plusSeconds(index * 30L), elapsedMs, value)
    private fun manifest() = JSONObject(File(mirror(), "manifest.json").readText())

    @Test fun readsOnlyTheNamedSocSensorAndDoesNotConfuseAnotherTemperatureWithIt() {
        val sysfs = temporary.newFolder("thermal")
        val battery = File(sysfs, "thermal_zone0").apply { mkdirs() }
        File(battery, "type").writeText("battery\n")
        File(battery, "temp").writeText("35000\n")
        val soc = File(sysfs, "thermal_zone7").apply { mkdirs() }
        File(soc, "type").writeText("soc-thmzone\n")
        File(soc, "temp").writeText("75400\n")
        assertEquals(75_400, readDeviceSocMillic(sysfs))
        File(battery, "type").delete()
        File(battery, "type").mkdirs() // unreadable/malformed unrelated sysfs entry.
        assertEquals(75_400, readDeviceSocMillic(sysfs))
        File(soc, "temp").writeText("999999\n")
        assertNull(readDeviceSocMillic(sysfs))
        File(soc, "temp").writeText("na\n")
        assertNull(readDeviceSocMillic(sysfs))
        File(soc, "type").writeText("soc-thmzone" + "x".repeat(150))
        assertNull(readDeviceSocMillic(sysfs))
    }

    @Test fun serviceRestartAndRebootRetainRowsAndAllocateNewIdentitiesWithResetElapsedTime() {
        val original = journal()
        original.append(0)
        original.append(1)
        original.publish("running", epoch.plusSeconds(30))
        val restarted = journal()
        val third = restarted.append(2, sessionId = "2".repeat(32), elapsedMs = 25)
        restarted.publish("running", epoch.plusSeconds(60))
        assertEquals(3L, third.sequence)
        assertEquals(3, restarted.retainedReadings)
        assertEquals(25L, manifest().getJSONObject("latest").getLong("elapsed_realtime_ms"))
        assertEquals("2".repeat(32), manifest().getJSONObject("latest").getString("session_id"))
    }

    @Test fun boundedRotationKeepsBothPrivateAndPublicFilesAndCountsWithinRetention() {
        val store = journal(size = 2, count = 2)
        repeat(9) { index -> store.append(index); store.publish("running", epoch.plusSeconds(index * 30L)) }
        assertEquals(3, store.retainedReadings)
        assertEquals(2, manifest().getJSONArray("chunks").length())
        assertEquals(3, manifest().getInt("retained_readings"))
        assertEquals(9L, manifest().getJSONObject("latest").getLong("sequence"))
        for (directory in listOf(privateRoot(), mirror())) {
            assertEquals(2, directory.listFiles()!!.count { it.name.startsWith("samples-") })
        }
    }

    @Test fun appendingDoesNotRewriteClosedChunksAndEveryManifestDigestMatchesCommittedBytes() {
        val writes = mutableListOf<String>()
        val store = journal(size = 2, write = { file, bytes -> writes += file.absolutePath; atomicTemperatureWrite(file, bytes) })
        repeat(5) { index -> store.append(index); store.publish("running", epoch.plusSeconds(index * 30L)) }
        val closed = File(mirror(), "samples-1.json").absolutePath
        assertEquals(2, writes.count { it == closed }) // first row, then completed pair; never again.
        val chunks = manifest().getJSONArray("chunks")
        repeat(chunks.length()) { index ->
            val descriptor = chunks.getJSONObject(index)
            val bytes = File(mirror(), descriptor.getString("file")).readBytes()
            val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            assertEquals(bytes.size, descriptor.getInt("size_bytes"))
            assertEquals(digest, descriptor.getString("sha256"))
        }
    }

    @Test fun damagedActiveChunkSkipsItsReservedIdentityRangeAndDoesNotEraseGoodEarlierHistory() {
        val original = journal(size = 2)
        repeat(3) { original.append(it) }
        File(privateRoot(), "samples-3.json").writeText("truncated")
        File(privateRoot(), "sequence").writeText("broken")
        val recovered = journal(size = 2)
        val next = recovered.append(3)
        recovered.publish("running", epoch.plusSeconds(90))
        assertEquals(5L, next.sequence)
        assertEquals(3, recovered.retainedReadings)
        assertEquals(2, manifest().getJSONArray("chunks").length())
        assertFalse(File(privateRoot(), "samples-3.json").exists())
    }

    @Test fun partialWritesAndInvalidUtf8NeverBecomeMeasurementsOrReuseAReservedSequence() {
        val original = journal()
        original.append(0)
        File(privateRoot(), "samples-400.json.partial").writeText("not a committed sample")
        File(privateRoot(), "sequence").writeText("7\n")
        val recovered = journal()
        assertEquals(8L, recovered.append(1).sequence)
        assertFalse(File(privateRoot(), "samples-400.json.partial").exists())
        File(privateRoot(), "samples-1.json").writeBytes(byteArrayOf(0xc3.toByte(), 0x28))
        val corrupt = journal()
        assertEquals(257L, corrupt.append(2).sequence)
        assertEquals(1, corrupt.retainedReadings)
    }

    @Test fun recoveredReservationIsDurableBeforeCorruptChunksAreRemovedEvenWithoutANewCapture() {
        val original = journal(size = 2)
        repeat(3) { original.append(it) }
        File(privateRoot(), "samples-3.json").writeText("truncated")
        File(privateRoot(), "sequence").writeText("broken")
        val recovered = journal(size = 2)
        recovered.publish("running", epoch.plusSeconds(90)) // load, but do not append.
        assertFalse(File(privateRoot(), "samples-3.json").exists())
        assertEquals("4", File(privateRoot(), "sequence").readText().trim())
        assertEquals(5L, journal(size = 2).append(4).sequence)
    }

    @Test fun failedReservationRepairPreservesTheCorruptFilenameUntilItCanBeReservedDurably() {
        val original = journal(size = 2)
        repeat(3) { original.append(it) }
        File(privateRoot(), "samples-3.json").writeText("truncated")
        File(privateRoot(), "sequence").writeText("broken")
        val recovered = journal(size = 2, write = { file, bytes ->
            if (file.name == "sequence") throw IOException("fixture")
            atomicTemperatureWrite(file, bytes)
        })
        assertThrows(IOException::class.java) { recovered.publish("running", epoch.plusSeconds(90)) }
        assertTrue(File(privateRoot(), "samples-3.json").exists())
        assertEquals(5L, journal(size = 2).append(4).sequence)
    }

    @Test fun steadyCaptureDoesNotReadClosedMirrorPayloadsAndAChangedMountIsVerifiedAgain() {
        val payloadReads = mutableListOf<String>()
        var destination = mirror()
        val store = TemperatureJournal(privateRoot(), { destination }, chunkReadings = 2,
            read = { file, maximum ->
                if (file.parentFile == destination && file.name.startsWith("samples-")) payloadReads += file.name
                file.readBytes().also { require(it.size <= maximum) }
            })
        repeat(5) { index -> store.append(index); store.publish("running", epoch.plusSeconds(index * 30L)) }
        assertFalse(payloadReads.contains("samples-1.json"))
        val other = File(temporary.root, "other-mount").apply { mkdirs() }
        mirror().listFiles()!!.forEach { File(other, it.name).writeBytes(it.readBytes()) }
        destination = other
        store.publish("running", epoch.plusSeconds(150))
        assertTrue(payloadReads.contains("samples-1.json"))
    }

    @Test fun incompletePublicPublicationIsRetriedWithoutDuplicatingPrivateRows() {
        var fail = false
        val store = journal(write = { file, bytes ->
            if (fail && file.parentFile == mirror() && file.name == "manifest.json") throw IOException("fixture")
            atomicTemperatureWrite(file, bytes)
        })
        store.append(0)
        store.publish("running", epoch)
        store.append(1)
        fail = true
        assertThrows(IOException::class.java) { store.publish("running", epoch.plusSeconds(30)) }
        assertEquals(1, manifest().getInt("retained_readings"))
        fail = false
        store.publish("running", epoch.plusSeconds(30))
        assertEquals(2, manifest().getInt("retained_readings"))
        assertEquals(2L, manifest().getJSONObject("latest").getLong("sequence"))
    }

    @Test fun corruptPublicChunkIsRepairedFromPrivateAuthorityInsteadOfBeingTrustedBySize() {
        val store = journal()
        store.append(0)
        store.publish("running", epoch)
        val destination = File(mirror(), "samples-1.json")
        destination.writeBytes(ByteArray(destination.length().toInt()) { 32 })
        destination.setLastModified(destination.lastModified() + 5_000)
        store.publish("running", epoch)
        assertEquals(1, JSONObject(destination.readText()).getJSONArray("readings").length())
    }

    @Test fun sensorAbsenceRecordsAnExplicitUnavailableObservationRatherThanZeroDegrees() {
        val store = journal()
        store.append(0, value = null)
        store.publish("sensor_unavailable", epoch)
        assertTrue(manifest().getJSONObject("latest").isNull("soc_millic"))
        assertEquals("unavailable", manifest().getJSONObject("latest").getString("quality"))
        assertEquals("sensor_unavailable", manifest().getString("collector_state"))
    }

    @Test fun cancellationOfAnObdWorkerDoesNotCancelTheSeparateCaptureLifetime() = runTest {
        var reads = 0
        val capture = TemperatureCapture(backgroundScope, journal(), sensor = { reads++; 72_000 },
            now = { epoch.plusMillis(testScheduler.currentTime) }, elapsed = { 1_000 + testScheduler.currentTime }, ownership = Mutex())
        val unrelatedObdWorker: Job = backgroundScope.launch { kotlinx.coroutines.delay(Long.MAX_VALUE) }
        capture.start()
        capture.start() // idempotent visible/activity/service start commands.
        runCurrent()
        assertEquals(1, reads)
        unrelatedObdWorker.cancelAndJoin()
        advanceTimeBy(60_000)
        runCurrent()
        assertEquals(3, reads)
        assertEquals("running", capture.snapshot().state)
        capture.close()
        advanceTimeBy(60_000)
        runCurrent()
        assertEquals(3, reads)
    }

    @Test fun failedJournalChildIsSupervisedAndRetriedWithNoPollingOrRadioDependency() = runTest {
        var failOnce = true
        val store = journal(write = { file, bytes ->
            if (failOnce) { failOnce = false; throw IOException("private fixture path is not public") }
            atomicTemperatureWrite(file, bytes)
        })
        val capture = TemperatureCapture(backgroundScope, store, sensor = { 72_000 },
            now = { epoch.plusMillis(testScheduler.currentTime) }, elapsed = { testScheduler.currentTime }, ownership = Mutex())
        capture.start()
        runCurrent()
        assertEquals("storage_unavailable", capture.snapshot().state)
        assertEquals("journal_unavailable", capture.snapshot().error)
        assertFalse(capture.snapshot().json().toString().contains("fixture"))
        advanceTimeBy(5_000)
        runCurrent()
        assertEquals("running", capture.snapshot().state)
        assertEquals(1, capture.snapshot().retainedReadings)
        capture.close()
    }

    @Test fun sensorExceptionsAndMirrorFailuresDoNotKillCaptureAndRecoveryPublishesRetainedHistory() = runTest {
        var mounted = false
        var sensorAvailable = false
        val store = TemperatureJournal(privateRoot(), { if (mounted) mirror() else null })
        val capture = TemperatureCapture(backgroundScope, store, sensor = { if (!sensorAvailable) throw IOException("sensor") else 73_000 },
            now = { epoch.plusMillis(testScheduler.currentTime) }, elapsed = { testScheduler.currentTime }, ownership = Mutex())
        capture.start()
        runCurrent()
        assertEquals("mirror_unavailable", capture.snapshot().error)
        mounted = true
        sensorAvailable = true
        advanceTimeBy(30_000)
        runCurrent()
        assertEquals("running", capture.snapshot().state)
        assertEquals(2, manifest().getInt("retained_readings"))
        val readings = JSONObject(File(mirror(), "samples-1.json").readText()).getJSONArray("readings")
        assertTrue(readings.getJSONObject(0).isNull("soc_millic"))
        assertEquals(73_000, readings.getJSONObject(1).getInt("soc_millic"))
        capture.close()
    }

    @Test fun processOwnershipPreventsAReplacementCollectorDuplicatingAnExistingOwner() = runTest {
        val lock = Mutex()
        var firstReads = 0
        var nextReads = 0
        val original = TemperatureCapture(backgroundScope, journal(), sensor = { firstReads++; 71_000 },
            now = { epoch.plusMillis(testScheduler.currentTime) }, elapsed = { testScheduler.currentTime }, ownership = lock)
        val next = TemperatureCapture(backgroundScope, journal(), sensor = { nextReads++; 72_000 },
            now = { epoch.plusMillis(testScheduler.currentTime) }, elapsed = { testScheduler.currentTime }, ownership = lock)
        original.start()
        next.start()
        runCurrent()
        assertEquals(1, firstReads)
        assertEquals(0, nextReads)
        original.close()
        runCurrent()
        assertEquals(1, nextReads)
        assertEquals(2L, next.snapshot().latest!!.sequence)
        next.close()
    }

    @Test fun unsetWallClockIsExplicitAndLaterValidClockDoesNotDiscardTheRawObservation() = runTest {
        var correctClock = false
        val store = journal()
        val capture = TemperatureCapture(backgroundScope, store, sensor = { 72_000 },
            now = { (if (correctClock) epoch else Instant.EPOCH).plusMillis(testScheduler.currentTime) },
            elapsed = { testScheduler.currentTime }, ownership = Mutex())
        capture.start()
        runCurrent()
        assertEquals("clock_unavailable", capture.snapshot().state)
        assertEquals("clock_unavailable", capture.snapshot().error)
        assertTrue(capture.snapshot().json().isNull("last_captured_at_utc"))
        assertTrue(capture.snapshot().copy(state = "storage_unavailable", error = "mirror_unavailable").json().isNull("last_captured_at_utc"))
        assertEquals("1970-01-01T00:00:00Z", manifest().getJSONObject("latest").getString("captured_at_utc"))
        correctClock = true
        advanceTimeBy(30_000)
        runCurrent()
        assertEquals("running", capture.snapshot().state)
        assertEquals(2, capture.snapshot().retainedReadings)
        capture.close()
    }

    @Test fun statusAndManifestExposeOnlyBoundedEvidenceAndNoNativeSleepOrInternalIdentity() {
        val store = journal()
        val row = store.append(0)
        store.publish("running", epoch)
        val json = TemperatureCaptureSnapshot("running", row, 1).json()
        assertEquals(30_000L, json.getLong("interval_ms"))
        assertEquals(1, json.getInt("retained_readings"))
        assertFalse(json.has("session_id"))
        val body = manifest().toString()
        for (privateField in listOf("boot_id", "boot_count", "pid", "private", "sleep_deadline", "acc_on", "wake")) {
            assertFalse(privateField, body.contains(privateField))
        }
    }

    @Test fun actualManifestAndChunkFixtureIncludesARebootSessionAndUnavailableSensor() {
        val store = journal()
        store.append(0)
        store.append(1, value = null)
        store.append(2, sessionId = "2".repeat(32), elapsedMs = 40, value = 73_000)
        store.publish("running", epoch.plusSeconds(60))
        assertEquals(3, manifest().getInt("retained_readings"))
        System.getenv("TEMPERATURE_CONTRACT_OUTPUT")?.takeIf { it.isNotBlank() }?.let { output ->
            val directory = File(output).apply { mkdirs() }
            mirror().listFiles()!!.filter { it.name == "manifest.json" || it.name.matches(Regex("samples-[0-9]+\\.json")) }
                .forEach { File(directory, it.name).writeBytes(it.readBytes()) }
            File(directory, "capture-status.json").writeText(TemperatureCaptureSnapshot("running", store.latest, 3).json().toString())
        }
    }
}
