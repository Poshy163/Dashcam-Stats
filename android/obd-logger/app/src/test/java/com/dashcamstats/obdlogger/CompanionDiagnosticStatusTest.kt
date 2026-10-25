package com.dashcamstats.obdlogger

import java.io.File
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class CompanionDiagnosticStatusTest {
    @Test fun advisoryDiagnosticsCannotGrantAnIgnitionEdgeOrExposePrivateAuthorityTokens() {
        val at = "2026-10-25T04:00:00Z"
        val change = TrialSettingChange(2, true, null, "operator", at)
        val trial = TrialSettingsSnapshot(true, null, 2, "operator", at, "ready", null, true,
            listOf(change), "private-authority-token")
        val startup = BootDiagnosticEntry("kernel_boot", at, 27, 1, "reboot", "sys_boot_reason",
            "not_recorded", null, BuildConfig.VERSION_CODE, BuildConfig.BUILD_GIT_SHA)
        val report = StatusPublisher.buildStatusJson(PublicStatus("parked", true,
            trialSettings = trial.json(), restartEvidence = BootDiagnosticsSnapshot("ready", startup, listOf(startup))), 0)
        assertEquals(1, report.getJSONObject("trial_settings").getInt("schema_version"))
        assertTrue(report.getJSONObject("trial_settings").getBoolean("enabled"))
        assertEquals("unknown", report.getJSONObject("restart_evidence").getString("cause_confidence"))
        assertTrue(report.isNull("sleep_deadline_evidence"))
        assertFalse(report.getBoolean("acc_state_known"))
        assertFalse(report.getBoolean("engine_running"))
        assertFalse(report.getBoolean("ingestion_sleep_hold_known"))
        assertFalse(report.toString().contains("private-authority-token"))
        assertTrue(report.getJSONObject("restart_evidence").keys().asSequence().none { it in setOf("boot_id", "process_token", "pid") })
        // Optional cross-language verification writes only this public synthetic status.
        System.getenv("OBD_CONTRACT_OUTPUT")?.let { path -> File(path).writeText(report.toString()) }
    }

    @Test fun startupWithoutDiagnosticEvidencePublishesExplicitNullRatherThanInventingHistory() {
        val report = StatusPublisher.buildStatusJson(PublicStatus("starting", true), 0)
        assertTrue(report.isNull("trial_settings"))
        assertTrue(report.isNull("restart_evidence"))
        assertTrue(report.isNull("sleep_deadline_evidence"))
    }
}
