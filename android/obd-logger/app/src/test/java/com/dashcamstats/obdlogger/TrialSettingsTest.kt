package com.dashcamstats.obdlogger

import android.content.Context
import android.os.Looper
import android.widget.Button
import android.widget.CheckBox
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.LooperMode
import java.io.File
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
class TrialSettingsTest {
    private class MemoryStorage : TrialSettingsStorage {
        var bytes: ByteArray? = null
        var failRead = false
        var failWrite = false
        var reads = 0
        var writes = 0
        override fun read(): ByteArray? { reads++; check(!failRead); return bytes?.copyOf() }
        override fun write(bytes: ByteArray) { writes++; check(!failWrite); this.bytes = bytes.copyOf() }
    }

    private class Fixture(present: Boolean = false, enabled: Boolean = false, reason: String? = null) {
        val storage = MemoryStorage()
        var legacy = LegacyTrialSetting(present, enabled, reason)
        var migrated: Int? = null
        var barrier: TrialSettingChange? = null
        var failMirror = false
        var failMarker = false
        var failBarrier = false
        var at: String? = "2026-10-25T04:00:00Z"
        fun repository(actualStorage: TrialSettingsStorage = storage) = TrialSettingsRepository(actualStorage, { legacy }, { migrated }, { revision ->
            if (failMarker) false else { if (migrated == null) migrated = revision; true }
        }, { state ->
            if (failMirror) false else {
                migrated = state.revision; legacy = LegacyTrialSetting(true, state.enabled, state.fallbackReason); true
            }
        }, { barrier }, { state -> if (failBarrier) false else { barrier = state; true } }, { at })

        fun editRecord(edit: (JSONObject) -> Unit) {
            val envelope = JSONObject(checkNotNull(storage.bytes).toString(Charsets.UTF_8))
            val body = JSONObject(envelope.getString("payload")); edit(body)
            val payload = body.toString()
            envelope.put("payload", payload).put("sha256", MessageDigest.getInstance("SHA-256")
                .digest(payload.toByteArray()).joinToString("") { "%02x".format(it) })
            storage.bytes = envelope.toString().toByteArray()
        }
    }

    @Test fun missingLegacyOptInMigratesAsBaselineWithUnknownOriginalChangeTime() {
        val f = Fixture(); val s = f.repository().snapshot()
        assertFalse(s.enabled); assertFalse(s.legacyKeyPresent)
        assertEquals("legacy_migration", s.source); assertNull(s.changedAt)
        assertEquals("ready", s.storageState); assertNull(s.error); assertEquals(1, s.revision)
    }

    @Test fun validLegacyOptInAndNamedFallbackEachSurviveNewProcessAndGeneralPreferenceLoss() {
        for (reason in listOf(null, "transport_fault")) {
            val f = Fixture(true, true, reason)
            val first = f.repository().snapshot()
            assertEquals(reason == null, first.enabled); assertEquals(reason, first.fallbackReason)
            f.legacy = LegacyTrialSetting(false, false, null)
            val restart = f.repository().snapshot()
            assertEquals(first.enabled, restart.enabled); assertEquals(first.fallbackReason, restart.fallbackReason)
            assertEquals(first.revision, restart.revision)
            assertEquals(first.recentChanges, restart.recentChanges)
        }
    }

    @Test fun aWarmProcessDoesNoRepeatedDiskReadsOrPreferenceWritesDuringPolling() {
        val f = Fixture(true, true); val repo = f.repository(); val original = repo.snapshot()
        val reads = f.storage.reads; val writes = f.storage.writes
        f.storage.failRead = true
        repeat(1_000) { assertSame(original, repo.snapshot()) }
        assertEquals(reads, f.storage.reads); assertEquals(writes, f.storage.writes)
    }

    @Test fun explicitChangesHaveBoundedAuditAndPersistAcrossRestartAndUpdate() {
        val f = Fixture(); val repo = f.repository()
        val old = repo.snapshot(); val armed = repo.change(true, null, old)
        assertTrue(armed.enabled); assertEquals("operator", armed.source)
        assertEquals(f.at, armed.changedAt); assertEquals(2, armed.revision)
        val restart = f.repository().snapshot(); assertEquals(armed.json().toString(), restart.json().toString())
        val blocked = repo.change(false, "critical_readings_missing")
        assertFalse(blocked.enabled); assertEquals("health", blocked.source)
        assertEquals("critical_readings_missing", f.repository().snapshot().fallbackReason)
    }

