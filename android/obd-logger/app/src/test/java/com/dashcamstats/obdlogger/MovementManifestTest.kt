package com.dashcamstats.obdlogger

import android.content.Context
import android.app.Application
import android.Manifest
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf

@RunWith(RobolectricTestRunner::class)
@OptIn(ExperimentalCoroutinesApi::class)
class MovementManifestTest {
    private fun point(at: Long, kind: String = "acc", acc: Boolean? = false,
                      speed: Double? = null, accuracy: Double? = null, age: Long? = null,
                      hacc: Double? = null, session: String = "session", segment: String = "segment") =
        MotionObservation(at, at, session, segment, kind, acc, speed, accuracy, age, hacc)

    @Test fun accOnDoesNotProveMovementAndMissingSpeedDoesNotBecomeStationary() {
        val rows = (0L..60_000L step 5_000).map { point(it, acc = true) }
        val result = MovementClassifier.assess(0, 60_000, rows)
        assertEquals("unknown", result.classification)
        assertEquals(0, result.evidence.getInt("valid_speed_samples"))
    }

    @Test fun offCoverageIsAnExplicitWeakCandidateNotStationaryProof() {
        val result = MovementClassifier.assess(0, 60_000,
            (0L..60_000L step 5_000).map { point(it) })
        assertEquals("acc_off_candidate", result.classification)
        assertEquals(60_000L, result.evidence.getLong("acc_off_ms"))
        assertTrue(result.reasons.contains("report_only"))
    }

    @Test fun aSingleValidatedPositiveObdSpeedOverridesParkedEvidence() {
        val rows = (0L..60_000L step 5_000).map { point(it) } +
            point(30_000, "obd", speed = 1.0)
        val result = MovementClassifier.assess(0, 60_000, rows, locked = true)
        assertEquals("moving_observed", result.classification)
        assertTrue(result.reasons.contains("locked_clip"))
        assertEquals(1, result.evidence.getInt("moving_samples"))
    }

