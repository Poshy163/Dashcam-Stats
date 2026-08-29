package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Test

class ElmProtocolTest {
    @Test
    fun byteTokenizedIsoReplyRequiresExactChecksumAndSource() {
        val response = "010C\r48\r6B\r10\r41\r0C\r0D\r7A\r97\r>"
        val result = ElmProtocol.payload(response, "010C", 0x0C, 2, 0x10)
        assertEquals(0x10, result?.first)
        assertEquals(listOf(0x0D.toByte(), 0x7A.toByte()), result?.second?.toList())
        assertEquals(mapOf("engine_rpm" to 862.5), ElmProtocol.decode(0x0C, result!!.second))
        assertNull(ElmProtocol.payload(response.replace("97", "98"), "010C", 0x0C, 2, 0x10))
        assertNull(ElmProtocol.payload(response, "010C", 0x0C, 2, 0x11))
        assertNull(ElmProtocol.payload(isoHeader(0x49, 0x41, 0x0C, 0x0D, 0x7A), "010C", 0x0C, 2, 0x10))
        assertThrows(ElmProtocolException::class.java) {
            ElmProtocol.requirePayload(response.replace("97", "98"), "010C", 0x0C, 2, 0x10)
        }
    }

    @Test
    fun destructiveAndPersistentCommandsAreNeverAllowed() {
        listOf("04", "08", "ATMA", "ATMR", "ATCV1234", "ATPP01SVFF", "AT@3", "ATSD", "ATLP")
            .forEach { assertFalse(it, ElmProtocol.isSafe(it)) }
        assertTrue(ElmProtocol.isSafe("010C"))
        assertTrue(ElmProtocol.isSafe("03"))
        assertTrue(ElmProtocol.isSafe("0904"))
        assertTrue(ElmProtocol.isSafe("020000"))
        assertTrue(ElmProtocol.isSafe("020200"))
    }

    @Test
    fun sparseDiagnosticsDecodeDtcReadinessAndCalibrationId() {
        val dtc = iso(0x43, 0x01, 0x33, 0, 0, 0, 0)
        assertEquals(
            listOf("P0133"),
            ElmProtocol.dtcs(ElmProtocol.modePayloads(dtc, "03", 0x03, 0x10)),
        )
        val readiness = ElmProtocol.readiness(byteArrayOf(0x81.toByte(), 0x07, 0x65, 0x04))
        assertEquals(true, readiness["mil_on"])
        assertEquals(1, readiness["confirmed_dtc_count"])
        assertEquals(false, readiness["complete"])
        assertTrue((readiness["incomplete"] as List<*>).contains("evaporative_system"))

        val compression = ElmProtocol.readiness(
            byteArrayOf(0x00, 0x08, 0x63, 0x42),
        )
        assertEquals("compression", compression["ignition_type"])
        assertTrue((compression["supported"] as List<*>).contains("particulate_filter"))
        assertTrue((compression["supported"] as List<*>).contains("exhaust_gas_sensor"))
        assertFalse((compression["supported"] as List<*>).contains("oxygen_sensor"))

        val unusualAdvertised = ElmProtocol.mode09Supported(
            iso(0x49, 0, 0x50, 0x40, 0, 0),
            0x10,
        )
        assertEquals(setOf(0x02, 0x04, 0x0A), unusualAdvertised)
        assertEquals(
            setOf(0x03, 0x04, 0x05, 0x06),
            ElmProtocol.mode09DirectProbePids(),
        )

        assertEquals(
            2,
            ElmProtocol.mode09Count(iso(0x49, 0x03, 0x02), 0x03, 0x10),
        )

        val chunks = listOf("1EK2", "Hkl0", "AHk\u0000")
        val response = chunks.mapIndexed { index, text ->
            iso(0x49, 0x04, index + 1, *text.toByteArray().map { it.toInt() }.toIntArray())
                .removeSuffix(">")
        }.joinToString("") + ">"
        assertEquals("1EK2Hkl0AHk", ElmProtocol.mode09Text(response, "0904", 4, 0x10))

        val malformedDtc = dtc.dropLast(4) + "00\r>"
        assertThrows(ElmProtocolException::class.java) {
            ElmProtocol.requireModePayloads(malformedDtc, "03", 0x03, 0x10)
        }
    }

    @Test
    fun slowTierContinuityValuesMatchHomeAssistantDecoders() {
        assertEquals(
            mapOf("oxygen_sensors_present" to listOf(1, 2, 8)),
            ElmProtocol.decode(0x13, byteArrayOf(0x83.toByte())),
        )
        assertEquals(
            mapOf("obd_standard" to "JOBD"),
            ElmProtocol.decode(0x1C, byteArrayOf(0x0A)),
        )
        assertEquals(
            mapOf("distance_with_mil" to 500.0),
            ElmProtocol.decode(0x21, byteArrayOf(0x01, 0xF4.toByte())),
        )
        assertEquals("3", ElmProtocol.protocolNumber("ATDPN\rA3\r>"))
    }

    @Test
    fun freezeFrameRequiresFrameByteAndStrictIsoEvidence() {
        val supported = iso(0x42, 0x00, 0x00, 0xBE, 0x1F, 0xB8, 0x11)
        val payload = ElmProtocol.requirePayload(
            supported,
            "020000",
            0x00,
            5,
            0x10,
            mode = 0x02,
        ).second
        assertEquals(0, payload.first().toInt())
        assertEquals(
            listOf(0xBE.toByte(), 0x1F.toByte(), 0xB8.toByte(), 0x11.toByte()),
            payload.drop(1),
        )
        assertEquals(0x12, ElmProtocol.negativeResponse(iso(0x7F, 0x02, 0x12), "020000", 0x02, 0x10))
    }

    private fun iso(vararg payload: Int): String {
        return isoHeader(0x48, *payload)
    }

    private fun isoHeader(firstHeaderByte: Int, vararg payload: Int): String {
        val body = byteArrayOf(firstHeaderByte.toByte(), 0x6B, 0x10, *payload.map(Int::toByte).toByteArray())
        val checksum = body.sumOf { it.toInt() and 0xFF } and 0xFF
        return (body + checksum.toByte()).joinToString("\r") { "%02X".format(it.toInt() and 0xFF) } + "\r>"
    }
}
