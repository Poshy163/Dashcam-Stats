package com.dashcamstats.obdlogger

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OptionalCommandRecoveryTest {
    @Test fun abortIsAnIgnoredIdleSpaceAndCannotRepeatOrTerminateABusCommand() {
        assertEquals(' ', OPTIONAL_COMMAND_ABORT_CHARACTER)
        assertFalse(OPTIONAL_COMMAND_ABORT_CHARACTER == '\r')
        assertFalse(OPTIONAL_COMMAND_ABORT_CHARACTER in "0123456789ABCDEF")
    }

    private fun deliver(session: ElmCommandSession, pending: CompletableDeferred<String>, text: String) {
        when (val assembled = session.accept(text.toByteArray())) {
            is ElmResponseAssembly.Complete -> pending.complete(assembled.response)
            ElmResponseAssembly.Pending -> Unit
            else -> pending.completeExceptionally(ElmException("ambiguous test response"))
        }
    }

    @Test fun timedOutOptionalValueIsDiscardedBeforeAnIndependentConfirmedPrompt() = runTest {
        val session = ElmCommandSession()
        session.freshConnection()
        session.beginCommand()
        val old = CompletableDeferred<String>()
        var markerRequests = 0
        val failure = runCatching {
            session.awaitResponse(old, "01041", 1, OptionalCommandRecovery(
                abort = { deliver(session, old, "486B10410420C8\r>") },
                confirm = {
                    markerRequests++
                    session.beginCommand()
                    val marker = CompletableDeferred<String>()
                    deliver(session, marker, "ATE0\rOK\r>")
                    session.awaitResponse(marker, "ATE0", 1000)
                },
            ))
        }.exceptionOrNull()
        assertTrue(failure is ElmCommandTimeoutException)
        assertTrue((failure as ElmCommandTimeoutException).promptRecovered)
        assertEquals(1, markerRequests)
        assertFalse(session.isTainted)
        session.beginCommand() // Only the next new command can supply a new measurement.
    }

    @Test fun lateOldPromptsBeforeDuringAndAfterMarkerAllFailClosed() = runTest {
        for (whenLate in listOf("before", "during", "after")) {
            val session = ElmCommandSession()
            session.freshConnection(); session.beginCommand()
            val old = CompletableDeferred<String>()
            val failure = runCatching {
                session.awaitResponse(old, "01041", 1, OptionalCommandRecovery(
                    abort = {
                        deliver(session, old, "STOPPED\r>")
                        if (whenLate == "before") launch {
                            delay(50); session.accept("STALE\r>".toByteArray())
                        }
                    },
                    confirm = {
                        session.beginCommand()
                        val marker = CompletableDeferred<String>()
                        deliver(session, marker, if (whenLate == "during") "486B10410420C8\r>" else "OK\r>")
                        if (whenLate == "after") launch {
                            delay(50); session.accept("STALE\r>".toByteArray())
                        }
                        session.awaitResponse(marker, "ATE0", 1000)
                    },
                ))
            }.exceptionOrNull()
            assertTrue("$whenLate must refuse recovery", failure is ElmException)
            assertFalse((failure as? ElmCommandTimeoutException)?.promptRecovered == true)
            assertTrue(session.isTainted)
        }
    }

    @Test fun missingPromptInvalidConfirmationDisconnectAndCancellationNeverRecover() = runTest {
        for (mode in listOf("no_original_prompt", "no_marker_prompt", "invalid_marker", "disconnect", "cancel")) {
            val session = ElmCommandSession()
            session.freshConnection(); session.beginCommand()
            val old = CompletableDeferred<String>()
            val failure = runCatching {
                session.awaitResponse(old, "01041", 1, OptionalCommandRecovery(
                    abort = {
                        when (mode) {
                            "cancel" -> throw CancellationException("operator stop")
                            "disconnect" -> {
                                session.taint()
                                old.completeExceptionally(ElmException("disconnected"))
                            }
                            "no_original_prompt" -> Unit
                            else -> deliver(session, old, "STOPPED\r>")
                        }
                    },
                    confirm = {
                        session.beginCommand()
                        val marker = CompletableDeferred<String>()
                        if (mode != "no_marker_prompt") deliver(session, marker, "?\r>")
                        session.awaitResponse(marker, "ATE0", 100)
                    },
                ))
            }.exceptionOrNull()
            assertTrue(mode, failure is Exception)
            assertFalse((failure as? ElmCommandTimeoutException)?.promptRecovered == true)
            assertTrue(mode, session.isTainted)
            if (mode == "cancel") assertTrue(failure is CancellationException)
        }
    }

    @Test fun markerCannotContainEchoedOldPidMultiplePromptsOrTrailingBytes() {
        listOf("OK\r>", "ATE0\rOK\r>", "\rOK\r\r>").forEach {
            assertTrue(isOptionalRecoveryConfirmation(it))
        }
        listOf("OK", "OK\r>STALE", "OK\r>\r>", "01041\rOK\r>", "OK\r486B10\r>", "?\r>").forEach {
            assertFalse(it, isOptionalRecoveryConfirmation(it))
        }
    }
}