    @Test fun aSettingsRenderAndCheckboxEditCannotPersistWithoutAnExplicitApply() {
        val f = Fixture(); val repo = f.repository(); val draft = TrialSettingsDraft(repo.snapshot())
        val writes = f.storage.writes
        draft.select(true)
        repeat(5) { draft.refresh(repo.snapshot()) }
        assertTrue(draft.selected); assertTrue(draft.dirty)
        assertFalse(repo.snapshot().enabled); assertEquals(writes, f.storage.writes)
        assertTrue(draft.apply(repo).enabled)
        assertTrue(f.repository().snapshot().enabled)
        assertFalse(draft.dirty)
    }

    @Test fun staleFormCannotClearNewHealthFallbackUntilOperatorReviewsAndAppliesAgain() {
        val f = Fixture(); val repo = f.repository(); val draft = TrialSettingsDraft(repo.snapshot())
        draft.select(true)
        repo.change(false, "transport_fault")
        draft.refresh(repo.snapshot())
        assertThrows(StaleTrialSettingsException::class.java) { draft.apply(repo) }
        assertFalse(repo.snapshot().enabled); assertEquals("transport_fault", repo.snapshot().fallbackReason)
        draft.reload(repo.snapshot()); draft.select(true)
        assertTrue(draft.apply(repo).enabled); assertNull(repo.snapshot().fallbackReason)
    }