    @Test fun validZeroSamplesNeedCoverageAndAreOnlySampledStationary() {
        val rows = (0L..60_000L step 5_000).map { point(it, "obd", speed = 0.0) }
        assertEquals("sampled_stationary", MovementClassifier.assess(0, 60_000, rows).classification)
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, rows.take(3)).classification)
    }

    @Test fun unknownAccReadAndAnUnobservedPollBothBreakCoverage() {
        val rows = (0L..60_000L step 5_000).map { point(it, acc = if (it == 30_000L) null else false) }
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, rows).classification)
        val missing = rows.filter { it.wallMs != 30_000L }
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, missing).classification)
    }

    @Test fun gpsJitterObservedOnTheActualParkedUnitDoesNotBecomeMovement() {
        val rows = listOf(point(10_000, "gps", speed = 0.297, accuracy = 0.36, age = 150, hacc = 2.213),
            point(15_000, "gps", speed = 0.4, accuracy = 0.36, age = 150, hacc = 2.213))
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, rows).classification)
    }

    @Test fun gpsRequiresTwoConsecutiveConfidentFreshObservations() {
        val first = point(10_000, "gps", speed = 10.0, accuracy = 0.3, age = 100, hacc = 5.0)
        val second = point(15_000, "gps", speed = 11.0, accuracy = 0.3, age = 100, hacc = 5.0)
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, listOf(first)).classification)
        assertEquals("moving_observed", MovementClassifier.assess(0, 60_000, listOf(first, second)).classification)
        assertFalse(MovementClassifier.confidentGps(second.copy(ageMs = 100_000)))
        assertFalse(MovementClassifier.confidentGps(second.copy(horizontalAccuracy = 100.0)))
        assertFalse(MovementClassifier.confidentGps(second.copy(accuracy = null)))
    }

    @Test fun restartAndClockSegmentChangesCannotProvideContinuousCoverage() {
        val rows = (0L..60_000L step 5_000).map {
            point(it, session = if (it < 30_000) "first" else "second")
        }
        val result = MovementClassifier.assess(0, 60_000, rows)
        assertEquals("unknown", result.classification)
        assertTrue(result.reasons.contains("clock_ambiguous"))
        val segments = rows.map { it.copy(sessionId = "same", segmentId = it.sessionId) }
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, segments).classification)
    }

    @Test fun oldClipsOrOrphanPartialsCannotInheritCurrentStationaryEvidence() {
        val rows = (0L..60_000L step 5_000).map { point(it) }
        assertEquals("unknown", MovementClassifier.assess(null, null, rows).classification)
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, rows, orphan = true).classification)
        assertEquals("unknown", MovementClassifier.assess(0, 60_000, emptyList()).classification)
    }

    @Test fun overlappingSourcesUseUnionCoverageRatherThanSummingDouble() {
        val rows = (0L..60_000L step 5_000).flatMap {
            listOf(point(it), point(it, "obd", speed = 0.0))
        }
        val result = MovementClassifier.assess(0, 60_000, rows)
        assertEquals(60_000L, result.coverage.getLong("acc_ms"))
        assertEquals(60_000L, result.coverage.getLong("speed_ms"))
        assertEquals(0L, result.coverage.getLong("unknown_ms"))
    }

    @Test fun wallClockAdjustmentStartsANewSegmentButOrdinaryTimeDoesNot() {
        var wall = 1_000_000L; var elapsed = 10L
        val clock = MotionClock("session", { wall }, { elapsed })
        val initial = clock.observation("acc", false)
        wall += 5_000; elapsed += 5_000
        assertEquals(initial.segmentId, clock.observation("acc", false).segmentId)
        wall -= 30_000; elapsed += 5_000
        assertNotEquals(initial.segmentId, clock.observation("acc", false).segmentId)
    }

    @Test fun privateJournalSurvivesRestartAndStoresUnknownReadsWithoutCoordinates() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        val first = MotionJournal(context, backgroundScope)
        first.acc(false); first.acc(null); first.speed(30.0)
        first.gps(0.297, 0.36, 150, 2.213)
        first.gps(10.0, 0.3, 100_000, 2.0)
        advanceTimeBy(MOTION_BATCH_WINDOW_MS + 100); runCurrent()
        val source = first.sourceId
        first.stop(); first.close()
        val second = MotionJournal(context, backgroundScope)
        assertEquals(source, second.sourceId)
        assertNotEquals(first.sessionId, second.sessionId)
        val rows = second.between(System.currentTimeMillis() - 60_000, System.currentTimeMillis() + 60_000)
        assertEquals(5, rows.size)
        assertTrue(rows.any { it.kind == "acc" && it.acc == null })
        assertTrue(rows.last().speed == null)
        val columns = second.readableDatabase.rawQuery("PRAGMA table_info(observations)", null).use { cursor ->
            buildList { while (cursor.moveToNext()) add(cursor.getString(1)) }
        }
        assertFalse(columns.any { it.contains("latitude") || it.contains("longitude") })
        second.stop(); second.close()
    }

    @Test fun closingJournalDrainsAcceptedObservationsWithoutWaitingForBatchWindow() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        val journal = MotionJournal(context, backgroundScope)
        journal.acc(false); journal.speed(1.0); journal.acc(null)
        val before = testScheduler.currentTime
        journal.stop()
        runCurrent()
        assertEquals(before, testScheduler.currentTime)
        val rows = journal.between(System.currentTimeMillis() - 60_000, System.currentTimeMillis() + 60_000)
        assertEquals(3, rows.size)
        assertTrue(rows.any { it.kind == "obd" && it.speed == 1.0 })
        journal.close()
    }

    @Test fun actualQueueOverflowCannotResurrectStationaryCoverageAfterRestart() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.deleteDatabase("clip-movement.db")
        val journal = MotionJournal(context, backgroundScope)
        // The actor has not run: fill its actual bounded channel, then drop a positive speed.
        repeat(256) { journal.acc(false) }
        journal.speed(30.0)
        assertTrue(journal.failed)
        val gapAt = System.currentTimeMillis()
        val zeros = listOf(point(0, "obd", speed = 0.0), point(10_000, "obd", speed = 0.0))
        assertEquals("sampled_stationary", MovementClassifier.assess(0, 10_000, zeros).classification)
        assertEquals("unknown", MovementClassifier.assess(0, 10_000, zeros,
            evidenceHealthy = journal.evidenceHealthy(gapAt - 5_000, gapAt + 5_000)).classification)
        journal.stop(); runCurrent(); journal.close()
        val reopened = MotionJournal(context, backgroundScope)
        assertFalse(reopened.evidenceHealthy(gapAt - 5_000, gapAt + 5_000))
        val afterRestart = MovementClassifier.assess(0, 10_000, zeros,
            evidenceHealthy = reopened.evidenceHealthy(gapAt - 5_000, gapAt + 5_000))
        assertEquals("unknown", afterRestart.classification)
        assertEquals(10_000L, afterRestart.coverage.getLong("unknown_ms"))
        assertTrue(reopened.evidenceHealthy(gapAt + 60_000, gapAt + 120_000))
        reopened.stop(); reopened.close()
    }

    @Test fun statusAdvertisesReportOnlyCapabilityWithoutOptionalPermissions() {
        val status = StatusPublisher.buildStatusJson(PublicStatus("parked", true,
            movementState = "directory_required", movementGpsState = "permission_required"), 0)
        assertTrue(status.getJSONArray("capabilities").toString().contains("clip_movement_manifest_v1"))
        assertEquals("directory_required", status.getString("movement_state"))
        assertTrue(status.getBoolean("movement_report_only"))
    }

    @Test fun passiveGpsIsOptInAndWhileInUsePermissionCannotEnableBackgroundStartup() = runTest {
        val context = ApplicationProvider.getApplicationContext<Application>()
        val preferences = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        preferences.edit().putBoolean(MOVEMENT_GPS_ENABLED, false).commit()
        val journal = MotionJournal(context, backgroundScope)
        val observer = PassiveMotionObserver(context, journal)
        var promotions = 0
        observer.start(true) { promotions += 1 }
        assertEquals("disabled", observer.state)
        preferences.edit().putBoolean(MOVEMENT_GPS_ENABLED, true).commit()
        shadowOf(context).denyPermissions(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        observer.start(false) { promotions += 1 }
        assertEquals("permission_required", observer.state)
        assertEquals(0, promotions)
        shadowOf(context).grantPermissions(Manifest.permission.ACCESS_FINE_LOCATION)
        MovementActivityVisibility.visible = false
        assertFalse(observer.permitted(true))
        assertFalse(observer.permitted(false))
        MovementActivityVisibility.visible = true
        assertTrue(observer.permitted(true))
        shadowOf(context).grantPermissions(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        MovementActivityVisibility.visible = false
        assertTrue(observer.permitted(false))
        observer.stop(); journal.stop(); journal.close()
    }
}
