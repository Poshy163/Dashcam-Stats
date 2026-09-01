package com.dashcamstats.obdlogger

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.nio.file.Files
import java.time.Instant
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@OptIn(ExperimentalCoroutinesApi::class)
class AppEventJournalTest {
    private lateinit var context: Context
    private lateinit var root: File
    private lateinit var clock: MutableClock
    private var snapshotEnabled = true

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.getSharedPreferences("event-test", Context.MODE_PRIVATE).edit().clear().commit()
        root = Files.createTempDirectory("obd-events").toFile()
        clock = MutableClock(Instant.parse("2026-09-01T10:00:00Z"))
        snapshotEnabled = true
    }

    @Test
    fun snapshotUsesExactSchemaProducerAndNullableFields() {
        val session = UUID.fromString("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").toString()
        val journal = journal()

        assertTrue(
            journal.append(
                sessionId = session,
                kind = "app.service",
                level = "info",
                outcome = "started",
                reasonCode = "service_started",
                metrics = mapOf(
                    "attempt" to 1,
                    "consecutive_failures" to 0,
                    "queue_depth" to 0,
                ),
            ),
        )

        val body = JSONObject(File(root, "events.json").readText())
        assertEquals(
            setOf(
                "schema_version", "source_id", "generated_at_utc", "first_sequence",
                "last_sequence", "producer", "events",
            ),
            body.keysSetForTest(),
        )
        assertEquals(1, body.getInt("schema_version"))
        assertEquals(1L, body.getLong("first_sequence"))
        assertEquals(1L, body.getLong("last_sequence"))
        assertEquals(
            setOf("app_version_name", "app_version_code", "build_git_sha"),
            body.getJSONObject("producer").keysSetForTest(),
        )
        val event = body.getJSONArray("events").getJSONObject(0)
        assertEquals(
            setOf(
                "sequence", "occurred_at_utc", "session_id", "kind", "level", "outcome",
                "reason_code", "drive_id", "metrics",
            ),
            event.keysSetForTest(),
        )
        assertTrue(event.isNull("drive_id"))
        assertEquals(1L, event.getJSONObject("metrics").getLong("attempt"))
        assertEquals(0L, event.getJSONObject("metrics").getLong("consecutive_failures"))
        assertEquals(0L, event.getJSONObject("metrics").getLong("queue_depth"))
        assertFalse(File(root, "events.json.partial").exists())
    }

    @Test
    fun sourceAndSequencePersistAcrossJournalInstances() {
        val session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        val first = journal()
        assertTrue(first.append(session, "app.service", "info", "started", "service_started"))
        val firstBody = JSONObject(File(root, "events.json").readText())

        clock.instant = clock.instant.plusSeconds(1)
        val second = journal()
        assertTrue(second.append(session, "app.service", "info", "completed", "service_stopped"))
        val secondBody = JSONObject(File(root, "events.json").readText())

        assertEquals(firstBody.getString("source_id"), secondBody.getString("source_id"))
        assertEquals(1L, secondBody.getLong("first_sequence"))
        assertEquals(2L, secondBody.getLong("last_sequence"))
    }

    @Test
    fun failedPublicProjectionRetriesFromTheDurablePrivateRing() {
        val journal = journal()
        val session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        snapshotEnabled = false

        assertTrue(journal.append(session, "app.service", "info", "started", "service_started"))
        assertTrue(journal.hasPendingProjection())
        assertFalse(File(root, "events.json").exists())

        snapshotEnabled = true
        assertTrue(journal.publishSnapshot())
        assertFalse(journal.hasPendingProjection())
        val body = JSONObject(File(root, "events.json").readText())
        assertEquals(1L, body.getLong("first_sequence"))
        assertEquals(1L, body.getLong("last_sequence"))
    }

    @Test
    fun invalidCodesAndMetricsAreRejectedWithoutPublishing() {
        val journal = journal()
        val session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

        assertFalse(journal.append(session, "free.text", "info", "started"))
        assertFalse(
            journal.append(
                session,
                "app.service",
                "info",
                "started",
                metrics = mapOf("raw_reply" to 1),
            ),
        )
        assertFalse(File(root, "events.json").exists())
        assertFalse(
            journal.append(
                "11111111-1111-1111-8111-111111111111",
                "app.service",
                "info",
                "started",
            ),
        )
        assertFalse(
            journal.append(
                session,
                "app.service",
                "info",
                "started",
                metrics = mapOf("polling_duty_cycle_percent" to 101),
            ),
        )
    }

    @Test
    fun ringPrunesByAgeAndSnapshotProjectsLatest512Ascending() {
        snapshotEnabled = false
        val journal = journal()
        val session = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        repeat(APP_EVENT_SNAPSHOT_LIMIT + 4) { index ->
            assertTrue(
                journal.append(
                    session,
                    "obd.poll_health",
                    "info",
                    "observed",
                    "drive_summary",
                    metrics = mapOf("sample_count" to index),
                ),
            )
            clock.instant = clock.instant.plusSeconds(1)
        }
        snapshotEnabled = true
        assertTrue(journal.publishSnapshot())
        val body = JSONObject(File(root, "events.json").readText())
        assertEquals(APP_EVENT_SNAPSHOT_LIMIT, body.getJSONArray("events").length())
        assertEquals(5L, body.getLong("first_sequence"))
        assertEquals((APP_EVENT_SNAPSHOT_LIMIT + 4).toLong(), body.getLong("last_sequence"))
        assertTrue(File(root, "events.json").length() <= 512 * 1_024)

        clock.instant = clock.instant.plusSeconds(APP_EVENT_RETENTION_DAYS * 86_400 + 1)
        assertTrue(journal.append(session, "app.service", "info", "started", "service_started"))
        val aged = JSONObject(File(root, "events.json").readText())
        assertEquals(1, aged.getJSONArray("events").length())
        assertEquals((APP_EVENT_SNAPSHOT_LIMIT + 5).toLong(), aged.getLong("first_sequence"))
    }

    @Test
    fun boundedEmitterReportsQueueDropsWithoutBlockingProducer() = runTest {
        val received = mutableListOf<AppEventDraft>()
        val dispatcher = StandardTestDispatcher(testScheduler)
        val workerScope = CoroutineScope(SupervisorJob() + dispatcher)
        val emitter = AppEventEmitter(
            journal = AppEventDraftSink {
                received += it
                true
            },
            sessionId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            scope = workerScope,
            now = { clock.instant },
            capacity = 1,
        )

        assertTrue(emitter.emit("app.service", "info", "started", "service_started"))
        assertFalse(emitter.emit("app.service", "info", "observed", "start_command"))
        assertEquals(1L, emitter.droppedCount())
        runCurrent()

        assertEquals(2, received.size)
        assertEquals("pruned", received.first().outcome)
        assertEquals(1L, received.first().metrics.getValue("gap_count"))
        assertEquals("started", received.last().outcome)
        emitter.closeBestEffort()
        runCurrent()
        workerScope.cancel()
    }

    @Test
    fun sinkFailureCannotCancelSiblingLoggerWork() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val workerScope = CoroutineScope(SupervisorJob() + dispatcher)
        val sibling = workerScope.launch { kotlinx.coroutines.awaitCancellation() }
        val emitter = AppEventEmitter(
            journal = AppEventDraftSink { throw IllegalStateException("private failure") },
            sessionId = "ffffffff-ffff-4fff-8fff-ffffffffffff",
            scope = workerScope,
            now = { clock.instant },
            capacity = 1,
        )

        assertTrue(emitter.emit("app.service", "info", "started", "service_started"))
        runCurrent()

        assertTrue(sibling.isActive)
        emitter.closeBestEffort()
        sibling.cancel()
        workerScope.cancel()
    }

    @Test
    fun falseSinkResultIsRetriedAsANumericGapOnTheNextDrain() = runTest {
        val received = mutableListOf<AppEventDraft>()
        var attempts = 0
        val dispatcher = StandardTestDispatcher(testScheduler)
        val workerScope = CoroutineScope(SupervisorJob() + dispatcher)
        val emitter = AppEventEmitter(
            journal = AppEventDraftSink { draft ->
                attempts += 1
                if (attempts == 1) {
                    false
                } else {
                    received += draft
                    true
                }
            },
            sessionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scope = workerScope,
            now = { clock.instant },
            capacity = 1,
        )

        assertTrue(emitter.emit("app.service", "info", "started", "service_started"))
        runCurrent()
        assertEquals(1L, emitter.droppedCount())

        assertTrue(emitter.emit("app.service", "info", "observed", "start_command"))
        runCurrent()
        assertEquals(listOf("pruned", "observed"), received.map(AppEventDraft::outcome))
        assertEquals(1L, received.first().metrics.getValue("gap_count"))
        assertEquals(0L, emitter.droppedCount())

        emitter.closeBestEffort()
        runCurrent()
        workerScope.cancel()
    }

    @Test
    fun terminalEventIsReservedWhenTheProducerQueueIsFull() = runTest {
        val received = mutableListOf<AppEventDraft>()
        val dispatcher = StandardTestDispatcher(testScheduler)
        val workerScope = CoroutineScope(SupervisorJob() + dispatcher)
        val emitter = AppEventEmitter(
            journal = AppEventDraftSink {
                received += it
                true
            },
            sessionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            scope = workerScope,
            now = { clock.instant },
            capacity = 1,
        )

        assertTrue(emitter.emit("app.service", "info", "started", "service_started"))
        assertFalse(emitter.emit("app.service", "info", "observed", "start_command"))
        assertTrue(
            emitter.closeWithTerminal(
                "app.service",
                "info",
                "completed",
                "service_stopped",
            ),
        )
        runCurrent()

        assertEquals(listOf("pruned", "started", "completed"), received.map(AppEventDraft::outcome))
        assertEquals("service_stopped", received.last().reasonCode)
        assertEquals(0L, emitter.droppedCount())
        workerScope.cancel()
    }

    private fun journal(): AppEventJournal = AppEventJournal(
        context = context,
        now = { clock.instant },
        sourceUuid = { UUID.fromString("11111111-1111-4111-8111-111111111111") },
        producer = AppEventProducer("0.2.5", 8, "0123456789ab"),
        snapshotRoot = { root.takeIf { snapshotEnabled } },
        preferences = context.getSharedPreferences("event-test", Context.MODE_PRIVATE),
    )

    private class MutableClock(var instant: Instant)
}

private fun JSONObject.keysSetForTest(): Set<String> = buildSet {
    val iterator = keys()
    while (iterator.hasNext()) add(iterator.next())
}
