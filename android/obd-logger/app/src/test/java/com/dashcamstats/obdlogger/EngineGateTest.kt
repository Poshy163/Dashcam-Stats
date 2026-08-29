package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.util.UUID
import java.util.zip.ZipEntry
import java.util.zip.ZipFile

class EngineGateTest {
    @Test
    fun recentRpmVetoesDipButSustainedLowVoltageStops() {
        val gate = EngineGate(voltageOff = 13.0, graceMillis = 30_000, rpmVetoMillis = 30_000)
        assertTrue(gate.remainsOnline(0, 13.4, 850.0))
        assertTrue(gate.remainsOnline(10_000, 12.8, null))
        assertTrue(gate.remainsOnline(31_000, 12.8, null))
        assertTrue(gate.remainsOnline(60_000, 12.8, null))
        assertFalse(gate.remainsOnline(91_001, 12.8, null))
    }

    @Test
    fun lifecycleNeedsVoltageThenChecksumValidEcuProofAndSignalsGracefulStop() {
        val lifecycle = EngineLifecycle(voltageOn = 13.2, voltageOff = 13.0, graceMillis = 30_000)
        assertFalse(lifecycle.observeParkedVoltage(13.1))
        assertEquals(EngineLifecycleState.PARKED, lifecycle.state)
        assertTrue(lifecycle.observeParkedVoltage(13.2))
        assertEquals(EngineLifecycleState.PROBING, lifecycle.state)
        // Voltage alone never authorizes startDrive; the service reaches RECORDING only after
        // ElmBleClient.proveEcu has checksum/source/length-validated the 0100 response.
        assertTrue(lifecycle.acceptChecksumValidEcuProof(true))
        assertEquals(EngineLifecycleState.RECORDING, lifecycle.state)
        assertTrue(lifecycle.remainsRecording(0, 13.4, 850.0))
        assertTrue(lifecycle.remainsRecording(31_000, 12.8, null))
        assertFalse(lifecycle.remainsRecording(61_001, 12.8, null))
        assertEquals(EngineLifecycleState.STOPPED, lifecycle.state)

        val rejected = EngineLifecycle(13.2, 13.0, 30_000)
        assertTrue(rejected.observeParkedVoltage(13.4))
        assertFalse(rejected.acceptChecksumValidEcuProof(false))
        assertEquals(EngineLifecycleState.PARKED, rejected.state)
    }

    @Test
    fun pollPlanNeverQueriesOrCountsUnsupportedPidsAsFailures() {
        val supported = setOf(0x0C, 0x03, 0x15, 0x13, 0x1C, 0x21, 0x99)
        val requested = ObdPollPlan.requestedPids(sequence = 0, supported = supported)
        assertEquals(listOf(0x0C, 0x03, 0x15, 0x13, 0x1C, 0x21), requested)

        val queried = mutableListOf<Int>()
        val missing = mutableListOf<Int>()
        for (pid in requested) {
            queried += pid
            val reply = if (pid in supported) mapOf("value" to 1.0) else emptyMap()
            if (reply.isEmpty()) missing += pid
        }
        assertEquals(listOf(0x0C, 0x03, 0x15, 0x13, 0x1C, 0x21), queried)
        assertTrue(missing.isEmpty())
        assertEquals(listOf(0x0C), ObdPollPlan.requestedPids(sequence = 1, supported = supported))
    }

    @Test
    fun uuid7CarriesTimeVersionAndVariant() {
        val first = UUID.fromString(uuid7(1_000))
        val second = UUID.fromString(uuid7(2_000))
        assertEquals(7, first.version())
        assertEquals(2, first.variant())
        assertTrue(first.toString() < second.toString())
    }

    @Test
    fun generatedLoggerIdentityIsBoundedStableFormatWithoutRawDeviceIds() {
        val uuid = UUID.fromString("01234567-89ab-4def-8123-456789abcdef")
        val loggerId = generatedLoggerId(uuid)
        assertEquals("logger-01234567-89ab-4def-8123-456789abcdef", loggerId)
        assertTrue(
            LoggerConfig(
                enabled = true,
                ownershipTransferred = true,
                adapterAddress = "00:11:22:33:44:55",
                vehicleId = "nissan_tiida",
                loggerId = loggerId,
            ).canRun,
        )
    }

    @Test
    fun missingVoltageAndRpmEventuallyStops() {
        val gate = EngineGate(voltageOff = 13.0, graceMillis = 30_000, rpmVetoMillis = 30_000)
        assertTrue(gate.remainsOnline(0, 13.4, 900.0))
        assertTrue(gate.remainsOnline(31_000, null, null))
        assertTrue(gate.remainsOnline(60_000, null, null))
        assertFalse(gate.remainsOnline(62_000, null, null))
    }

    @Test
    fun completedDriveRecoveryRetriesFailuresAndQuarantinesEmptyCrashes() {
        assertEquals(
            ExportRecoveryAction.EXPORT,
            ExportRecoveryPlanner.action(1, "waiting_for_backup"),
        )
        assertEquals(
            ExportRecoveryAction.QUARANTINE_ZERO_SAMPLES,
            ExportRecoveryPlanner.action(0, "waiting_for_backup"),
        )
        assertEquals(
            ExportRecoveryAction.SKIP,
            ExportRecoveryPlanner.action(10, "exported"),
        )
        assertEquals(
            ExportRecoveryAction.SKIP,
            ExportRecoveryPlanner.action(0, "not_exportable_zero_samples"),
        )
    }

