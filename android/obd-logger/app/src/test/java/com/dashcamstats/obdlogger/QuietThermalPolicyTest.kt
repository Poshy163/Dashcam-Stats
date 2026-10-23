package com.dashcamstats.obdlogger

import android.content.Context
import android.net.Uri
import android.os.PowerManager
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class QuietThermalPolicyTest {
    private class ControlledAnalyzer(context: Context) : VisualQuietAnalyzer(context) {
        var temperature: Int? = 94_999
        var sensor: String? = "soc-thmzone"
        var battery: Int? = 419
        var thermalStatus: Int? = PowerManager.THERMAL_STATUS_MODERATE
        var verifiedFirmware = false
        var memoryRefusal: QuietAnalysisReason? = null
        val probes = mutableListOf<String>()

        override fun batteryResourceRefusal(): QuietAnalysisReason? {
            probes += "battery"
            return VisualQuietAnalyzer.batteryTemperatureRefusal(battery)
        }
        override fun systemResourceRefusal(): QuietAnalysisReason? {
            probes += "system"
            return VisualQuietAnalyzer.systemThermalRefusal(thermalStatus, verifiedFirmware)
        }
        override fun memoryResourceRefusal(): QuietAnalysisReason? {
            probes += "memory"
            return memoryRefusal
        }
        override fun cpuResourceRefusal(phase: QuietThermalPhase): QuietAnalysisReason? {
            probes += "cpu-$phase"
            return VisualQuietAnalyzer.cpuTemperatureRefusal(sensor, temperature, phase)
        }
    }

    private fun analyzer() = ControlledAnalyzer(ApplicationProvider.getApplicationContext())
    private fun scene() = IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT) {
        if (it % QUIET_GRID_WIDTH < 80) 50 else 100
    }
    private fun completeTracker() = QuietFrameTracker().also { tracker ->
        val frame = scene()
        repeat(301) { tracker.accept(frame, it * 100_000L) }
    }

    @Test fun admissionAndContinuationUseDistinctExactThresholds() {
        val admission = QuietThermalPhase.ADMISSION
        val decoding = QuietThermalPhase.DECODING
        for (value in listOf(10_000, 85_000, 94_999)) {
            assertNull(VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, admission))
            assertNull(VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, decoding))
        }
        for (value in listOf(95_000, 95_001, 99_999)) {
            assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_GUARD,
                VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, admission))
            assertNull(VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, decoding))
        }
        for (phase in QuietThermalPhase.entries) for (value in listOf(100_000, 100_001, 150_000)) {
            assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT,
                VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, phase))
        }
        for (phase in QuietThermalPhase.entries) {
            for (value in listOf(null, 9_999, 150_001)) assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE,
                VisualQuietAnalyzer.cpuTemperatureRefusal("soc-thmzone", value, phase))
            assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE,
                VisualQuietAnalyzer.cpuTemperatureRefusal("battery", 90_000, phase))
        }
    }

    @Test fun moderateIsPermittedButSevereAndUnknownSystemStatusesStillRefuse() {
        for (status in PowerManager.THERMAL_STATUS_NONE..PowerManager.THERMAL_STATUS_MODERATE)
            assertNull(VisualQuietAnalyzer.systemThermalRefusal(status))
        for (status in PowerManager.THERMAL_STATUS_SEVERE..PowerManager.THERMAL_STATUS_SHUTDOWN)
            assertEquals(QuietAnalysisReason.SYSTEM_THERMAL_GUARD, VisualQuietAnalyzer.systemThermalRefusal(status))
        for (status in listOf(null, -1, PowerManager.THERMAL_STATUS_SHUTDOWN + 1))
            assertEquals(QuietAnalysisReason.SYSTEM_THERMAL_UNAVAILABLE, VisualQuietAnalyzer.systemThermalRefusal(status))
    }

    @Test fun verifiedQ30FirmwarePermitsOnlySevereAndStillUsesMeasuredCpuAndOtherResources() {
        val analyzer = analyzer()
        analyzer.thermalStatus = PowerManager.THERMAL_STATUS_SEVERE
        analyzer.temperature = 79_735
        assertEquals(QuietAnalysisReason.SYSTEM_THERMAL_GUARD, analyzer.resourceRefusal())
        analyzer.verifiedFirmware = true
        assertNull(analyzer.resourceRefusal())
        analyzer.temperature = 95_000
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_GUARD, analyzer.resourceRefusal())
        assertNull(analyzer.resourceRefusal(QuietThermalPhase.DECODING))
        analyzer.temperature = 100_000
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT,
            analyzer.resourceRefusal(QuietThermalPhase.DECODING))
        analyzer.temperature = null
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE, analyzer.resourceRefusal())
        analyzer.temperature = 79_735
        analyzer.battery = 420
        assertEquals(QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD, analyzer.resourceRefusal())
        analyzer.battery = 419
        analyzer.memoryRefusal = QuietAnalysisReason.MEMORY_GUARD
        assertEquals(QuietAnalysisReason.MEMORY_GUARD, analyzer.resourceRefusal())
        for (status in PowerManager.THERMAL_STATUS_CRITICAL..PowerManager.THERMAL_STATUS_SHUTDOWN)
            assertEquals(QuietAnalysisReason.SYSTEM_THERMAL_GUARD,
                VisualQuietAnalyzer.systemThermalRefusal(status, true))
        for (status in listOf(null, -1, 7)) assertEquals(QuietAnalysisReason.SYSTEM_THERMAL_UNAVAILABLE,
            VisualQuietAnalyzer.systemThermalRefusal(status, true))
    }

    @Test fun firmwareCompatibilityRequiresEveryVerifiedReleaseIdentityField() {
        val display = "QC_Q30_1025HJYEFPSL_U_Q30_MB_1025_CarCharger_user_2026060112"
        val model = "Q30_1025HJYEFPSL_U"
        val hardware = "uis7861_6h10"
        val prefix = "UNISOC/uis7861_6h10_Natv/uis7861_6h10:14/UP1A.231005.007/"
        val fingerprint = prefix + "2026060112:user/release-keys"
        fun matches(d: String? = display, m: String? = model, h: String? = hardware,
                    sdk: Int = 34, f: String? = fingerprint) = QuietFirmwareThermalProfile.matches(d, m, h, sdk, f)
        assertTrue(matches())
        for (value in listOf(null, "", "$display-new")) assertFalse(matches(d = value))
        for (value in listOf(null, "", "$model-new")) assertFalse(matches(m = value))
        for (value in listOf(null, "", "$hardware-new")) assertFalse(matches(h = value))
        for (value in listOf(33, 35)) assertFalse(matches(sdk = value))
        for (value in listOf(null, "", "other/" + fingerprint, prefix + ":user/release-keys",
            prefix + "2026060112:userdebug/test-keys", prefix + "bad/:user/release-keys"))
            assertFalse(matches(f = value))
    }

    @Test fun policyAndOrderedResourceRefusalsHappenBeforeOpeningVideo() = runTest {
        val analyzer = analyzer()
        val uri = Uri.parse("content://invalid/private-test.ts")
        assertEquals("policy_not_permitted", analyzer.analyze(uri) { false }.getString("reason"))
        assertTrue(analyzer.probes.isEmpty())
        analyzer.battery = 420
        assertEquals("battery_temperature_guard", analyzer.analyze(uri) { true }.getString("reason"))
        assertEquals(listOf("battery"), analyzer.probes)
        analyzer.probes.clear(); analyzer.battery = 419; analyzer.thermalStatus = PowerManager.THERMAL_STATUS_SEVERE
        assertEquals("system_thermal_guard", analyzer.analyze(uri) { true }.getString("reason"))
        assertEquals(listOf("battery", "system"), analyzer.probes)
        analyzer.probes.clear(); analyzer.thermalStatus = PowerManager.THERMAL_STATUS_MODERATE
        analyzer.memoryRefusal = QuietAnalysisReason.MEMORY_GUARD
        assertEquals("memory_guard", analyzer.analyze(uri) { true }.getString("reason"))
        assertEquals(listOf("battery", "system", "memory"), analyzer.probes)
        analyzer.probes.clear(); analyzer.memoryRefusal = null; analyzer.temperature = 95_000
        val result = analyzer.analyze(uri) { true }
        assertEquals("cpu_temperature_guard", result.getString("reason"))
        assertEquals(listOf("battery", "system", "memory", "cpu-ADMISSION"), analyzer.probes)
        assertEquals(0, result.getInt("decoded_frames"))
        assertFalse(result.toString().contains("private-test"))
        assertFalse(ClipTransferPolicy.validVisual(result))
    }

    @Test fun warmingDuringDecodeUsesRunningLimitAndNeverAddsBinderProbesPerFrame() {
        val analyzer = analyzer()
        assertNull(analyzer.resourceRefusal()) // Admitted below95; all other guards passed.
        var at = 0L
        val gate = analyzer.decodeResourceGate { at }
        val tracker = QuietFrameTracker()
        var reads = 0
        analyzer.probes.clear()
        val frame = scene()
        for ((index, temperature) in listOf(94_999, 95_000, 99_999).withIndex()) {
            analyzer.temperature = temperature
            at = index * 333L
            assertNull(gate.periodicRefusal())
            assertNull(analyzer.acceptDecodedFrame(tracker, gate, index * 100_000L) { reads++; frame })
        }
        assertEquals(3, reads)
        assertEquals(listOf("cpu-DECODING", "cpu-DECODING", "cpu-DECODING"), analyzer.probes)
        at = 1_000
        assertNull(gate.periodicRefusal())
        assertEquals(listOf("battery", "system", "memory", "cpu-DECODING"), analyzer.probes.takeLast(4))
        val fullProbeCount = analyzer.probes.count { it == "battery" }
        repeat(100) { assertNull(gate.periodicRefusal()) }
        assertEquals(fullProbeCount, analyzer.probes.count { it == "battery" })
        analyzer.temperature = 100_000
        val refusal = analyzer.acceptDecodedFrame(tracker, gate, 300_000) { reads++; frame }
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT, refusal)
        assertEquals(3, reads) // Hot frame was refused before image access or luma work.
        assertEquals(3, tracker.frames)
        val report = tracker.proof(4, false, false, "unavailable", refusal)
        assertEquals("cpu_temperature_hard_limit", report.getString("reason"))
        assertFalse(ClipTransferPolicy.validVisual(report))
        // A new analysis is an admission again: warming tolerance cannot leak across clips.
        analyzer.temperature = 95_000
        assertEquals(QuietAnalysisReason.CPU_TEMPERATURE_GUARD, analyzer.resourceRefusal())
        analyzer.temperature = 94_999
        assertNull(analyzer.resourceRefusal())
    }

    @Test fun periodicContinuationGuardStillStopsBatteryMemorySystemAndUnknownCpu() {
        for (reason in listOf(QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD,
            QuietAnalysisReason.MEMORY_GUARD, QuietAnalysisReason.SYSTEM_THERMAL_GUARD,
            QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE)) {
            val analyzer = analyzer()
            var at = 0L
            val gate = analyzer.decodeResourceGate { at }
            when (reason) {
                QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD -> analyzer.battery = 420
                QuietAnalysisReason.MEMORY_GUARD -> analyzer.memoryRefusal = reason
                QuietAnalysisReason.SYSTEM_THERMAL_GUARD -> analyzer.thermalStatus = PowerManager.THERMAL_STATUS_SEVERE
                else -> analyzer.temperature = null
            }
            at = 999
            assertNull(gate.periodicRefusal())
            at = 1_000
            assertEquals(reason, gate.periodicRefusal())
        }
    }

    @Test fun finalEosChecksAllResourcesEvenWithoutAnotherPayloadFrame() {
        val tracker = completeTracker()
        assertTrue(ClipTransferPolicy.validVisual(tracker.proof(301, true, true)))
        for (reason in listOf(QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT,
            QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE, QuietAnalysisReason.SYSTEM_THERMAL_GUARD,
            QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD, QuietAnalysisReason.MEMORY_GUARD)) {
            val analyzer = analyzer()
            val gate = analyzer.decodeResourceGate { 0L }
            when (reason) {
                QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT -> analyzer.temperature = 100_000
                QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE -> analyzer.temperature = null
                QuietAnalysisReason.SYSTEM_THERMAL_GUARD -> analyzer.thermalStatus = PowerManager.THERMAL_STATUS_SEVERE
                QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD -> analyzer.battery = 420
                else -> analyzer.memoryRefusal = reason
            }
            val report = analyzer.finalProof(tracker, gate, 301, true, true, { true })
            assertEquals("unavailable", report.getString("status"))
            assertEquals(reason.wire, report.getString("reason"))
            assertFalse(report.getBoolean("decoder_eos"))
            assertFalse(ClipTransferPolicy.validVisual(report))
        }
        val analyzer = analyzer()
        analyzer.temperature = 99_999
        assertTrue(ClipTransferPolicy.validVisual(analyzer.finalProof(tracker,
            analyzer.decodeResourceGate { 0L }, 301, true, true, { true })))
        analyzer.probes.clear()
        val cancelled = analyzer.finalProof(tracker, analyzer.decodeResourceGate { 0L }, 301, true, true, { false })
        assertEquals("policy_not_permitted", cancelled.getString("reason"))
        assertTrue(analyzer.probes.isEmpty())
        assertFalse(ClipTransferPolicy.validVisual(cancelled))
    }
}