    @Test fun clickingApplyOnAnAlreadyDisabledFallbackDoesNotEraseItsReason() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot()
        val blocked = repo.change(false, "sustained_overrun")
        val same = repo.change(false, null, blocked)
        assertEquals(blocked.revision, same.revision)
        assertEquals("sustained_overrun", same.fallbackReason)
    }

    @Test fun operatorWriteFailureIsVisibleAndDoesNotReplaceThePreviousGoodBaseline() {
        val f = Fixture(); val repo = f.repository(); val previous = repo.snapshot()
        val bytes = checkNotNull(f.storage.bytes).copyOf(); f.storage.failWrite = true
        assertThrows(TrialSettingsStorageException::class.java) { repo.change(true, null, previous) }
        val failed = repo.snapshot()
        assertFalse(failed.enabled); assertEquals("unavailable", failed.storageState); assertEquals("write_failed", failed.error)
        assertArrayEquals(bytes, f.storage.bytes)
        assertFalse(f.repository().snapshot().enabled)
    }

    @Test fun failedFallbackPrimaryWriteCannotReviveThePreviousArmedStateAfterRestart() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot(); repo.change(true, null)
        val armedBytes = checkNotNull(f.storage.bytes).copyOf(); f.storage.failWrite = true
        assertThrows(TrialSettingsStorageException::class.java) { repo.change(false, "transport_fault") }
        assertEquals("transport_fault", repo.snapshot().fallbackReason)
        assertFalse(repo.snapshot().enabled); assertArrayEquals(armedBytes, f.storage.bytes)
        val unavailableAfterRestart = f.repository().snapshot()
        assertFalse(unavailableAfterRestart.enabled)
        assertEquals("transport_fault", unavailableAfterRestart.fallbackReason)
        assertEquals("write_failed", unavailableAfterRestart.error)
        f.storage.failWrite = false
        val recovered = f.repository().snapshot()
        assertFalse(recovered.enabled); assertEquals("transport_fault", recovered.fallbackReason)
        assertEquals("ready", recovered.storageState); assertNull(f.barrier)
    }

    @Test fun aMalformedSafetyBarrierCannotBeUsedToArmAFormerBaseline() {
        val f = Fixture(); f.repository().snapshot()
        f.barrier = TrialSettingChange(2, true, null, "operator", f.at)
        val blocked = f.repository().snapshot()
        assertFalse(blocked.enabled); assertEquals("read_failed", blocked.error)
        assertEquals("unavailable", blocked.storageState)
    }

    @Test fun totalStorageFailureKeepsTheExistingSessionLatchClosedUntilAFallbackCanBeSaved() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot(); repo.change(true, null)
        val latch = PollTrialFallbackLatch()
        f.failBarrier = true; f.storage.failWrite = true
        assertThrows(TrialSettingsStorageException::class.java) {
            latch.disable("transport_fault") { repo.change(false, it) }
        }
        assertFalse(repo.snapshot().enabled); assertEquals("unavailable", repo.snapshot().storageState)
        var connections = 0
        assertThrows(TrialSettingsStorageException::class.java) {
            latch.beforeConnection { repo.change(false, it) }; connections++
        }
        assertEquals(0, connections)
        f.failBarrier = false; f.storage.failWrite = false
        latch.beforeConnection { repo.change(false, it) }; connections++
        assertEquals(1, connections)
        assertFalse(f.repository().snapshot().enabled)
        assertEquals("transport_fault", f.repository().snapshot().fallbackReason)
    }

    @Test fun compatibilityMirrorFailureCannotRollbackDurableIntentOrMaskFallbackReason() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot(); f.failMirror = true
        val armed = repo.change(true, null)
        assertTrue(armed.enabled); assertEquals("ready", armed.storageState); assertEquals("mirror_failed", armed.error)
        assertTrue(f.repository().snapshot().enabled)
        val blocked = repo.change(false, "required_pids_unavailable")
        assertFalse(blocked.enabled); assertEquals("required_pids_unavailable", blocked.fallbackReason)
        assertEquals("mirror_failed", blocked.error)
        f.failMirror = false
        assertEquals("required_pids_unavailable", f.repository().snapshot().fallbackReason)
    }

    @Test fun coldRestartRejectsAnOlderValidArmedFileAfterANewerFallbackWasCommitted() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot(); repo.change(true, null)
        val armed = checkNotNull(f.storage.bytes).copyOf()
        val off = repo.change(false, "transport_fault")
        assertNull(f.barrier); assertEquals(off.revision, f.migrated)
        val mirror = f.legacy; val writes = f.storage.writes
        f.storage.bytes = armed // Model a lost directory replacement while newer preferences survived.
        val cold = f.repository(); val rejected = cold.snapshot()
        assertFalse(rejected.enabled); assertEquals("unavailable", rejected.storageState)
        assertEquals("corrupt_record", rejected.error)
        assertEquals(off.revision, f.migrated); assertEquals(mirror, f.legacy)
        assertEquals(writes, f.storage.writes)
        val repaired = cold.change(true, null, rejected) // A new operator choice may repair it.
        assertTrue(repaired.enabled); assertEquals(off.revision + 1, repaired.revision)
        assertEquals(repaired.revision, f.migrated)
    }

    @Test fun anOlderMirrorAnchorDoesNotDiscardANewerValidPrimaryDecision() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot()
        f.failMirror = true
        val armed = repo.change(true, null)
        assertTrue(checkNotNull(f.migrated) < armed.revision)
        val fresh = f.repository().snapshot()
        assertTrue(fresh.enabled); assertEquals(armed.revision, fresh.revision)
        assertEquals("mirror_failed", fresh.error)
        f.failMirror = false
        val synchronized = f.repository().snapshot()
        assertTrue(synchronized.enabled); assertNull(synchronized.error)
        assertEquals(armed.revision, f.migrated)
    }

    @Test fun anOperatorOffBarrierSurvivesMirrorFailureAndAnOlderArmedPrimaryRollback() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot(); repo.change(true, null)
        val armed = checkNotNull(f.storage.bytes).copyOf()
        f.failMirror = true
        val off = repo.change(false, null)
        assertFalse(off.enabled); assertEquals("mirror_failed", off.error)
        assertNotNull(f.barrier); assertEquals("operator", f.barrier?.source)
        f.storage.bytes = armed
        val cold = f.repository().snapshot()
        assertFalse(cold.enabled); assertEquals("operator", cold.source)
        assertEquals(off.revision, cold.revision)
        assertNotNull(f.barrier) // Do not remove the only surviving newer OFF evidence.
        f.failMirror = false
        assertFalse(f.repository().snapshot().enabled)
        assertNull(f.barrier)
    }

    @Test fun theLastRevisionSlotRemainsAvailableForAStickyHealthFallback() {
        val f = Fixture(); f.repository().snapshot()
        f.editRecord { body ->
            body.getJSONObject("state").put("revision", Int.MAX_VALUE - 1).put("enabled", true)
            body.getJSONArray("recent_changes").getJSONObject(0).put("revision", Int.MAX_VALUE - 1).put("enabled", true)
        }
        f.migrated = Int.MAX_VALUE - 1
        val repo = f.repository(); assertTrue(repo.snapshot().enabled)
        val off = repo.change(false, "transport_fault")
        assertFalse(off.enabled); assertEquals(Int.MAX_VALUE, off.revision)
        val fresh = f.repository().snapshot()
        assertFalse(fresh.enabled); assertEquals("transport_fault", fresh.fallbackReason)
        assertThrows(TrialSettingsStorageException::class.java) { repo.change(true, null, off) }
        assertFalse(repo.snapshot().enabled); assertEquals("revision_exhausted", repo.snapshot().error)
    }

    @Test fun aMissingAuthoritativeFileAfterMigrationNeverReimportsOldEnabledPreferences() {
        val f = Fixture(true, true); f.repository().snapshot()
        f.storage.bytes = null
        val writes = f.storage.writes
        val missing = f.repository().snapshot()
        assertFalse(missing.enabled); assertEquals("missing_record", missing.error)
        assertEquals("unknown", missing.source); assertNull(missing.changedAt)
        assertEquals(writes, f.storage.writes)
    }

    @Test fun firstMigrationWriteFailureMarksTheBoundaryBeforeAColdRestart() {
        val f = Fixture(true, true); f.storage.failWrite = true
        assertEquals("write_failed", f.repository().snapshot().error)
        assertNotNull(f.migrated)
        f.storage.failWrite = false
        val next = f.repository().snapshot()
        assertFalse(next.enabled); assertEquals("missing_record", next.error)
    }

    @Test fun invalidLegacyValueNeverLooksLikeAnOperatorDisableOrBecomesArmedLater() {
        val f = Fixture(true, true, "unbounded reason")
        val invalid = f.repository().snapshot()
        assertFalse(invalid.enabled); assertEquals("legacy_invalid", invalid.error)
        assertEquals("unknown", invalid.source); assertNull(invalid.changedAt)
        f.legacy = LegacyTrialSetting(true, true, null)
        assertEquals("missing_record", f.repository().snapshot().error)
    }

    @Test fun damagedChecksumAndOversizedRecordNeverFallBackToTheLegacyOnKey() {
        for (bytes in listOf("broken".toByteArray(), ByteArray(TrialSettingsRepository.MAX_BYTES + 1))) {
            val f = Fixture(true, true); f.repository().snapshot(); f.storage.bytes = bytes
            val invalid = f.repository().snapshot()
            assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
            assertEquals("unknown", invalid.source); assertNull(invalid.changedAt)
        }
    }

    @Test fun trailingOuterDataCannotLeaveAnOtherwiseValidArmedRecordEnabled() {
        val f = Fixture(true, true); f.repository().snapshot()
        f.storage.bytes = checkNotNull(f.storage.bytes) + " trailing".toByteArray()
        val invalid = f.repository().snapshot()
        assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
    }

    @Test fun trailingPayloadWithARecomputedChecksumCannotAuthorizeAnArmedRecord() {
        val f = Fixture(true, true); f.repository().snapshot()
        val envelope = JSONObject(checkNotNull(f.storage.bytes).toString(Charsets.UTF_8))
        val payload = envelope.getString("payload") + " trailing"
        val digest = MessageDigest.getInstance("SHA-256").digest(payload.toByteArray())
            .joinToString("") { "%02x".format(it) }
        f.storage.bytes = envelope.put("payload", payload).put("sha256", digest).toString().toByteArray()
        val invalid = f.repository().snapshot()
        assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
    }

    @Test fun objectPayloadCannotBeCoercedIntoAValidArmedStringPayload() {
        val f = Fixture(true, true); f.repository().snapshot()
        val envelope = JSONObject(checkNotNull(f.storage.bytes).toString(Charsets.UTF_8))
        envelope.put("payload", JSONObject(envelope.getString("payload")))
        f.storage.bytes = envelope.toString().toByteArray()
        val invalid = f.repository().snapshot()
        assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
    }

    @Test fun invalidUtf8CannotBeReplacedOrIgnoredAfterAValidArmedRecord() {
        val f = Fixture(true, true); f.repository().snapshot()
        f.storage.bytes = checkNotNull(f.storage.bytes) + byteArrayOf(0xc0.toByte(), 0xaf.toByte())
        val invalid = f.repository().snapshot()
        assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
    }

    @Test fun malformedBarrierFramingIsUnavailableRatherThanANamedHealthDecision() {
        val valid = TrialSettingChange(2, false, "transport_fault", "health", "2026-10-25T04:00:00Z")
        assertEquals(valid, TrialSettingsRepository.decodeBarrier(valid.json().toString()))
        assertThrows(Exception::class.java) { TrialSettingsRepository.decodeBarrier(valid.json().toString() + " trailing") }
    }

    @Test fun realSyncedFileReplacementSurvivesReopenAndNeverPromotesAnUnfinishedArmedFile() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val file = File(context.noBackupFilesDir, "trial-real-roundtrip.json")
        val pending = File(file.path + ".new")
        file.delete(); pending.delete()
        try {
            val f = Fixture(); val disk = AtomicTrialSettingsStorage(file); val repo = f.repository(disk)
            assertFalse(repo.snapshot().enabled)
            assertTrue(repo.change(true, null).enabled)
            val armed = file.readBytes()
            assertTrue(f.repository(AtomicTrialSettingsStorage(file)).snapshot().enabled)
            repo.change(false, null)
            pending.writeBytes(armed)
            assertFalse(f.repository(AtomicTrialSettingsStorage(file)).snapshot().enabled)
            file.delete()
            val missing = f.repository(AtomicTrialSettingsStorage(file)).snapshot()
            assertFalse(missing.enabled); assertEquals("missing_record", missing.error)
        } finally { file.delete(); pending.delete() }
    }

    @Test fun aRealPendingWriteFailurePreservesThePreviousCommittedRecord() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val file = File(context.noBackupFilesDir, "trial-real-failure.json")
        val pending = File(file.path + ".new")
        file.delete(); pending.delete()
        try {
            val f = Fixture(); val repo = f.repository(AtomicTrialSettingsStorage(file))
            val before = repo.snapshot(); val committed = file.readBytes()
            assertTrue(pending.mkdir()) // A directory cannot be opened as the pending data file.
            assertThrows(TrialSettingsStorageException::class.java) { repo.change(true, null, before) }
            assertArrayEquals(committed, file.readBytes())
            assertFalse(f.repository(AtomicTrialSettingsStorage(file)).snapshot().enabled)
        } finally { file.delete(); pending.delete() }
    }

    @Test fun validChecksumDoesNotMakeMalformedStateOrAnInconsistentHistoryTrustworthy() {
        val mutations: List<(JSONObject) -> Unit> = listOf(
            { it.put("schema_version", 2) },
            { it.getJSONObject("state").put("enabled", "true") },
            { it.getJSONObject("state").put("revision", 2_147_483_648L) },
            { it.getJSONObject("state").put("fallback_reason", "raw transport text") },
            { it.getJSONObject("state").put("source", "reboot_guessed") },
            { it.getJSONObject("state").put("changed_at_utc", "not a date") },
            { it.getJSONArray("recent_changes").put(it.getJSONObject("state")) },
            { it.put("unknown", true) },
        )
        for (mutation in mutations) {
            val f = Fixture(true, true); f.repository().snapshot(); f.editRecord(mutation)
            val invalid = f.repository().snapshot()
            assertFalse(invalid.enabled); assertEquals("corrupt_record", invalid.error)
        }
    }

    @Test fun readFailureIsExplicitAndOnlyAnExplicitRepairRetriesTheDiskInThatProcess() {
        val f = Fixture(true, true); f.repository().snapshot(); f.storage.failRead = true
        val repo = f.repository(); val failed = repo.snapshot()
        assertFalse(failed.enabled); assertEquals("read_failed", failed.error); assertNull(failed.changedAt)
        val count = f.storage.reads; f.storage.failRead = false
        assertSame(failed, repo.snapshot()); assertEquals(count, f.storage.reads)
        assertThrows(StaleTrialSettingsException::class.java) { repo.change(true, null, failed) }
        assertTrue(repo.snapshot().enabled) // Validated old intent is now available for operator review.
    }

    @Test fun anExplicitChoiceCanRecoverAMissingRecordButNeverByRenderingIt() {
        val f = Fixture(true, true); f.repository().snapshot(); f.storage.bytes = null
        val repo = f.repository(); val missing = repo.snapshot()
        val draft = TrialSettingsDraft(missing)
        draft.refresh(missing)
        assertNull(f.storage.bytes); assertFalse(repo.snapshot().enabled)
        draft.select(true)
        val recovered = draft.apply(repo)
        assertTrue(recovered.enabled); assertEquals("operator", recovered.source); assertTrue(recovered.revision > 1)
    }

    @Test fun auditIsBoundedAndClockFailureIsNotInventedAsARealTimestamp() {
        val f = Fixture(); val repo = f.repository(); repo.snapshot()
        repeat(24) { repo.change(it % 2 == 0, null) }
        val last = repo.snapshot()
        assertEquals(TrialSettingsRepository.HISTORY_LIMIT, last.recentChanges.size)
        assertTrue(checkNotNull(f.storage.bytes).size <= TrialSettingsRepository.MAX_BYTES)
        assertTrue(last.recentChanges.zipWithNext().all { (a,b) -> a.revision < b.revision })
        f.at = "untrusted clock"; val next = repo.change(true, null)
        assertNull(next.changedAt); assertEquals("operator", next.source)
    }

    @Test fun revisionExhaustionCannotWrapAroundOrArmTheTrial() {
        val f = Fixture(); f.repository().snapshot()
        f.editRecord { body ->
            body.getJSONObject("state").put("revision", Int.MAX_VALUE)
            body.getJSONArray("recent_changes").getJSONObject(0).put("revision", Int.MAX_VALUE)
        }
        f.migrated = Int.MAX_VALUE
        val repo = f.repository(); val last = repo.snapshot()
        assertEquals(Int.MAX_VALUE, last.revision)
        assertThrows(TrialSettingsStorageException::class.java) { repo.change(true, null, last) }
        assertFalse(repo.snapshot().enabled); assertEquals("revision_exhausted", repo.snapshot().error)
    }

    @Test @LooperMode(LooperMode.Mode.PAUSED)
    fun actualActivityCheckboxEventsNeedTheDedicatedApplyButton() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE).edit().clear().commit()
        // Isolate this integration fixture from the process-persistent store used by other cases.
        TrialSettings::class.java.getDeclaredField("repositories").apply { isAccessible = true }
            .get(TrialSettings).let { (it as MutableMap<*, *>).clear() }
        val file = File(context.noBackupFilesDir, "poll-trial-state-v1.json")
        listOf(file, File(file.path + ".bak"), File(file.path + ".new")).forEach { it.delete() }
        val controller = Robolectric.buildActivity(MainActivity::class.java).create().resume()
        val activity = controller.get()
        try {
            val checkField = MainActivity::class.java.getDeclaredField("pollTrial").apply { isAccessible = true }
            val limit = System.nanoTime() + 5_000_000_000L
            while (runCatching { checkField.get(activity) }.getOrNull() == null) {
                Shadows.shadowOf(Looper.getMainLooper()).idle()
                check(System.nanoTime() < limit)
                Thread.sleep(10)
            }
            val checkbox = checkField.get(activity) as CheckBox
            checkbox.isChecked = true // A framework/programmatic change must not persist either.
            assertFalse(LoggerPreferences.load(context, false).pollTrialEnabled)
            val apply = MainActivity::class.java.getDeclaredField("pollTrialApply").apply { isAccessible = true }
                .get(activity) as Button
            assertTrue(apply.isEnabled); apply.performClick()
            val savedLimit = System.nanoTime() + 5_000_000_000L
            while (!LoggerPreferences.load(context, false).pollTrialEnabled) {
                Shadows.shadowOf(Looper.getMainLooper()).idle()
                check(System.nanoTime() < savedLimit)
                Thread.sleep(10)
            }
            assertTrue(LoggerPreferences.load(context, false).pollTrialEnabled)
        } finally { controller.pause().stop().destroy() }
    }
}
