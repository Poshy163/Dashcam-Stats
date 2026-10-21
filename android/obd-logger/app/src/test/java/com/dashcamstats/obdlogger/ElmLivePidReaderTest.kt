package com.dashcamstats.obdlogger

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ElmLivePidReaderTest {
    @Test
    fun malformedSuffixedReplySelectsPlainOnlyForThatPidOnItsNextScheduledPoll() = runTest {
        val reader = ElmLivePidReader()
        val session = ElmCommandSession()
        session.freshConnection()
        val sent = mutableListOf<String>()
        val replies = ArrayDeque(listOf(
            iso(0x15, 0x68, 0x80, checksumDelta = 1),
            iso(0x0C, 0x0D, 0x7A),
            iso(0x15, 0x68, 0x80),
            iso(0x15, 0x69, 0x80),
        ))
        suspend fun execute(command: String): String {
            assertEquals(ElmCommandCategory.VEHICLE_BUS, ElmProtocol.commandCategory(command))
            sent += command
            session.beginCommand()
            val assembled = session.accept((command + "\r" + replies.removeFirst()).toByteArray())
            assertTrue(assembled is ElmResponseAssembly.Complete)
            return (assembled as ElmResponseAssembly.Complete).response
        }

        val failure = runCatching { reader.queryPayload(0x15, 0x10, ::execute) }.exceptionOrNull()
        assertTrue(failure is ElmProtocolException)
        assertTrue(failure!!.message!!.endsWith("next scheduled poll uses a plain request"))
        assertEquals(listOf("01151"), sent) // No immediate duplicate request or retry loop.
        assertFalse(session.isTainted)
        assertEquals(listOf(0x0D.toByte(), 0x7A.toByte()), reader.queryPayload(0x0C, 0x10, ::execute)!!.toList())
        val recovered = reader.queryPayload(0x15, 0x10, ::execute)!!
        assertEquals(mapOf("oxygen_sensor_2_voltage" to 0.52, "oxygen_sensor_2_short_term_fuel_trim" to 0.0), ElmProtocol.decode(0x15, recovered))
        reader.queryPayload(0x15, 0x10, ::execute)
        assertEquals(listOf("01151", "010C1", "0115", "0115"), sent)
        assertTrue(reader.responseCountSuffixActive)
        assertFalse(session.isTainted)
    }

    @Test
    fun plainReprobeStillRejectsWrongSourceLengthChecksumAndPid() = runTest {
        val badReplies = listOf(
            iso(0x21, 0x01, 0xF4, source = 0x11),
            iso(0x21, 0x01),
            iso(0x21, 0x01, 0xF4, 0x00),
            iso(0x21, 0x01, 0xF4, checksumDelta = 1),
            iso(0x15, 0x01, 0xF4),
        )
        for (badReply in badReplies) {
            val reader = ElmLivePidReader()
            val sent = mutableListOf<String>()
            repeat(2) {
                val failure = runCatching {
                    reader.queryPayload(0x21, 0x10) { command -> sent += command; badReply }
                }.exceptionOrNull()
                assertTrue(failure is ElmProtocolException)
                assertFalse(failure!!.message!!.contains(badReply))
            }
            val payload = reader.queryPayload(0x21, 0x10) { command ->
                sent += command
                iso(0x21, 0x01, 0xF4)
            }!!
            assertEquals(mapOf("distance_with_mil" to 500.0), ElmProtocol.decode(0x21, payload))
            assertEquals(listOf("01211", "0121", "0121"), sent)
        }
    }

    @Test
    fun firstPlainReprobeRemainsEligibleAtBothPidsNextOrdinaryScheduleSlot() = runTest {
        for (pid in listOf(0x15, 0x21)) {
            val reader = ElmLivePidReader()
            val tracker = LivePidMalformedTracker()
            val first = (0L..20L).first { pid in ObdPollPlan.requestedPids(it, setOf(pid)) }
            val failure = runCatching {
                reader.queryPayload(pid, 0x10) { iso(pid, 0x01, 0x80, checksumDelta = 1) }
            }.exceptionOrNull()
            assertTrue(failure is ElmProtocolException)
            tracker.recordMalformed(pid, first)
            val next = ((first + 1)..(first + 20)).first {
                pid in ObdPollPlan.requestedPids(it, setOf(pid))
            }
            assertTrue("the plain reprobe must not be skipped by the first-error cooldown", tracker.shouldPoll(pid, next))
            reader.queryPayload(pid, 0x10) { command ->
                assertEquals("01%02X".format(pid), command)
                iso(pid, 0x01, 0x80)
            }
            tracker.recordValid(pid)
            assertTrue(tracker.shouldPoll(pid, next + 1))
        }
    }

    @Test
    fun freshConnectionResetsPerPidFormattingWithoutWeakeningTheVoltageOnlyWriteFence() = runTest {
        val reader = ElmLivePidReader()
        runCatching { reader.queryPayload(0x15, 0x10) { iso(0x15, 0x01, 0x80, checksumDelta = 1) } }
        reader.freshConnection()
        reader.queryPayload(0x15, 0x10) { command ->
            assertEquals("01151", command)
            iso(0x15, 0x01, 0x80)
        }
        val voltageOnlyGate = ElmCommandWriteGate(ElmCommandPolicy.VOLTAGE_ONLY)
        var writes = 0
        val failure = runCatching {
            reader.queryPayload(0x15, 0x10) { command ->
                if (voltageOnlyGate.authorize(command) == null) throw ElmCommandRejectedException("policy refused")
                writes++
                iso(0x15, 0x01, 0x80)
            }
        }.exceptionOrNull()
        assertTrue(failure is ElmCommandRejectedException)
        assertEquals(0, writes)
    }

    @Test
    fun unsupportedSuffixStillFallsBackGloballyWhileNoDataAndNegativeRepliesDoNot() = runTest {
        var suffixDisabledCount = 0
        val reader = ElmLivePidReader { suffixDisabledCount++ }
        val sent = mutableListOf<String>()
        reader.queryPayload(0x15, 0x10) { command ->
            sent += command
            if (command == "01151") "?\r>" else iso(0x15, 0x01, 0x80)
        }
        reader.queryPayload(0x21, 0x10) { command -> sent += command; iso(0x21, 0x00, 0x01) }
        assertEquals(listOf("01151", "0115", "0121"), sent)
        assertEquals(1, suffixDisabledCount)
        assertFalse(reader.responseCountSuffixActive)

        reader.freshConnection()
        assertNull(reader.queryPayload(0x15, 0x10) { "NO DATA\r>" })
        val negative = isoNegative(0x12)
        val rejection = runCatching { reader.queryPayload(0x21, 0x10) { negative } }.exceptionOrNull()
        assertTrue(rejection is ElmCommandRejectedException)
        reader.queryPayload(0x15, 0x10) { command -> assertEquals("01151", command); iso(0x15, 0x01, 0x80) }
        reader.queryPayload(0x21, 0x10) { command -> assertEquals("01211", command); iso(0x21, 0x00, 0x01) }
        assertEquals(1, suffixDisabledCount)
    }

    @Test
    fun transportFailuresAndQuiesceNeverTriggerAReprobeOrAnotherCommand() = runTest {
        val reader = ElmLivePidReader()
        var commands = 0
        val timeout = runCatching {
            reader.queryPayload(0x21, 0x10) { commands++; throw ElmCommandTimeoutException("prompt timeout") }
        }.exceptionOrNull()
        assertTrue(timeout is ElmCommandTimeoutException)
        assertEquals(1, commands)
        val transport = runCatching { reader.queryPayload(0x21, 0x10) { commands++; "BUS INIT: ERROR\r>" } }.exceptionOrNull()
        assertTrue(transport is ElmException)
        assertEquals(2, commands)
        val quiesce = runCatching {
            reader.queryPayload(0x21, 0x10) { commands++; throw ElmQuiesceRequestedException() }
        }.exceptionOrNull()
        assertTrue(quiesce is ElmQuiesceRequestedException)
        assertEquals(3, commands)
        reader.queryPayload(0x21, 0x10) { command -> assertEquals("01211", command); iso(0x21, 0x00, 0x01) }
    }

    private fun iso(pid: Int, vararg values: Int, source: Int = 0x10, checksumDelta: Int = 0): String {
        val body = listOf(0x48, 0x6B, source, 0x41, pid) + values.toList()
        val checksum = (body.sum() + checksumDelta) and 0xFF
        return (body + checksum).joinToString(" ") { "%02X".format(it) } + "\r>"
    }

    private fun isoNegative(code: Int): String {
        val body = listOf(0x48, 0x6B, 0x10, 0x7F, 0x01, code)
        return (body + (body.sum() and 0xFF)).joinToString(" ") { "%02X".format(it) } + "\r>"
    }
}
