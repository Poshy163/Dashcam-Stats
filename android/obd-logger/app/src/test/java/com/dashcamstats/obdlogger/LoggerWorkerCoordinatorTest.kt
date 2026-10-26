package com.dashcamstats.obdlogger

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class LoggerWorkerCoordinatorTest {
    @Test fun repeatedReloadsWaitForActualLoggerCleanupAndStartOnlyLatestReplacement() = runTest {
        val cleanup = CompletableDeferred<Unit>()
        val events = mutableListOf<String>()
        val coordinator = LoggerWorkerCoordinator(backgroundScope) { events += "disconnected" }
        coordinator.request(false) { restarting ->
            events += "original:$restarting"
            try {
                awaitCancellation()
            } finally {
                withContext(NonCancellable) {
                    events += "cleanup-started"
                    cleanup.await()
                    events += "cleanup-finished"
                }
            }
        }
        runCurrent()
        coordinator.request(true) { events += "superseded"; awaitCancellation() }
        runCurrent()
        coordinator.request(true) { restarting ->
            events += "replacement:$restarting"
            awaitCancellation()
        }
        coordinator.request(false) { events += "duplicate-start" }
        runCurrent()
        assertEquals(listOf("original:false", "cleanup-started"), events)

        cleanup.complete(Unit)
        runCurrent()
        assertEquals(
            listOf("original:false", "cleanup-started", "cleanup-finished", "disconnected", "replacement:true"),
            events,
        )
        coordinator.close()
        runCurrent()
    }

    @Test fun closingDuringCleanupPreventsReplacementAndFurtherStarts() = runTest {
        val cleanup = CompletableDeferred<Unit>()
        val events = mutableListOf<String>()
        val coordinator = LoggerWorkerCoordinator(backgroundScope) { events += "disconnected" }
        coordinator.request(false) {
            events += "original"
            try {
                awaitCancellation()
            } finally {
                withContext(NonCancellable) { cleanup.await() }
            }
        }
        runCurrent()
        coordinator.request(true) { events += "replacement" }
        runCurrent()
        coordinator.close()
        coordinator.request(false) { events += "after-close" }
        cleanup.complete(Unit)
        runCurrent()
        assertEquals(listOf("original"), events)
    }

    @Test fun ordinaryStartsKeepRunningAndACompletedWorkerCanStartAgain() = runTest {
        val events = mutableListOf<Boolean>()
        val coordinator = LoggerWorkerCoordinator(backgroundScope)
        coordinator.request(false) { restarting -> events += restarting; awaitCancellation() }
        runCurrent()
        coordinator.request(false) { events += true }
        runCurrent()
        assertEquals(listOf(false), events)
        coordinator.close()

        val completed = LoggerWorkerCoordinator(backgroundScope)
        repeat(2) {
            completed.request(false) { restarting -> events += restarting }
            runCurrent()
        }
        assertEquals(listOf(false, false, false), events)
        completed.close()
    }

    @Test fun failingPostStopCleanupCannotStartAReplacementBeforeOriginalHasFinished() = runTest {
        val cleanup = CompletableDeferred<Unit>()
        val events = mutableListOf<String>()
        val errors = mutableListOf<Throwable>()
        val scope = CoroutineScope(
            SupervisorJob() + StandardTestDispatcher(testScheduler) +
                CoroutineExceptionHandler { _, error -> errors += error },
        )
        val coordinator = LoggerWorkerCoordinator(scope) {
            events += "post-stop"
            throw IllegalStateException("injected disconnect failure")
        }
        try {
            coordinator.request(false) {
                try {
                    awaitCancellation()
                } finally {
                    withContext(NonCancellable) {
                        cleanup.await()
                        events += "original-finished"
                    }
                }
            }
            runCurrent()
            coordinator.request(true) { events += "replacement" }
            runCurrent()
            coordinator.request(true) { events += "latest-replacement" }
            runCurrent()
            assertEquals(emptyList<String>(), events)
            cleanup.complete(Unit)
            runCurrent()
            assertEquals(listOf("original-finished", "post-stop"), events)
            assertEquals(1, errors.size)
        } finally {
            coordinator.close()
            scope.cancel()
        }
    }
}
