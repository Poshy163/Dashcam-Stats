package com.dashcamstats.obdlogger

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.time.Instant

internal fun trialCalculationCases(): List<JSONObject> {
    val relative = "tests/fixtures/obd_poll_plan_v8_calculations.json"
    val fixture = generateSequence(File(System.getProperty("user.dir"))) { it.parentFile }
        .map { File(it, relative) }.first { it.isFile }
    val cases = JSONObject(fixture.readText()).getJSONArray("cases")
    return (0 until cases.length()).map(cases::getJSONObject)
}

@RunWith(RobolectricTestRunner::class)
class TimedDriveCalculationsTest {
    @Test
    fun sharedServerFixturesMatchFuelDistanceAndIdleCalculations() {
        for (case in trialCalculationCases()) {
            val name = case.getString("name")
            val cycle = if (case.getInt("poll_plan_version") == 8) 3.0 else 5.0
            val trial = case.getInt("poll_plan_version") == 8
            val fuel = EstimatedFuelAccumulator(cycle, if (trial) 3.0 else 15.0, requireTiming = trial)
            val motion = TimedMotionAccumulator(cycle, requireTiming = trial)
            val rows = case.getJSONArray("samples")
            for (index in 0 until rows.length()) {
                val row = rows.getJSONObject(index)
                val quality = row.getJSONObject("quality")
                val missing = quality.getJSONArray("missing_pids")
                val missingMaf = (0 until missing.length()).any { missing.getInt(it) == 0x10 }
                val rowAt = Instant.parse(row.getString("timestamp_utc"))
                val rate = if (row.isNull("estimated_fuel_rate")) null else row.getDouble("estimated_fuel_rate")
                fuel.observe(
                    driveId = row.getString("drive_id"),
                    sequence = row.getLong("sequence"),
                    capturedAt = rowAt,
                    estimatedRate = rate,
                    invalid = row.getString("ecu_data_status") != "live" ||
                        quality.getString("transport") != "ok" || missingMaf,
                    responseAgeMillis = obdResponseAgeMillis(quality, 0x10),
                    timed = quality.has(PID_RESPONSE_AGES_KEY),
                )
                motion.observe(
                    drive = row.getString("drive_id"), sequence = row.getLong("sequence"),
                    rowAt = rowAt,
                    speed = if (row.isNull("vehicle_speed")) null else row.getDouble("vehicle_speed"),
                    rpm = if (row.isNull("engine_rpm")) null else row.getDouble("engine_rpm"),
                    status = row.getString("ecu_data_status"), quality = quality,
                )
            }
            val expected = case.getJSONObject("expected")
            if (expected.isNull("distance_km")) assertEquals(name, 0, motion.distanceIntervals)
            else assertEquals(name, expected.getDouble("distance_km"), motion.distanceKm, 1e-10)
            if (expected.isNull("estimated_fuel_used_l")) assertFalse(name, fuel.hasEvidence)
            else {
                assertTrue(name, fuel.hasEvidence)
                assertEquals(name, expected.getDouble("estimated_fuel_used_l"), fuel.litres, 1e-10)
            }
            if (expected.isNull("idle_duration_s")) assertFalse(name, motion.idleEvidence)
            else {
                assertTrue(name, motion.idleEvidence)
                assertEquals(name, expected.getDouble("idle_duration_s"), motion.idleSeconds, 1e-10)
            }
        }
    }

    @Test
    fun incompleteOrInvalidExplicitAgesNeverBecomeFreshRowTimeReads() {
        val rowAt = Instant.parse("2026-10-22T00:01:00Z")
        assertEquals(rowAt, obdResponseTime(rowAt, JSONObject(), 0x0D))
        assertNull(obdResponseTime(rowAt, JSONObject(), 0x0D, requireTiming = true))
        for (invalid in listOf<Any>(true, "1000", 1.5, -1, 60_001, JSONObject.NULL)) {
            val quality = JSONObject().put(PID_RESPONSE_AGES_KEY, JSONObject().put("0D", invalid))
            assertNull("age $invalid", obdResponseTime(rowAt, quality, 0x0D))
        }
        assertNull(obdResponseTime(rowAt, JSONObject().put(PID_RESPONSE_AGES_KEY, JSONObject()), 0x0D))
        assertEquals(rowAt.minusMillis(1000), obdResponseTime(
            rowAt, JSONObject().put(PID_RESPONSE_AGES_KEY, JSONObject().put("0D", 1000)), 0x0D,
        ))
    }

    @Test
    fun missingRequiredTimingBreaksFuelHoldEvenWithoutAnExplicitMafFailure() {
        val start = Instant.parse("2026-10-22T00:00:00Z")
        val fuel = EstimatedFuelAccumulator(3.0, 3.0, requireTiming = true)
        fuel.observe("one", 0, start.plusSeconds(2), 6.0, responseAgeMillis = 200, timed = true)
        fuel.observe("one", 1, start.plusSeconds(5), null, timed = false)
        fuel.observe("one", 2, start.plusSeconds(8), 12.0, responseAgeMillis = 200, timed = true)
        assertFalse(fuel.hasEvidence)
        fuel.observe("one", 3, start.plusSeconds(11), 12.0, responseAgeMillis = 200, timed = true)
        assertEquals(12.0 * 3 / 3600, fuel.litres, 1e-10)
    }
}
