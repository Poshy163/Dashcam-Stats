package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import java.lang.reflect.InvocationTargetException

@RunWith(RobolectricTestRunner::class)
class LoggerRecoveryStateTest {
    private lateinit var context: Context
    private lateinit var database: ObdDatabase
    private lateinit var service: ObdLoggerService

    @Before fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase("obd_drives.db")
        database = ObdDatabase(context)
        // Do not run onCreate: this exercises the service's DB recovery without starting BLE,
        // native sleep control, location, or the independent temperature collector.
        service = Robolectric.buildService(ObdLoggerService::class.java).get()
        field("database").set(service, database)
        database.startDrive(
            DriveRecord(
                driveId = DRIVE,
                vehicleId = "test-car",
                adapterId = "test-adapter",
                loggerId = "test-logger",
                loggerVersion = "test",
                startedAtUtc = "2026-10-26T01:00:00Z",
                originalTimezone = "UTC",
                startReason = "test",
                obdProtocol = "test",
                pollPlanVersion = PollTrialPlan.VERSION,
            ),
        )
        database.addSample(
            SampleRecord(
                driveId = DRIVE,
                sequence = 0,
                timestampUtc = "2026-10-26T01:00:03Z",
                values = mapOf("vehicle_speed" to 12.0, "engine_rpm" to 1_200.0),
            ),
        )
        field("currentDriveId").set(service, DRIVE)
        field("currentPollPlanVersion").setInt(service, PollTrialPlan.VERSION)
        field("driveStartedElapsedMillis").set(service, 1_000L)
        failFinalisation()
    }

    @After fun tearDown() {
        database.close()
        context.deleteDatabase("obd_drives.db")
    }

    @Test fun transientFinalisationFailureRecoversRowsAndClearsStaleActiveDriveState() {
        val finalise = ObdLoggerService::class.java.getDeclaredMethod(
            "finalizeActiveDrive", String::class.java, String::class.java, String::class.java,
            String::class.java, kotlin.jvm.functions.Function0::class.java,
        ).apply { isAccessible = true }
        val failure = assertThrows(InvocationTargetException::class.java) {
            finalise.invoke(
                service, DRIVE, "database_fault", "2026-10-26T01:00:03Z",
                "2026-10-26T01:00:04Z", { "2026-10-26T01:00:04Z" },
            )
        }
        assertTrue(failure.cause is android.database.SQLException)
        assertEquals(DRIVE, field("currentDriveId").get(service))
        assertEquals("recording", database.drive(DRIVE).getString("status"))

        database.writableDatabase.execSQL("DROP TRIGGER fail_logger_finalisation")
        val recovered = recover()
        assertEquals(listOf(DRIVE), recovered.map { it.driveId })
        assertNull(field("currentDriveId").get(service))
        assertEquals(ObdPollPlan.VERSION, field("currentPollPlanVersion").getInt(service))
        assertNull(field("driveStartedElapsedMillis").get(service))
        assertEquals("interrupted", database.drive(DRIVE).getString("status"))
        assertEquals(1, database.samples(DRIVE).size)
        assertEquals(1_200.0, database.samples(DRIVE).single().getDouble("engine_rpm"), 0.0)
        assertTrue(database.completedDriveIds().contains(DRIVE))
        assertTrue(recover().isEmpty())
    }

    @Test fun failedRecoveryKeepsActiveStateUntilDatabaseActuallyFinalises() {
        val failure = assertThrows(InvocationTargetException::class.java) { recover() }
        assertTrue(failure.cause is android.database.SQLException)
        assertEquals(DRIVE, field("currentDriveId").get(service))
        assertEquals(PollTrialPlan.VERSION, field("currentPollPlanVersion").getInt(service))
        assertEquals(1_000L, field("driveStartedElapsedMillis").get(service))
        assertEquals("recording", database.drive(DRIVE).getString("status"))
        assertEquals(1, database.samples(DRIVE).size)
    }

    private fun failFinalisation() {
        database.writableDatabase.execSQL(
            """
            CREATE TEMP TRIGGER fail_logger_finalisation BEFORE UPDATE OF status ON drives
            WHEN NEW.status IN ('finalising','interrupted')
            BEGIN SELECT RAISE(ABORT, 'injected transient finalisation failure'); END
            """.trimIndent(),
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun recover(): List<DriveFinalization> =
        ObdLoggerService::class.java.getDeclaredMethod("recoverInterruptedDrives", String::class.java)
            .apply { isAccessible = true }.invoke(service, "process_terminated") as List<DriveFinalization>

    private fun field(name: String) =
        ObdLoggerService::class.java.getDeclaredField(name).apply { isAccessible = true }

    private companion object { const val DRIVE = "recovery-test-drive" }
}
