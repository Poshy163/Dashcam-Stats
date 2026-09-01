package com.dashcamstats.obdlogger

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.time.Instant
import java.util.UUID
import java.util.zip.ZipEntry
import java.util.zip.ZipFile

@OptIn(ExperimentalCoroutinesApi::class)
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
        assertEquals(listOf(0x0C, 0x03, 0x15, 0x13), requested)

        val queried = mutableListOf<Int>()
        val missing = mutableListOf<Int>()
        for (pid in requested) {
            queried += pid
            val reply = if (pid in supported) mapOf("value" to 1.0) else emptyMap()
            if (reply.isEmpty()) missing += pid
        }
        assertEquals(listOf(0x0C, 0x03, 0x15, 0x13), queried)
        assertTrue(missing.isEmpty())
        assertEquals(listOf(0x0C), ObdPollPlan.requestedPids(sequence = 1, supported = supported))
    }

    @Test
    fun malformedOptionalLivePidIsMissingAndDoesNotPoisonTheNextPid() = runTest {
        val queried = mutableListOf<Int>()
        val malformed = pollLivePid(0x15) { pid ->
            queried += pid
            val badChecksum = "48\r6B\r10\r41\r15\r80\r00\r>"
            val payload = ElmProtocol.requirePayload(badChecksum, "0115", pid, 2, 0x10).second
            ElmProtocol.decode(pid, payload)
        }
        assertTrue(malformed is LivePidPollResult.Malformed)

        val next = pollLivePid(0x0C) { pid ->
            queried += pid
            mapOf("engine_rpm" to 850.0)
        }
        assertEquals(listOf(0x15, 0x0C), queried)
        assertEquals(
            mapOf("engine_rpm" to 850.0),
            (next as LivePidPollResult.Values).decoded,
        )
    }

    @Test
    fun livePidPollerNeverDowngradesTransportFailuresToMissingValues() {
        val disconnected = assertThrows(ElmException::class.java) {
            runTest {
                pollLivePid(0x15) { throw ElmException("BLE disconnected") }
            }
        }
        assertEquals("BLE disconnected", disconnected.message)
    }

    @Test
    fun transientMalformedPidRetriesAndPermanentFailureUsesBoundedCircuitBreaker() {
        val tracker = LivePidMalformedTracker()
        assertTrue(tracker.shouldPoll(0x10, 0))
        assertEquals(2, tracker.recordMalformed(0x10, 0).retryAtCycle)
        assertFalse(tracker.shouldPoll(0x10, 1))
        assertTrue(tracker.shouldPoll(0x10, 2))
        tracker.recordValid(0x10)
        assertTrue(tracker.shouldPoll(0x10, 3))

        val permanent = LivePidMalformedTracker(baseCooldownCycles = 2, maximumCooldownCycles = 12)
        assertEquals(2, permanent.recordMalformed(0x15, 0).retryAtCycle)
        assertEquals(6, permanent.recordMalformed(0x15, 2).retryAtCycle)
        assertEquals(14, permanent.recordMalformed(0x15, 6).retryAtCycle)
        assertEquals(26, permanent.recordMalformed(0x15, 14).retryAtCycle)
        assertFalse(permanent.shouldPoll(0x15, 25))
        assertTrue(permanent.shouldPoll(0x15, 26))
    }

    @Test
    fun mediumAndSlowCommandsAreDistributedWithoutChangingPerPidCadence() {
        val supported = setOf(
            0x04, 0x0C, 0x0D,
            0x0E, 0x10, 0x11,
            0x03, 0x05, 0x06, 0x07, 0x0F, 0x14, 0x15,
            0x13, 0x1C, 0x21,
        )
        val cycles = (0L until 24L).associateWith { ObdPollPlan.requestedPids(it, supported) }
        assertTrue(cycles.values.all { requested -> requested.containsAll(listOf(0x04, 0x0C, 0x0D)) })
        val rotatingCountsPerCycle = cycles.values.map { requested -> requested.size - 3 }
        assertTrue(rotatingCountsPerCycle.all { it in 3..5 })
        for (pid in listOf(0x0E, 0x10, 0x11, 0x03, 0x05, 0x06, 0x07, 0x0F, 0x14, 0x15)) {
            val observed = cycles.filterValues { pid in it }.keys.toList()
            assertEquals(8, observed.size)
            assertTrue(observed.zipWithNext().all { (first, second) -> second - first == 3L })
            assertEquals("medium", ObdPollPlan.tier(pid))
            assertEquals(3, ObdPollPlan.expectedIntervalCycles(pid))
        }
        for (pid in listOf(0x13, 0x1C, 0x21)) {
            val observed = cycles.filterValues { pid in it }.keys.toList()
            assertEquals(2, observed.size)
            assertEquals(12L, observed[1] - observed[0])
            assertEquals("slow", ObdPollPlan.tier(pid))
            assertEquals(12, ObdPollPlan.expectedIntervalCycles(pid))
        }
    }

    @Test
    fun parkedProbeIsAdapterLocalAndFullInitializationIsDeferred() {
        assertEquals("ATRV", ElmAdapterCommandPlan.parkedVoltageProbe)
        assertFalse(ElmAdapterCommandPlan.parkedVoltageProbe.startsWith("01"))
        assertFalse(ElmAdapterCommandPlan.fullInitialization.contains("ATRV"))
        assertTrue(ElmAdapterCommandPlan.fullInitialization.contains("ATZ"))
        assertTrue(ElmAdapterCommandPlan.fullInitialization.all(ElmProtocol::isSafe))
        assertTrue(ElmAdapterCommandPlan.fullInitialization.none { it.startsWith("01") })
        assertFalse(parkedVoltageWarrantsInitialization(null, voltageOn = 13.2))
        assertFalse(parkedVoltageWarrantsInitialization(Double.NaN, voltageOn = 13.2))
        assertFalse(parkedVoltageWarrantsInitialization(Double.POSITIVE_INFINITY, voltageOn = 13.2))
        assertFalse(parkedVoltageWarrantsInitialization(13.19, voltageOn = 13.2))
        assertTrue(parkedVoltageWarrantsInitialization(13.2, voltageOn = 13.2))
        assertTrue(parkedVoltageWarrantsInitialization(14.1, voltageOn = 13.2))
        assertFalse(parkedProbeMayInitialize(true, 14.1, voltageOn = 13.2))
        assertTrue(parkedProbeMayInitialize(false, 14.1, voltageOn = 13.2))
    }

    @Test
    fun parkedVoltageProbeExecutesOnlyAtrvAndHonorsQuiesceBoundaries() {
        val commands = mutableListOf<String>()
        runTest {
            val voltage = probeParkedAdapterVoltage(quiesceRequested = { false }) { command ->
                commands += command
                "ATRV\r12.4 V\r>"
            }
            assertEquals(12.4, voltage!!, 0.001)
        }
        assertEquals(listOf("ATRV"), commands)

        val beforeCommands = mutableListOf<String>()
        assertThrows(ElmQuiesceRequestedException::class.java) {
            runTest {
                probeParkedAdapterVoltage(quiesceRequested = { true }) { command ->
                    beforeCommands += command
                    "12.4 V\r>"
                }
            }
        }
        assertTrue(beforeCommands.isEmpty())

        var quiesceChecks = 0
        val afterCommands = mutableListOf<String>()
        assertThrows(ElmQuiesceRequestedException::class.java) {
            runTest {
                probeParkedAdapterVoltage(quiesceRequested = { ++quiesceChecks == 2 }) { command ->
                    afterCommands += command
                    "12.4 V\r>"
                }
            }
        }
        assertEquals(listOf("ATRV"), afterCommands)
        assertEquals(2, quiesceChecks)
    }

    @Test
    fun voltageFreshnessExpiresWithoutTurningStaleDataIntoReachability() {
        val probe = VoltageProbeSnapshot(
            result = "valid",
            sampleAtUtc = "2026-08-30T00:00:00Z",
            parsedVoltage = 12.7,
            sanitizedRawResponse = "12.7 V",
        )

        val fresh = voltagePublicState(probe, Instant.parse("2026-08-30T00:01:15Z"), 75)
        assertTrue(fresh.fresh)
        assertTrue(fresh.adapterReachable)
        assertEquals("valid", fresh.quality)

        val stale = voltagePublicState(probe, Instant.parse("2026-08-30T00:01:16Z"), 75)
        assertFalse(stale.fresh)
        assertFalse(stale.adapterReachable)
        assertEquals("stale", stale.quality)

        val invalid = voltagePublicState(
            VoltageProbeSnapshot("invalid", "2026-08-30T00:01:16Z"),
            Instant.parse("2026-08-30T00:01:16Z"),
            75,
        )
        assertFalse(invalid.fresh)
        assertEquals("invalid", invalid.quality)
    }

    @Test
    fun onlyFailureArmsAReconnectAttempt() {
        val tracker = ReconnectAttemptTracker()
        assertFalse(tracker.nextAttemptIsReconnect())
        assertFalse(tracker.nextAttemptIsReconnect())

        tracker.connectionFailed()
        assertTrue(tracker.nextAttemptIsReconnect())
        assertFalse(tracker.nextAttemptIsReconnect())

        tracker.connectionFailed()
        tracker.connectionFailed()
        assertTrue(tracker.nextAttemptIsReconnect())
        assertFalse(tracker.nextAttemptIsReconnect())
    }

    @Test
    fun retryBackoffIsBoundedJitteredAndResettable() {
        val centered = BoundedExponentialBackoff(
            baseDelayMillis = 2_000,
            maximumDelayMillis = 300_000,
            jitterFraction = 0.2,
            randomUnit = { 0.5 },
        )
        assertEquals(2_000L, centered.nextDelayMillis())
        assertEquals(4_000L, centered.nextDelayMillis())
        assertEquals(8_000L, centered.nextDelayMillis())
        repeat(20) { centered.nextDelayMillis() }
        assertEquals(300_000L, centered.nextDelayMillis())
        centered.reset()
        assertEquals(2_000L, centered.nextDelayMillis())

        val lowJitter = BoundedExponentialBackoff(randomUnit = { 0.0 })
        val highJitter = BoundedExponentialBackoff(randomUnit = { 1.0 })
        assertEquals(1_600L, lowJitter.nextDelayMillis())
        assertEquals(2_400L, highJitter.nextDelayMillis())
        repeat(30) { highJitter.nextDelayMillis() }
        assertTrue(highJitter.nextDelayMillis() <= 300_000L)
    }

    @Test
    fun liveReconnectProgressAndWakeSignalsResetTheShortRetryBudget() {
        val controller = ConnectionRetryController(
            BoundedExponentialBackoff(
                baseDelayMillis = 2_000,
                maximumDelayMillis = 30_000,
                jitterFraction = 0.0,
            ),
        )
        assertEquals(ConnectionFailureDecision(2_000L, 1), controller.failed())
        assertEquals(ConnectionFailureDecision(4_000L, 2), controller.failed())
        assertEquals(ConnectionFailureDecision(8_000L, 3), controller.failed())

        // A checksum-valid ECU proof occurs while runOneConnection is still active. It must
        // reset escalation immediately rather than waiting until the whole drive ends.
        controller.progressConfirmed()
        assertEquals(ConnectionFailureDecision(2_000L, 1), controller.failed())
        repeat(20) { controller.failed() }
        assertEquals(30_000L, controller.failed().delayMillis)

        controller.externalWakeObserved()
        val afterWake = controller.failed()
        assertEquals(2_000L, afterWake.delayMillis)
        assertTrue(afterWake.consecutiveFailures > 1)

        controller.progressConfirmed()
        assertEquals(ConnectionFailureDecision(2_000L, 1), controller.failed())

        assertEquals("bluetooth_on", LoggerWakeReason.BLUETOOTH_ON.eventReasonCode())
        assertEquals("screen_on", LoggerWakeReason.SCREEN_ON.eventReasonCode())
        assertEquals("user_present", LoggerWakeReason.USER_PRESENT.eventReasonCode())
        assertEquals("power_connected", LoggerWakeReason.POWER_CONNECTED.eventReasonCode())
        assertEquals("acc_on", LoggerWakeReason.ACC_ON.eventReasonCode())
    }

    @Test
    fun headUnitWakeInterruptsALongReconnectDelay() = runTest {
        val signals = Channel<LoggerWakeReason>(Channel.CONFLATED)
        val waiting = async {
            awaitInterruptibleDelay(
                durationMillis = 300_000,
                wakeSignals = signals,
                preempted = { false },
                monotonicMillis = { testScheduler.currentTime },
            )
        }
        runCurrent()
        advanceTimeBy(900)
        signals.trySend(LoggerWakeReason.BLUETOOTH_ON)
        runCurrent()

        assertEquals(
            InterruptibleWaitResult.Woken(LoggerWakeReason.BLUETOOTH_ON),
            waiting.await(),
        )
        assertEquals(900L, testScheduler.currentTime)
    }

    @Test
    fun wakeQueuedDuringAFailedAttemptSurvivesIntoTheRetryWait() = runTest {
        val signals = Channel<LoggerWakeReason>(Channel.CONFLATED)
        signals.trySend(LoggerWakeReason.ACC_ON)

        val waiting = async {
            awaitInterruptibleDelay(
                durationMillis = 30_000,
                wakeSignals = signals,
                preempted = { false },
                monotonicMillis = { testScheduler.currentTime },
            )
        }
        runCurrent()

        assertEquals(InterruptibleWaitResult.Woken(LoggerWakeReason.ACC_ON), waiting.await())
        assertEquals(0L, testScheduler.currentTime)
    }

    @Test
    fun ingestionRequestPreemptsReconnectWaitWithinTheBoundedPollInterval() = runTest {
        val signals = Channel<LoggerWakeReason>(Channel.CONFLATED)
        var ingestionRequested = false
        val waiting = async {
            awaitInterruptibleDelay(
                durationMillis = 30_000,
                wakeSignals = signals,
                preempted = { ingestionRequested },
                monotonicMillis = { testScheduler.currentTime },
                preemptionPollMillis = 250,
            )
        }
        runCurrent()
        advanceTimeBy(750)
        ingestionRequested = true
        advanceTimeBy(250)
        runCurrent()

        assertEquals(InterruptibleWaitResult.Preempted, waiting.await())
        assertEquals(1_000L, testScheduler.currentTime)

        assertEquals(
            InterruptibleWaitResult.Preempted,
            awaitInterruptibleDelay(
                durationMillis = 30_000,
                wakeSignals = signals,
                preempted = { true },
                monotonicMillis = { testScheduler.currentTime },
            ),
        )
        assertEquals(1_000L, testScheduler.currentTime)
    }

    @Test
    fun exhaustedLiveCycleBudgetDefersSparseDiagnosticWithoutDequeuingIt() {
        val budget = SparseDiagnosticBudgetTracker()
        assertEquals(6_000L, budget.requiredBudgetMillis())
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(0, budget.requiredBudgetMillis()))

        budget.observeCommand(700)
        assertEquals(2_000L, budget.requiredBudgetMillis())
        assertTrue(ObdPollPlan.mayRunSparseDiagnostic(3_000, budget.requiredBudgetMillis()))
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(3_001, budget.requiredBudgetMillis()))

        budget.observeCommand(2_900)
        assertEquals(6_000L, budget.requiredBudgetMillis())
        assertFalse(ObdPollPlan.mayRunSparseDiagnostic(0, budget.requiredBudgetMillis()))

        val queue = OneStepPerCycleQueue<String>()
        queue.add("readiness")
        if (ObdPollPlan.mayRunSparseDiagnostic(0, budget.requiredBudgetMillis())) queue.take(0)
        assertEquals("readiness", queue.take(1))
    }

    @Test
    fun fatalTransportFailurePersistsOnlyNonemptyPartialValuesWithStableSequence() {
        assertNull(
            partialSampleAfterTransportFailure(
                "drive-1", 7, "2026-08-29T12:00:00Z", emptyMap(), listOf(0x15),
            ),
        )

        val sample = partialSampleAfterTransportFailure(
            driveId = "drive-1",
            sequence = 7,
            timestampUtc = "2026-08-29T12:00:00Z",
            values = linkedMapOf("engine_rpm" to 850.0),
            missingPids = listOf(0x15, 0x15, 0x21),
        )!!
        assertEquals("drive-1-7", sample.sampleId)
        assertEquals(7, sample.sequence)
        assertEquals(mapOf("engine_rpm" to 850.0), sample.values)
        assertEquals("failed_after_partial", sample.transportQuality)
        assertEquals("partial", sample.parserQuality)
        assertEquals(listOf(0x15, 0x21), sample.missingPids)
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
    fun savedConfigurationRestartsAnActiveWorkerBeforeApplyingNewPolicy() {
        assertEquals(
            ServiceWorkerDecision.RESTART_FOR_CONFIGURATION,
            serviceWorkerDecision(workerActive = true, configurationReloadRequested = true),
        )
        assertEquals(
            ServiceWorkerDecision.KEEP_RUNNING,
            serviceWorkerDecision(workerActive = true, configurationReloadRequested = false),
        )
        assertEquals(
            ServiceWorkerDecision.START,
            serviceWorkerDecision(workerActive = false, configurationReloadRequested = true),
        )
    }

    @Test
    fun databaseInitializationIsGatedBehindForegroundPromotion() {
        val gate = ForegroundFirstStartupGate()
        var databaseInitialized = false

        val error = assertThrows(IllegalStateException::class.java) {
            gate.afterForeground { databaseInitialized = true }
        }
        assertEquals("database initialization requires foreground promotion", error.message)
        assertFalse(databaseInitialized)

        gate.markForegroundStarted()
        val result = gate.afterForeground {
            databaseInitialized = true
            "opened"
        }
        assertTrue(databaseInitialized)
        assertEquals("opened", result)
    }

    @Test
    fun unchangedStatusIsDurableOnlyOnACoarseHeartbeat() {
        val gate = StatusWriteGate()
        assertTrue(gate.shouldWrite("ecu_online", 0))
        assertFalse(gate.shouldWrite("ecu_online", 5_000))
        assertTrue(gate.shouldWrite("backoff", 5_001))
        assertFalse(gate.shouldWrite("backoff", 10_000))
        assertTrue(gate.shouldWrite("backoff", 65_001))
        gate.writeFailed("backoff")
        assertTrue(gate.shouldWrite("backoff", 65_002))
    }

    @Test
    fun voltageEvidenceTransitionsPublishWithoutWaitingForTheHeartbeat() {
        val gate = StatusWriteGate()
        val unavailable = PublicStatus(
            state = "parked",
            ownershipEnabled = true,
            batteryVoltageQuality = "unavailable",
        )
        val valid = unavailable.copy(
            adapterReachable = true,
            batteryVoltage = 12.7,
            batteryVoltageSource = "dashcam_elm_atrv",
            batteryVoltageFresh = true,
            batteryVoltageQuality = "valid",
        )
        val failed = valid.copy(
            adapterReachable = false,
            batteryVoltage = null,
            batteryVoltageFresh = false,
            batteryVoltageQuality = "failed",
        )

        assertTrue(gate.shouldWrite(durableStatusSignature(unavailable), 0))
        assertTrue(gate.shouldWrite(durableStatusSignature(valid), 30_000))
        assertTrue(gate.shouldWrite(durableStatusSignature(failed), 45_000))
        assertFalse(gate.shouldWrite(durableStatusSignature(failed), 59_999))
        assertTrue(gate.shouldWrite(durableStatusSignature(failed), 105_000))
    }

    @Test
    fun sampleAndMetricChurnDoesNotWriteStatusEveryCycle() {
        val gate = StatusWriteGate(heartbeatMillis = 60_000)
        val metrics = PipelineMetrics()
        var writes = 0
        repeat(12) { cycle ->
            metrics.commandRequested()
            metrics.sampleCreated()
            val status = PublicStatus(
                state = "ecu_online",
                ownershipEnabled = true,
                currentDriveId = "drive-1",
                lastSampleAtUtc = "2026-08-30T00:00:${cycle.toString().padStart(2, '0')}Z",
                metrics = metrics.snapshot(),
            )
            if (gate.shouldWrite(durableStatusSignature(status), cycle * 5_000L)) writes += 1
        }
        assertEquals(1, writes)

        val heartbeatStatus = PublicStatus(
            state = "ecu_online",
            ownershipEnabled = true,
            currentDriveId = "drive-1",
            lastSampleAtUtc = "2026-08-30T00:01:00Z",
            metrics = metrics.snapshot(),
        )
        assertTrue(gate.shouldWrite(durableStatusSignature(heartbeatStatus), 60_000L))

        val quiescing = heartbeatStatus.copy(
            state = "ingestion_ready",
            ingestionRequestId = "request-1",
        )
        assertTrue(gate.shouldWrite(durableStatusSignature(quiescing), 60_001L))
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
                parkedIntervalSeconds = 14,
            ).thresholdConfigurationValid,
        )
        assertFalse(
            LoggerConfig(
                true,
                true,
                "00:11:22:33:44:55",
                "tiida",
                "logger-1",
                parkedIntervalSeconds = 3_601,
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
        assertEquals("interrupted", completionStatus("connection_lost"))
        assertEquals("interrupted", completionStatus("ingestion_requested"))
        assertEquals("interrupted", completionStatus("administratively_disabled"))
        assertEquals("interrupted", completionStatus("process_terminated"))
        assertEquals("complete", completionStatus(null))
    }

    @Test
    fun onlyBootCompletedClassifiesAnOrphanAsDeviceRestart() {
        assertEquals("device_restart", startupRecoveryReason("device_restart"))
        assertEquals("process_terminated", startupRecoveryReason("process_terminated"))
        assertEquals("process_terminated", startupRecoveryReason(null))
        assertEquals("process_terminated", startupRecoveryReason("untrusted"))
    }

    @Test
    fun perDriveUtcClockIgnoresWallClockChangesAndNeverRegresses() {
        var elapsed = 10_000L
        val clock = MonotonicUtcClock(
            anchorUtc = Instant.parse("2026-08-30T00:00:00Z"),
            anchorElapsedMillis = elapsed,
            elapsedRealtimeMillis = { elapsed },
        )
        assertEquals("2026-08-30T00:00:00Z", clock.nowUtc())

        // A wall-clock update is deliberately absent from the timestamp source; only uptime moves.
        elapsed = 15_250L
        assertEquals("2026-08-30T00:00:05.250Z", clock.nowUtc())

        // Defensive clamping also prevents a faulty/regressed monotonic reading from going back.
        elapsed = 12_000L
        assertEquals("2026-08-30T00:00:05.250Z", clock.nowUtc())
    }

    @Test
    fun identicalThirtySecondParkedProbesProduceOneTransitionPerBand() {
        val gate = ParkedObservationEventGate()
        var events = 0

        // A full day at the default 30-second interval is one event, not 2,880 disk writes.
        repeat(2_880) {
            if (gate.changed(ParkedObservationBand.BELOW_START)) events += 1
        }
        assertEquals(1, events)
        if (gate.changed(ParkedObservationBand.ENGINE_CANDIDATE)) events += 1
        if (gate.changed(ParkedObservationBand.BELOW_START)) events += 1
        assertEquals(3, events)
    }

    @Test
    fun connectionTimeoutReasonRequiresTheStructuredTimeoutType() {
        assertEquals(
            "gatt_timeout",
            bleConnectionFailureReason(ElmConnectionTimeoutException("bounded timeout")),
        )
        assertEquals("gatt_error", bleConnectionFailureReason(ElmException("other failure")))
    }

    @Test
    fun firstSampleTimingCoversConnectPathAndShorterDriveStartSegment() {
        assertEquals(
            mapOf("first_sample_ms" to 2_500L, "elapsed_ms" to 400L),
            firstSampleTimingMetrics(
                connectionStartedAtMillis = 1_000L,
                driveStartedAtMillis = 3_100L,
                observedAtMillis = 3_500L,
            ),
        )
        assertEquals(
            3_600_000L,
            firstSampleTimingMetrics(0L, null, 4_000_000L).getValue("first_sample_ms"),
        )
    }
}
