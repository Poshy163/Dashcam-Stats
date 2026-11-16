package com.dashcamstats.obdlogger

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
@OptIn(ExperimentalCoroutinesApi::class)
class SleepWindowControllerTest {
    @Test
    fun unreadableLeaseCannotBecomeCompletionOrAuthorizeParkedWork() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val property = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS)
        var on = true
        val controller = AdaptiveSleepWindowController(
            context, this, property,
            object : AccStateAccessor { override fun readAccOn(): Boolean = on },
            elapsedMillis = { testScheduler.currentTime },
            bootIdentity = { SleepBootIdentity("01234567-1234-1234-1234-012345678901", 12) },
            deadlineStore = null,
        )
        try {
            controller.start()
            controller.setIngestionRequestActive(false)
            runCurrent()
            on = false
            advanceTimeBy(5_000); runCurrent()
            val original = controller.snapshot().deadline!!
            assertEquals(IDLE_SLEEP_WINDOW_SECONDS, original.windowSeconds)

            // A lease arriving after OFF may change the property but cannot extend
            // the five-minute native countdown already captured for this parking.
            controller.setIngestionRequestActive(true)
            runCurrent()
            assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, property.readSeconds())

            controller.setIngestionRequestActive(null)
            // Optional indexing sees uncertainty even before the policy worker runs.
            assertFalse(controller.snapshot().ingestionStateKnown)
            assertEquals(ClipIndexWorkMode.INGESTION, clipIndexWorkMode(true, false,
                controller.snapshot().ingestionStateKnown, controller.snapshot().ingestionRequestActive))
            runCurrent()
            assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, property.readSeconds())
            assertFalse(controller.snapshot().ingestionStateKnown)
            assertTrue(controller.snapshot().ingestionRequestActive)

            controller.setIngestionRequestActive(true)
            runCurrent()
            assertTrue(controller.snapshot().ingestionStateKnown)
            assertEquals(listOf(ACTIVE_SLEEP_WINDOW_SECONDS), property.writes)

            // Only a confirmed absence after the unknown read completes the valid lease.
            controller.setIngestionRequestActive(null)
            runCurrent()
            controller.setIngestionRequestActive(false)
            runCurrent()
            assertEquals(IDLE_SLEEP_WINDOW_SECONDS, property.readSeconds())
            assertTrue(controller.snapshot().ingestionStateKnown)
            assertFalse(controller.snapshot().ingestionRequestActive)
            assertEquals(original.offLowerElapsedMillis, controller.snapshot().deadline?.offLowerElapsedMillis)
            assertEquals(original.offUpperElapsedMillis, controller.snapshot().deadline?.offUpperElapsedMillis)
            assertEquals(original.windowSeconds, controller.snapshot().deadline?.windowSeconds)
        } finally { controller.close(); runCurrent() }
    }

    @Test
    fun unknownStartupAndQueuedCompletionWaitForConfirmedAbsence() = runTest {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val property = FakeProperty(ACTIVE_SLEEP_WINDOW_SECONDS)
        val controller = AdaptiveSleepWindowController(
            context, this, property,
            object : AccStateAccessor { override fun readAccOn(): Boolean = false },
            elapsedMillis = { testScheduler.currentTime },
            bootIdentity = { SleepBootIdentity("01234567-1234-1234-1234-012345678901", 12) },
            deadlineStore = null,
        )
        try {
            controller.start()
            controller.setIngestionRequestActive(null)
            runCurrent()
            assertFalse(controller.snapshot().ingestionStateKnown)
            assertEquals("awaiting_ingestion_state", controller.snapshot().policy)
            assertTrue(property.writes.isEmpty())
            controller.setIngestionRequestActive(false)
            runCurrent()
            assertEquals(IDLE_SLEEP_WINDOW_SECONDS, property.readSeconds())

            controller.setIngestionRequestActive(true)
            runCurrent()
            val before = property.writes.toList()
            controller.setIngestionRequestActive(false)
            controller.setIngestionRequestActive(null)
            runCurrent()
            assertEquals(before, property.writes)
            assertFalse(controller.snapshot().ingestionStateKnown)
            assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, property.readSeconds())
            controller.setIngestionRequestActive(false)
            runCurrent()
            assertEquals(IDLE_SLEEP_WINDOW_SECONDS, property.readSeconds())
            assertTrue(controller.snapshot().ingestionStateKnown)
        } finally { controller.close(); runCurrent() }
    }

    @Test
    fun unknownOrNewlyActiveLeaseOverridesAStaleCompletionEvent() {
        for (event in listOf(SleepWindowEvent.INGESTION_ENDED, SleepWindowEvent.INGESTION_STATE_UNKNOWN)) {
            assertEquals("awaiting_ingestion_state", sleepWindowCommand(event,
                wifiConnected = false, ingestionStateKnown = false, ingestionRequestActive = false,
                ingestionCompleted = true, accStateKnown = true, accOn = false).policy)
            assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, sleepWindowCommand(event,
                wifiConnected = true, ingestionStateKnown = false, ingestionRequestActive = false,
                ingestionCompleted = true, accStateKnown = true, accOn = false).targetSeconds)
            assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, sleepWindowCommand(event,
                wifiConnected = false, ingestionStateKnown = true, ingestionRequestActive = true,
                ingestionCompleted = true, accStateKnown = true, accOn = false).targetSeconds)
        }
        assertEquals(IDLE_SLEEP_WINDOW_SECONDS, sleepWindowCommand(SleepWindowEvent.INGESTION_ABSENCE_OBSERVED,
            wifiConnected = true, ingestionStateKnown = true, ingestionRequestActive = false,
            ingestionCompleted = true, accStateKnown = true, accOn = false).targetSeconds)
    }

    @Test
    fun stateTransitionsSelectManagedOrServerOwnedWindows() {
        assertEquals(
            IDLE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.STARTED,
                wifiConnected = false,
                ingestionStateKnown = true,
                ingestionRequestActive = false,
            ).targetSeconds,
        )
        assertEquals(
            IDLE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.ACC_BECAME_ON,
                wifiConnected = false,
                ingestionStateKnown = true,
                ingestionRequestActive = false,
                accStateKnown = true,
                accOn = true,
            ).targetSeconds,
        )
        assertEquals(
            ACTIVE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.WIFI_BECAME_PRESENT,
                wifiConnected = true,
                ingestionStateKnown = true,
                ingestionRequestActive = false,
            ).targetSeconds,
        )
        assertEquals(
            ACTIVE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.WIFI_LOST,
                wifiConnected = false,
                ingestionStateKnown = true,
                ingestionRequestActive = true,
            ).targetSeconds,
        )
        val completedWithAccOff = sleepWindowCommand(
            SleepWindowEvent.INGESTION_ENDED,
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            ingestionCompleted = true,
            accStateKnown = true,
            accOn = false,
        )
        assertEquals("managed_idle", completedWithAccOff.policy)
        assertEquals(IDLE_SLEEP_WINDOW_SECONDS, completedWithAccOff.targetSeconds)
        assertEquals(
            ACTIVE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.INGESTION_ENDED,
                wifiConnected = true,
                ingestionStateKnown = true,
                ingestionRequestActive = false,
                ingestionCompleted = true,
                accStateKnown = true,
                accOn = true,
            ).targetSeconds,
        )
        assertEquals(
            IDLE_SLEEP_WINDOW_SECONDS,
            sleepWindowCommand(
                SleepWindowEvent.INGESTION_ENDED,
                wifiConnected = false,
                ingestionStateKnown = true,
                ingestionRequestActive = false,
            ).targetSeconds,
        )
    }

    @Test
    fun matchingReadbackDoesNotWrite() {
        val property = FakeProperty(ACTIVE_SLEEP_WINDOW_SECONDS)

        val evidence = SleepWindowReconciler(property).reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
        )

        assertTrue(evidence.verified)
        assertNull(evidence.error)
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, evidence.observedSeconds)
        assertTrue(property.writes.isEmpty())
    }

    @Test
    fun changedWindowIsWrittenAndReadBack() {
        val property = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS)

        val evidence = SleepWindowReconciler(property).reconcile(
            wifiConnected = false,
            ingestionStateKnown = true,
            ingestionRequestActive = true,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
        )

        assertEquals(listOf(ACTIVE_SLEEP_WINDOW_SECONDS), property.writes)
        assertTrue(evidence.verified)
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, evidence.observedSeconds)
        assertTrue(evidence.ingestionRequestActive)
    }

    @Test
    fun refusedOrMismatchedPropertyIsEvidenceNotAnException() {
        val refused = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS, acceptWrites = false)
        val refusedEvidence = SleepWindowReconciler(refused).reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
        )
        assertFalse(refusedEvidence.verified)
        assertEquals("sleep countdown update was refused", refusedEvidence.error)

        val ignored = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS, retainOldValue = true)
        val ignoredEvidence = SleepWindowReconciler(ignored).reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
        )
        assertFalse(ignoredEvidence.verified)
        assertEquals(
            "sleep countdown readback did not match the requested value",
            ignoredEvidence.error,
        )
    }

    @Test
    fun unavailablePropertyAccessDoesNotExposeTheException() {
        val property = object : SleepWindowPropertyAccessor {
            override fun readSeconds(): Int? = throw IllegalStateException("secret vendor output")

            override fun writeSeconds(seconds: Int): Boolean =
                throw IllegalStateException("secret vendor output")
        }

        val evidence = SleepWindowReconciler(property).reconcile(
            wifiConnected = false,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_idle", IDLE_SLEEP_WINDOW_SECONDS),
        )

        assertFalse(evidence.verified)
        assertEquals("sleep countdown update was refused", evidence.error)
        assertFalse(evidence.toString().contains("secret"))
    }

    @Test
    fun serverOwnedTransitionObservesWithoutWriting() {
        val property = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS)

        val evidence = SleepWindowReconciler(property).reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("server_owned", null),
        )

        assertEquals("server_owned", evidence.policy)
        assertNull(evidence.targetSeconds)
        assertEquals(IDLE_SLEEP_WINDOW_SECONDS, evidence.observedSeconds)
        assertFalse(evidence.verified)
        assertNull(evidence.error)
        assertTrue(property.writes.isEmpty())
    }

    @Test
    fun startupWithoutWifiWaitsUntilIngestionStateIsKnown() {
        val command = sleepWindowCommand(
            SleepWindowEvent.STARTED,
            wifiConnected = false,
            ingestionStateKnown = false,
            ingestionRequestActive = false,
        )

        assertEquals("awaiting_ingestion_state", command.policy)
        assertNull(command.targetSeconds)
    }

    @Test
    fun accParserKeepsUnknownDistinctFromOff() {
        assertEquals(true, parseAccState("1\n"))
        assertEquals(false, parseAccState("off"))
        assertNull(parseAccState("null"))
        assertNull(parseAccState("permission denied"))
    }

    @Test
    fun wifiStartupAppliesActiveWindowBeforeKnownAbsentBecomesServerOwned() {
        val property = FakeProperty(IDLE_SLEEP_WINDOW_SECONDS)
        val reconciler = SleepWindowReconciler(property)

        val startup = sleepWindowCommand(
            SleepWindowEvent.STARTED,
            wifiConnected = true,
            ingestionStateKnown = false,
            ingestionRequestActive = false,
        )
        val startupEvidence = reconciler.reconcile(
            wifiConnected = true,
            ingestionStateKnown = false,
            ingestionRequestActive = false,
            command = startup,
        )
        val absence = sleepWindowCommand(
            SleepWindowEvent.INGESTION_ABSENCE_OBSERVED,
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
        )
        val absenceEvidence = reconciler.reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = absence,
        )

        assertTrue(startupEvidence.verified)
        assertEquals(listOf(ACTIVE_SLEEP_WINDOW_SECONDS), property.writes)
        assertEquals("managed_active", absenceEvidence.policy)
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, absenceEvidence.targetSeconds)
        assertEquals(ACTIVE_SLEEP_WINDOW_SECONDS, absenceEvidence.observedSeconds)
    }

    @Test
    fun transientFailureRetriesAndSucceedsWithoutAnotherStateTransition() = runTest {
        val property = ScriptedProperty(IDLE_SLEEP_WINDOW_SECONDS, refusedWrites = 1)
        val delays = mutableListOf<Long>()

        val evidence = SleepWindowRetryer(
            SleepWindowReconciler(property),
            retryDelaysMillis = listOf(10L, 20L),
        ).reconcile(
            wifiConnected = true,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
            waitBeforeRetry = {
                delays += it
                true
            },
        )

        assertTrue(evidence.verified)
        assertNull(evidence.error)
        assertEquals(listOf(10L), delays)
        assertEquals(
            listOf(ACTIVE_SLEEP_WINDOW_SECONDS, ACTIVE_SLEEP_WINDOW_SECONDS),
            property.writes,
        )
    }

    @Test
    fun permanentFailureStopsAtRetryLimitAndRemainsVisible() = runTest {
        val property = ScriptedProperty(ACTIVE_SLEEP_WINDOW_SECONDS, refusedWrites = Int.MAX_VALUE)
        val delays = mutableListOf<Long>()
        val published = mutableListOf<SleepWindowEvidence>()

        val evidence = SleepWindowRetryer(
            SleepWindowReconciler(property),
            retryDelaysMillis = listOf(10L, 20L),
        ).reconcile(
            wifiConnected = false,
            ingestionStateKnown = true,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_idle", IDLE_SLEEP_WINDOW_SECONDS),
            waitBeforeRetry = {
                delays += it
                true
            },
            publish = { next, _, _ -> published += next },
        )

        assertFalse(evidence.verified)
        assertEquals("sleep countdown update was refused", evidence.error)
        assertEquals(listOf(10L, 20L), delays)
        assertEquals(3, property.writes.size)
        assertEquals(3, published.size)
        assertEquals(evidence, published.last())
    }

    @Test
    fun newerStateEventInvalidatesBackoffBeforeAnotherPropertyWrite() = runTest {
        val property = ScriptedProperty(IDLE_SLEEP_WINDOW_SECONDS, refusedWrites = Int.MAX_VALUE)
        var waits = 0

        val evidence = SleepWindowRetryer(
            SleepWindowReconciler(property),
            retryDelaysMillis = listOf(10L, 20L),
        ).reconcile(
            wifiConnected = true,
            ingestionStateKnown = false,
            ingestionRequestActive = false,
            command = SleepWindowCommand("managed_active", ACTIVE_SLEEP_WINDOW_SECONDS),
            waitBeforeRetry = {
                waits += 1
                false
            },
        )

        assertFalse(evidence.verified)
        assertEquals("sleep countdown update was refused", evidence.error)
        assertEquals(1, waits)
        assertEquals(1, property.writes.size)
    }

    @Test
    fun movementLocationPermissionsDoNotPermitNetworkMutation() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val permissions = context.packageManager
            .getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS)
            .requestedPermissions
            ?.toSet()
            .orEmpty()

        assertTrue(Manifest.permission.ACCESS_NETWORK_STATE in permissions)
        assertTrue(Manifest.permission.ACCESS_FINE_LOCATION in permissions)
        assertTrue(Manifest.permission.ACCESS_COARSE_LOCATION in permissions)
        assertTrue(Manifest.permission.FOREGROUND_SERVICE_LOCATION in permissions)
        assertFalse(Manifest.permission.CHANGE_NETWORK_STATE in permissions)
    }

    private class FakeProperty(
        initial: Int?,
        private val acceptWrites: Boolean = true,
        private val retainOldValue: Boolean = false,
    ) : SleepWindowPropertyAccessor {
        private var value = initial
        val writes = mutableListOf<Int>()

        override fun readSeconds(): Int? = value

        override fun writeSeconds(seconds: Int): Boolean {
            writes += seconds
            if (acceptWrites && !retainOldValue) value = seconds
            return acceptWrites
        }
    }

    private class ScriptedProperty(
        initial: Int?,
        private var refusedWrites: Int,
    ) : SleepWindowPropertyAccessor {
        private var value = initial
        val writes = mutableListOf<Int>()

        override fun readSeconds(): Int? = value

        override fun writeSeconds(seconds: Int): Boolean {
            writes += seconds
            if (refusedWrites > 0) {
                refusedWrites -= 1
                return false
            }
            value = seconds
            return true
        }
    }
}
