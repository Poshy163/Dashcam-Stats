package com.dashcamstats.obdlogger

import android.content.Context
import android.provider.Settings
import androidx.test.core.app.ApplicationProvider
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class BootDiagnosticsTest {
    @get:Rule val temporary = TemporaryFolder()
    private val epoch = Instant.parse("2026-10-25T00:00:00Z")
    private val boot = "01234567-1234-1234-1234-012345678901"
    private val otherBoot = "11234567-1234-1234-1234-012345678901"
    private val unavailableExits = BootProcessExits(false)

    private fun observation(
        id: String? = boot,
        count: Int? = 25,
        process: Int = 1,
        atSeconds: Long = 0,
        elapsed: Long = 1_000 + atSeconds * 1_000,
        version: Int = 31,
        build: String = "a".repeat(12),
        reason: BootReportedReason = boundedBootReason("reboot", "reboot"),
    ) = BootDiagnosticObservation(id, count, UUID(0, process.toLong()).toString(), elapsed,
        epoch.plusSeconds(atSeconds).toString(), version, build, reason)

    private fun file(name: String = "boot.json") = File(temporary.root, name)
    private fun store(file: File = file()) = AtomicBootDiagnosticsStore(file)
    private fun BootDiagnosticsRecorder.observe(value: BootDiagnosticObservation) = observe(value, unavailableExits)

    @Test fun firstObservationDoesNotInventARebootOrExplainTheOldReboots() {
        val snapshot = BootDiagnosticsRecorder(store()).observe(observation())
        assertEquals("ready", snapshot.storageState)
        assertEquals("first_observation", snapshot.current?.transition)
        assertNull(snapshot.current?.bootCountDelta)
        assertEquals("reboot", snapshot.current?.bootReason)
        assertEquals("unknown", snapshot.toJson().getString("cause_confidence"))
        assertEquals("unavailable", snapshot.current?.previousProcessExit)
    }

    @Test fun sameProcessServiceRestartsDoNotEraseTheUsefulStartupHistory() {
        val store = store()
        BootDiagnosticsRecorder(store).observe(observation())
        val snapshot = BootDiagnosticsRecorder(store).observe(observation(atSeconds = 30))
        assertEquals("same_process", snapshot.current?.transition)
        assertEquals(1, snapshot.history.size)
        assertEquals(epoch.toString(), store.read().record?.observation?.observedAtUtc)
    }

    @Test fun aNewProcessOnTheSameKernelBootIsNotCalledADeviceReboot() {
        val store = store()
        BootDiagnosticsRecorder(store).observe(observation())
        val snapshot = BootDiagnosticsRecorder(store).observe(observation(process = 2, atSeconds = 30))
        assertEquals("process_restart", snapshot.current?.transition)
        assertNull(snapshot.current?.bootCountDelta)
        assertEquals(2, snapshot.history.size)
    }

    @Test fun inPlaceUpdatesUseVersionAndSourceWhileKeepingKernelBootIdentitySeparate() {
        for ((version, source) in listOf(32 to "b".repeat(12), 31 to "b".repeat(12))) {
            val store = store(file("$version-$source.json"))
            BootDiagnosticsRecorder(store).observe(observation())
            val snapshot = BootDiagnosticsRecorder(store).observe(observation(process = 2,
                atSeconds = 30, version = version, build = source))
            assertEquals("package_update", snapshot.current?.transition)
            assertNull(snapshot.current?.bootCountDelta)
        }
    }

    @Test fun aChangedKernelUuidOrIncreasingNativeCountEstablishesARecordedNewBoot() {
        for ((id, count) in listOf(otherBoot to 26, null to 26)) {
            val store = store(file("${id ?: "count"}.json"))
            BootDiagnosticsRecorder(store).observe(observation())
            val snapshot = BootDiagnosticsRecorder(store).observe(observation(id, count, process = 2,
                atSeconds = 30, elapsed = 500))
            assertEquals("kernel_boot", snapshot.current?.transition)
            assertEquals(1, snapshot.current?.bootCountDelta)
        }
    }

    @Test fun unavailableConflictingAndRolledBackIdentityNeverCreatesAClaimedBootCountDelta() {
        val cases = listOf(
            observation(id = null, count = null, process = 2, atSeconds = 30),
            observation(id = boot, count = 26, process = 2, atSeconds = 30),
            observation(id = null, count = 24, process = 2, atSeconds = 30),
            observation(process = 2, atSeconds = 30, elapsed = 100),
        )
        cases.forEachIndexed { i, value ->
            val store = store(file("conflict-$i.json"))
            BootDiagnosticsRecorder(store).observe(observation())
            val snapshot = BootDiagnosticsRecorder(store).observe(value)
            assertEquals("unknown", snapshot.current?.transition)
            assertNull(snapshot.current?.bootCountDelta)
        }
    }

    @Test fun countDeltasAndHistoryStayBoundedAcrossManyStarts() {
        val store = store()
        repeat(12) { i ->
            val id = UUID(100, i.toLong()).toString()
            BootDiagnosticsRecorder(store).observe(observation(id, 25 + i, i + 1, i * 30L, elapsed = 500))
        }
        val record = checkNotNull(store.read().record)
        assertEquals(BOOT_DIAGNOSTIC_HISTORY_LIMIT, record.history.size)
        assertEquals(29, record.history.first().bootCount)
        val huge = BootDiagnosticsRecorder(store).observe(observation(otherBoot, Int.MAX_VALUE, 50, 400, elapsed = 500))
        assertEquals("kernel_boot", huge.current?.transition)
        assertNull(huge.current?.bootCountDelta)
    }

    @Test fun publicProjectionNeverContainsPrivateIdentityOrUnboundedVendorText() {
        val file = file()
        val value = observation(reason = boundedBootReason("vendor-user@example.test-secret", "reboot"))
        val snapshot = BootDiagnosticsRecorder(store(file)).observe(value)
        val wire = snapshot.toJson().toString()
        assertFalse(wire.contains(boot))
        assertFalse(wire.contains(value.processToken))
        assertFalse(wire.contains("example.test"))
        assertFalse(wire.contains("boot_id"))
        assertFalse(wire.contains("process_token"))
        assertEquals("reboot", snapshot.current?.bootReason)
        assertTrue(file.readText().contains(boot)) // Kept only in the app-private diagnostic record.
    }

    @Test fun genericReasonAndProcessExitCannotBePromotedIntoAProvenKernelCause() {
        val store = store()
        BootDiagnosticsRecorder(store).observe(observation())
        val snapshot = BootDiagnosticsRecorder(store).observe(observation(otherBoot, 26, 2, 60, elapsed = 500),
            BootProcessExits(true, listOf(BootProcessExit("anr", epoch.plusSeconds(30).toString()))))
        assertEquals("anr", snapshot.current?.previousProcessExit)
        assertEquals("reboot", snapshot.current?.bootReason)
        assertEquals("unknown", snapshot.toJson().getString("cause_confidence"))
        assertEquals("reported", BootDiagnosticsRecorder(store(file("thermal.json"))).observe(observation(
            reason = boundedBootReason("reboot,thermal", null))).toJson().getString("cause_confidence"))
    }

    @Test fun priorProcessExitMustBeFromThisObservedIntervalAndIsOnlyABoundedCategory() {
        val store = store()
        BootDiagnosticsRecorder(store).observe(observation())
        val exits = BootProcessExits(true, listOf(
            BootProcessExit("crash", epoch.minusSeconds(5).toString()),
            BootProcessExit("native_crash", epoch.plusSeconds(70).toString()),
            BootProcessExit("anr", "1800-01-01T00:00:00Z"),
            BootProcessExit("raw private trace", epoch.plusSeconds(50).toString()),
            BootProcessExit("low_memory", epoch.plusSeconds(20).toString()),
            BootProcessExit("crash", epoch.plusSeconds(30).toString()),
        ))
        val snapshot = BootDiagnosticsRecorder(store).observe(observation(process = 2, atSeconds = 60), exits)
        assertEquals("crash", snapshot.current?.previousProcessExit)
        assertEquals(epoch.plusSeconds(30).toString(), snapshot.current?.previousProcessExitAtUtc)
        assertFalse(snapshot.toJson().toString().contains("trace"))
    }

    @Test fun unsupportedAndDeniedExitApisStayUnavailableWithoutRunningOldPlatformCode() {
        var called = false
        assertFalse(boundedProcessExits(29) { called = true; emptyList() }.available)
        assertFalse(called)
        assertFalse(boundedProcessExits(34) { throw SecurityException("private vendor detail") }.available)
        assertEquals(4, boundedProcessExits(34) {
            (0..20).map { BootProcessExit("crash", epoch.plusSeconds(it.toLong()).toString()) }
        }.entries.size)
        assertEquals("unknown", boundedExitReason(999))
        assertEquals("crash", boundedExitReason(4))
        assertEquals("package_updated", boundedExitReason(16))
    }

    @Test fun damagedUnboundedOrExtendedPrivateRecordsStayUnavailableAndAreNotOverwritten() {
        for ((i, transform) in listOf<(String) -> String>(
            { it + "trailing-data" },
            { it.replace("\"sha256\":\"", "\"sha256\":\"0") },
            { "x".repeat(BOOT_DIAGNOSTIC_MAX_BYTES + 1) },
            { text -> val envelope = JSONObject(text); val body = JSONObject(envelope.getString("payload"))
                body.put("raw_trace", "private"); envelopeFor(body.toString()) },
        ).withIndex()) {
            val file = file("damaged-$i.json")
            val store = store(file)
            BootDiagnosticsRecorder(store).observe(observation())
            val damaged = transform(file.readText())
            file.writeText(damaged)
            val snapshot = BootDiagnosticsRecorder(store).observe(observation(process = 2, atSeconds = 30))
            assertEquals("invalid", snapshot.storageState)
            assertNull(snapshot.current)
            assertEquals(damaged, file.readText())
        }
    }

    @Test fun unreadableAndFailedWritesAreVisibleAndKeepThePreviousCommittedRecord() {
        val directory = file("directory").apply { mkdirs() }
        assertEquals("read_failed", BootDiagnosticsRecorder(store(directory)).observe(observation()).storageState)
        val file = file()
        val store = store(file)
        BootDiagnosticsRecorder(store).observe(observation())
        val before = file.readText()
        File(file.path + ".new").mkdirs() // Writing a file over this directory must fail.
        val snapshot = BootDiagnosticsRecorder(store).observe(observation(process = 2, atSeconds = 30))
        assertEquals("write_failed", snapshot.storageState)
        assertEquals(1, snapshot.history.size)
        assertEquals(before, file.readText())
    }

    @Test fun malformedTypesUnknownReasonsAndOutOfRangeTimesAreRejectedEvenWithCorrectChecksum() {
        val changes: List<(JSONObject) -> Unit> = listOf(
            { it.put("boot_count", 25.5) },
            { it.put("boot_reason", "a vendor secret") },
            { it.put("observed_at_utc", "2101-01-01T00:00:00Z") },
            { it.put("app_version_code", 0) },
        )
        changes.forEachIndexed { i, change ->
            val file = file("type-$i.json")
            val store = store(file)
            BootDiagnosticsRecorder(store).observe(observation())
            val body = JSONObject(JSONObject(file.readText()).getString("payload"))
            change(body.getJSONObject("observation"))
            file.writeText(envelopeFor(body.toString()))
            assertEquals("mutation $i", "invalid", store.read().state)
        }
        // Android JSONObject.toString() normalises25.0 to25, so constructing that
        // value with put(Double) accidentally produces a valid integer fixture.
        // Preserve a literal floating token in the checksummed payload instead.
        val file = file("integral-double.json")
        val store = store(file)
        BootDiagnosticsRecorder(store).observe(observation())
        val payload = JSONObject(file.readText()).getString("payload")
        val floating = payload.replace("\"boot_count\":25", "\"boot_count\":25.0")
        assertNotEquals(payload, floating)
        assertTrue(JSONObject(floating).getJSONObject("observation").get("boot_count") is Double)
        file.writeText(envelopeFor(floating))
        assertEquals("literal floating token", "invalid", store.read().state)
    }

    @Test fun invalidFreshProbeDoesNotWriteAnApparentlyHealthyDiagnostic() {
        val file = file()
        val store = store(file)
        assertEquals("probe_unavailable", BootDiagnosticsRecorder(store).observe(observation(
            atSeconds = -1_000_000_000_000)).storageState)
        assertFalse(file.exists())
    }

    @Test fun rawBootPropertiesAreReducedToExactAllowlistedCategories() {
        assertEquals(BootReportedReason("unknown", "unavailable"), boundedBootReason("thermal,private", "vendor-secret"))
        assertEquals(BootReportedReason("unknown", "unavailable"), boundedBootReason("x".repeat(1_000), null))
        assertEquals(BootReportedReason("reboot", "sys_boot_reason"), boundedBootReason("reboot", "watchdog"))
        assertEquals(BootReportedReason("watchdog", "ro_boot_bootreason"), boundedBootReason(null, "WATCHDOG"))
        assertEquals(BootReportedReason("adb", "sys_boot_reason"), boundedBootReason("reboot,adb", "reboot"))
    }

    @Test @Config(sdk = [28]) fun androidAdapterUsesNoBackupStorageAndDoesNotRequireTheNewExitApi() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val file = File(context.noBackupFilesDir, "boot-diagnostics-v1.json")
        file.delete()
        Settings.Global.putInt(context.contentResolver, Settings.Global.BOOT_COUNT, 25)
        val diagnostics = BootDiagnostics(context)
        assertEquals("pending", diagnostics.snapshot().storageState)
        diagnostics.observeServiceStart()
        assertEquals("ready", diagnostics.snapshot().storageState)
        assertTrue(file.isFile)
        assertEquals("unavailable", diagnostics.snapshot().current?.previousProcessExit)
    }

    private fun envelopeFor(payload: String): String = JSONObject().put("payload", payload).put("sha256",
        MessageDigest.getInstance("SHA-256").digest(payload.toByteArray())
            .joinToString("") { "%02x".format(it) }).toString()
}
