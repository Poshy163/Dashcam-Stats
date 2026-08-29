package com.dashcamstats.obdlogger

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class ElmCommandSessionTest {
    @Test
    fun fragmentedNotificationsUseTheProductionPromptAssembler() {
        val session = ElmCommandSession()
        session.freshConnection()
        session.beginCommand()

        assertEquals(
            ElmResponseAssembly.Pending,
            session.accept("486B10 41".toByteArray()),
        )
        assertEquals(
            ElmResponseAssembly.Pending,
            session.accept(" 0C 1A F8\r".toByteArray()),
        )
        assertEquals(
            ElmResponseAssembly.Complete("486B10 41 0C 1A F8\r>"),
            session.accept(">".toByteArray()),
        )
        assertFalse(session.isTainted)
    }

    @Test
    fun multipleIsoFramesInOneNotificationAreKeptUntilTheSinglePrompt() {
        val session = ElmCommandSession()
        session.freshConnection()
        session.beginCommand()
        val notification = "486B10 41 00 BE 1F A8 13 6B\r486B18 41 00 00 00 00 00 2D\r>"

        assertEquals(
            ElmResponseAssembly.Complete(notification),
            session.accept(notification.toByteArray()),
        )
        assertFalse(session.isTainted)
    }

    @Test
    fun missingPromptTimesOutAndTaintsTheSession() {
        val session = ElmCommandSession()
        session.freshConnection()
        session.beginCommand()
        assertEquals(
            ElmResponseAssembly.Pending,
            session.accept("ELM327 v1.5\r".toByteArray()),
        )

        val error = assertThrows(ElmException::class.java) {
            runTest {
                session.awaitResponse(CompletableDeferred(), "ATI", 1)
            }
        }
        assertTrue(error.message!!.contains("prompt from ATI"))
        assertTrue(session.isTainted)
    }

    @Test
    fun protocolSearchTimeoutRequiresAFreshConnection() {
        val session = ElmCommandSession()
        session.freshConnection()
        session.beginCommand()

        val error = assertThrows(ElmException::class.java) {
            runTest {
                session.awaitResponse(CompletableDeferred(), "0100", 1)
            }
        }
        assertEquals("timed out waiting for prompt from 0100", error.message)
        assertTrue(session.isTainted)
        assertThrows(ElmException::class.java) { session.beginCommand() }
    }

    @Test
    fun disconnectAloneCannotClearTaintButFreshGattConnectCan() {
        val session = ElmCommandSession()
        session.freshConnection()
        session.taint()

        session.disconnected()
        assertTrue(session.isTainted)
        assertThrows(ElmException::class.java) { session.beginCommand() }

        session.freshConnection()
        assertFalse(session.isTainted)
        session.beginCommand()
        assertEquals(ElmResponseAssembly.Pending, session.accept("OK\r".toByteArray()))
    }

    @Test
    fun overflowTaintsTheSameSessionPathUsedByBleCallbacks() {
        val session = ElmCommandSession(maximumResponseBytes = 4)
        session.freshConnection()
        session.beginCommand()

        assertEquals(ElmResponseAssembly.Overflow, session.accept("12345".toByteArray()))
        assertTrue(session.isTainted)
    }
}