    @Test
    fun stickyRestartNeverStartsTypedForegroundServiceAfterPermissionRevocation() {
        assertEquals(
            ServiceStartupDecision.STOP_PERMISSION_REQUIRED,
            ServiceStartupGate.decide(canRun = true, hasPermissions = false),
        )
        assertEquals(
            ServiceStartupDecision.STOP_DISABLED,
            ServiceStartupGate.decide(canRun = false, hasPermissions = true),
        )
        assertEquals(
            ServiceStartupDecision.START,
            ServiceStartupGate.decide(canRun = true, hasPermissions = true),
        )
    }

    @Test
    fun unchangedStatusIsDurableOnlyOnACoarseHeartbeat() {
        val gate = StatusWriteGate(heartbeatMillis = 60_000)
        assertTrue(gate.shouldWrite("ecu_online", 0))
        assertFalse(gate.shouldWrite("ecu_online", 5_000))
        assertTrue(gate.shouldWrite("backoff", 5_001))
        assertFalse(gate.shouldWrite("backoff", 10_000))
        assertTrue(gate.shouldWrite("backoff", 65_001))
        gate.writeFailed("backoff")
        assertTrue(gate.shouldWrite("backoff", 65_002))
    }

    @Test
    fun sparseDiagnosticWorkIsCappedAtOneStepPerSampleCycle() {
        val queue = OneStepPerCycleQueue<Int>()
        repeat(20, queue::add)
        assertEquals(0, queue.take(0))
        assertNull(queue.take(0))
        assertEquals(1, queue.take(1))
        assertNull(queue.take(1))
        assertEquals(2, queue.take(2))
    }

    @Test
    fun dtcScanCompletionRequiresAllThreeSuccessfulModes() {
        val sparse = DtcScanCompletionTracker()
        sparse.markSuccessful(0x03)
        assertFalse(sparse.isComplete)
        sparse.markSuccessful(0x07)
        assertFalse(sparse.isComplete)

        val complete = DtcScanCompletionTracker()
        complete.markSuccessful(0x03)
        complete.markSuccessful(0x07)
        complete.markSuccessful(0x0A)
        assertTrue(complete.isComplete)
    }

    @Test
    fun sparseDiagnosticFailuresPreserveParserTransportAndRejectionSemantics() {
        assertEquals("parser_failure", diagnosticFailureKind(ElmProtocolException("bad frame")))
        assertEquals("malformed", diagnosticProbeStatus(ElmProtocolException("bad frame")))

        val rejected = ElmCommandRejectedException("NRC 12")
        assertEquals("connection_failure", diagnosticFailureKind(rejected))
        assertEquals("rejected", diagnosticProbeStatus(rejected))

        val disconnected = ElmException("BLE disconnected")
        assertEquals("connection_failure", diagnosticFailureKind(disconnected))
        assertEquals("transport_error", diagnosticProbeStatus(disconnected))

        val freezeScan = DiagnosticScanFailureTracker()
        freezeScan.failed("transport_error")
        assertEquals("transport_error", freezeScan.finalStatus("ok"))
        freezeScan.reset()
        assertEquals("ok", freezeScan.finalStatus("ok"))
    }

    @Test
    fun configurableEngineGateRequiresBoundedHysteresis() {
        assertTrue(
            LoggerConfig(true, true, "00:11:22:33:44:55", "tiida", "logger-1").canRun,
        )
        assertFalse(
            LoggerConfig(
                true,
                true,
                "00:11:22:33:44:55",
                "tiida",
                "logger-1",
                voltageOn = 13.0,
                voltageOff = 13.0,
            ).thresholdConfigurationValid,
        )
        assertFalse(
            LoggerConfig(
                true,
                true,
                "00:11:22:33:44:55",
                "tiida",
                "logger-1",
                offGraceSeconds = 301,
            ).thresholdConfigurationValid,
        )
    }

    @Test
    fun exportNeverFallsBackWhenRemovableStorageMountsLate() {
        assertNull(
            RemovableStoragePolicy.selectIndex(
                listOf(StorageVolumeState(removable = false, mounted = true)),
            ),
        )
        assertEquals(
            1,
            RemovableStoragePolicy.selectIndex(
                listOf(
                    StorageVolumeState(removable = false, mounted = true),
                    StorageVolumeState(removable = true, mounted = true),
                ),
            ),
        )
    }

    @Test
    fun atomicStoredZipIsFinishedAndSyncedBeforeItsDescriptorCloses() {
        val directory = Files.createTempDirectory("obd-zip-test").toFile()
        try {
            val first = directory.resolve("one.json").apply { writeText("{\"one\":1}") }
            val second = directory.resolve("two.json").apply { writeText("{\"two\":2}") }
            val bundle = directory.resolve("bundle.partial")
            writeStoredZip(bundle, listOf("one.json" to first, "two.json" to second))
            ZipFile(bundle).use { zip ->
                assertEquals(setOf("one.json", "two.json"), zip.entries().toList().map { it.name }.toSet())
                assertTrue(zip.entries().toList().all { it.method == ZipEntry.STORED })
            }
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun exporterUsesExactServerDriveIdContract() {
        assertTrue(isSafeDriveId("019d0123-4567-7abc-8123-456789abcdef"))
        assertFalse(isSafeDriveId("drive.with.dot"))
        assertFalse(isSafeDriveId("d".repeat(65)))
    }

    @Test
    fun recoveredDrivePreservesVersionedCompletionStatus() {
        assertEquals("recovered", completionStatus("device_restart"))
        assertEquals("complete", completionStatus("engine_stopped"))
        assertEquals("complete", completionStatus(null))
    }
}
