package com.dashcamstats.obdlogger

class ElmProtocolException(message: String) : RuntimeException(message)

enum class ElmCommandCategory(val metricName: String) {
    ADAPTER_LOCAL("adapter_local"),
    VEHICLE_BUS("vehicle_bus"),
}

enum class ElmCommandPolicy { FULL_OBD, VOLTAGE_ONLY }

/** Stateful, fail-closed gate used by the production BLE write path. */
internal class ElmCommandWriteGate(private val policy: ElmCommandPolicy) {
    private var voltageOnlyCommandConsumed = false

    @Synchronized
    fun authorize(command: String): ElmCommandCategory? {
        if (!ElmProtocol.commandAllowed(policy, command)) return null
        if (policy == ElmCommandPolicy.VOLTAGE_ONLY) {
            if (voltageOnlyCommandConsumed) return null
            voltageOnlyCommandConsumed = true
        }
        return ElmProtocol.commandCategory(command)
    }
}

object ElmProtocol {
    const val MIN_ADAPTER_VOLTAGE = 9.0
    const val MAX_ADAPTER_VOLTAGE = 16.5

    val pidLengths = mapOf(
        0x01 to 4,
        0x03 to 2,
        0x04 to 1,
        0x05 to 1,
        0x06 to 1,
        0x07 to 1,
        0x0C to 2,
        0x0D to 1,
        0x0E to 1,
        0x0F to 1,
        0x10 to 2,
        0x11 to 1,
        0x13 to 1,
        0x14 to 2,
        0x15 to 2,
        0x1C to 1,
        0x20 to 4,
        0x21 to 2,
    )

    private val safeAt = setOf(
        "ATZ", "ATI", "ATD", "ATD0", "ATE0", "ATL0", "ATH1", "ATSP0", "ATM0",
        "ATS0", "ATAT1", "ATAL", "ATST64", "ATRV", "AT@1", "AT@2", "ATIGN", "ATDP",
        "ATDPN", "ATPC",
    )
    private val safeDiagnostics = setOf(
        "03", "07", "0A", "0900", "0902", "0903", "0904", "0905", "0906", "090A",
    )
    private val obdStandards = mapOf(
        1 to "OBD-II (CARB)",
        2 to "OBD (EPA)",
        3 to "OBD and OBD-II",
        4 to "OBD-I",
        5 to "Not OBD compliant",
        6 to "EOBD",
        7 to "EOBD and OBD-II",
        8 to "EOBD and OBD",
        9 to "EOBD, OBD and OBD-II",
        10 to "JOBD",
        11 to "JOBD and OBD-II",
        12 to "JOBD and EOBD",
        13 to "JOBD, EOBD and OBD-II",
        17 to "Engine Manufacturer Diagnostics",
        18 to "Engine Manufacturer Diagnostics Enhanced",
        19 to "Heavy Duty OBD (Child/Partial)",
        20 to "Heavy Duty OBD",
        21 to "World Wide Harmonized OBD",
        23 to "Heavy Duty Euro OBD Stage I without NOx control",
        24 to "Heavy Duty Euro OBD Stage I with NOx control",
        25 to "Heavy Duty Euro OBD Stage II without NOx control",
        26 to "Heavy Duty Euro OBD Stage II with NOx control",
        28 to "Brazil OBD Phase 1",
        29 to "Brazil OBD Phase 2",
        30 to "Korean OBD",
        31 to "India OBD I",
        32 to "India OBD II",
        33 to "Heavy Duty Euro OBD Stage VI",
    )
    val freezeFramePids = setOf(
        0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F,
        0x10, 0x11, 0x14, 0x15,
    )
    private val freezeFrame = Regex("^02(?:00|01|02|03|04|05|06|07|0C|0D|0E|0F|10|11|14|15)00$")

    fun normalize(command: String): String = command.filterNot(Char::isWhitespace).uppercase()

    fun commandCategory(command: String): ElmCommandCategory? {
        if (command.any(Char::isISOControl)) return null
        val value = normalize(command)
        if (value in safeAt) return ElmCommandCategory.ADAPTER_LOCAL
        if (value in safeDiagnostics || freezeFrame.matches(value)) {
            return ElmCommandCategory.VEHICLE_BUS
        }
        if (value.length == 4 && value.startsWith("01")) {
            return if (
                value.substring(2).toIntOrNull(16)?.let { it in pidLengths || it == 0 } == true
            ) {
                ElmCommandCategory.VEHICLE_BUS
            } else {
                null
            }
        }
        return null
    }

    fun isSafe(command: String): Boolean = commandCategory(command) != null

    /** The voltage-only policy is enforced immediately before every BLE write. */
    fun commandAllowed(policy: ElmCommandPolicy, command: String): Boolean {
        if (commandCategory(command) == null) return false
        val normalized = normalize(command)
        return policy == ElmCommandPolicy.FULL_OBD ||
            normalized == ElmAdapterCommandPlan.parkedVoltageProbe
    }

    private val voltageLinePattern =
        Regex("^([0-9]{1,2}(?:\\.[0-9]+)?)\\s*V$", RegexOption.IGNORE_CASE)

    private fun parsedVoltageResult(response: String): Pair<Double, String>? {
        val framed = response.trim()
        if (!framed.endsWith(">") || framed.dropLast(1).contains('>')) return null
        val lines = framed.dropLast(1)
            .split('\r', '\n')
            .map(String::trim)
            .filter(String::isNotEmpty)
            .toMutableList()
        if (lines.firstOrNull()?.let(::normalize) == ElmAdapterCommandPlan.parkedVoltageProbe) {
            lines.removeAt(0)
        }
        if (lines.size != 1) return null
        val match = voltageLinePattern.matchEntire(lines.single()) ?: return null
        val token = match.groupValues[1]
        val value = token.toDoubleOrNull() ?: return null
        if (!value.isFinite() || value !in MIN_ADAPTER_VOLTAGE..MAX_ADAPTER_VOLTAGE) return null
        return value to "$token V"
    }

    /**
     * Parse one unambiguous adapter-voltage result.
     *
     * A missing suffix, more than one voltage token, and values outside a conservative 12 V
     * automotive electrical range are invalid. Never clamp malformed input into validity.
     */
    fun voltage(response: String): Double? {
        return parsedVoltageResult(response)?.first
    }

    /** Return only the bounded numeric token from a validated ATRV response. */
    fun sanitizedVoltageResponse(response: String): String? {
        return parsedVoltageResult(response)?.second
    }

    fun protocolNumber(response: String): String? {
        val value = normalize(response.replace("ATDPN", "").replace(">", ""))
        return Regex("A?([0-9A-C])").matchEntire(value)?.groupValues?.get(1)
    }

    fun hasTransportError(response: String): Boolean {
        val upper = response.uppercase()
        return listOf(
            "UNABLE TO CONNECT", "BUS INIT: ERROR", "CAN ERROR", "STOPPED", "BUFFER FULL",
            "RX ERROR",
        ).any(upper::contains)
    }

    private fun cleanedLines(response: String, command: String): List<String> {
        val expected = normalize(command)
        return response.replace(">", "\r").split('\r', '\n').mapNotNull { raw ->
            var line = raw.trim()
            if (line.isEmpty() || normalize(line) == expected) return@mapNotNull null
            if (line.uppercase().startsWith("SEARCHING...")) line = line.drop(12).trim()
            line.ifEmpty { null }
        }
    }

    private fun frames(response: String, command: String): List<ByteArray> {
        val result = mutableListOf<ByteArray>()
        val byteRun = mutableListOf<Byte>()
        fun flush() {
            if (byteRun.isNotEmpty()) {
                result += splitIso(byteRun.toByteArray())
                byteRun.clear()
            }
        }
        for (line in cleanedLines(response, command)) {
            val compact = line.substringAfter(':', line).replace(" ", "")
            if (!compact.matches(Regex("^[0-9A-Fa-f]+$")) || compact.length % 2 != 0) {
                flush()
                continue
            }
            val bytes = compact.chunked(2).map { it.toInt(16).toByte() }
            if (bytes.size == 1) byteRun += bytes.single()
            else {
                flush()
                result += bytes.toByteArray()
            }
        }
        flush()
        return result
    }

    private fun splitIso(data: ByteArray): List<ByteArray> {
        val headers = (0 until data.size - 1).filter {
            data[it].toInt() and 0xFF == 0x48 && data[it + 1].toInt() and 0xFF == 0x6B
        }
        if (headers.isEmpty()) return listOf(data)
        val valid = mutableMapOf<Int, Int>()
        for ((position, start) in headers.withIndex()) {
            for (end in headers.drop(position + 1) + data.size) {
                val candidate = data.copyOfRange(start, end)
                if (candidate.size >= 5 && checksum(candidate)) {
                    valid[start] = end
                    break
                }
            }
        }
        if (valid.isEmpty()) return listOf(data)
        val result = mutableListOf<ByteArray>()
        var cursor = 0
        while (cursor < data.size) {
            val start = headers.firstOrNull { it >= cursor && valid.containsKey(it) }
            if (start == null) {
                result += data.copyOfRange(cursor, data.size)
                break
            }
            if (start > cursor) result += data.copyOfRange(cursor, start)
            val end = valid.getValue(start)
            result += data.copyOfRange(start, end)
            cursor = end
        }
        return result
    }

    private fun checksum(frame: ByteArray): Boolean {
        if (frame.size < 5) return false
        val sum = frame.dropLast(1).sumOf { it.toInt() and 0xFF } and 0xFF
        return sum == frame.last().toInt() and 0xFF
    }

    private fun validIso(frame: ByteArray, markerIndex: Int, expectedSource: Int?): Boolean =
        markerIndex == 3 && frame.size >= 5 &&
            frame[0].toInt() and 0xFF == 0x48 && frame[1].toInt() and 0xFF == 0x6B &&
            checksum(frame) &&
            (expectedSource == null || frame[2].toInt() and 0xFF == expectedSource)

    fun payload(
        response: String,
        command: String,
        pid: Int,
        length: Int,
        expectedSource: Int? = null,
        mode: Int = 0x01,
    ): Pair<Int, ByteArray>? {
        val marker = byteArrayOf((mode + 0x40).toByte(), pid.toByte())
        for (frame in frames(response, command)) {
            val index = frame.indices.firstOrNull { at ->
                at + 1 < frame.size && frame[at] == marker[0] && frame[at + 1] == marker[1]
            } ?: continue
            val end = index + marker.size + length
            if (!validIso(frame, index, expectedSource) || frame.size != end + 1) continue
            val source = frame[2].toInt() and 0xFF
            return source to frame.copyOfRange(index + marker.size, end)
        }
        return null
    }

    fun requirePayload(
        response: String,
        command: String,
        pid: Int,
        length: Int,
        expectedSource: Int? = null,
        mode: Int = 0x01,
    ): Pair<Int, ByteArray> = payload(
        response,
        command,
        pid,
        length,
        expectedSource,
        mode,
    ) ?: throw ElmProtocolException(
        "$command reply failed ISO header/length/source/checksum validation",
    )

    fun modePayloads(
        response: String,
        command: String,
        mode: Int,
        expectedSource: Int,
    ): List<ByteArray> = buildList {
        for (frame in frames(response, command)) {
            val index = frame.indexOf((mode + 0x40).toByte())
            if (!validIso(frame, index, expectedSource)) continue
            if (frame.size > index + 2) add(frame.copyOfRange(index + 1, frame.size - 1))
        }
    }

    fun requireModePayloads(
        response: String,
        command: String,
        mode: Int,
        expectedSource: Int,
    ): List<ByteArray> = modePayloads(response, command, mode, expectedSource).takeIf {
        it.isNotEmpty()
    } ?: throw ElmProtocolException(
        "$command reply failed ISO header/source/checksum validation",
    )

    fun negativeResponse(
        response: String,
        command: String,
        requestedMode: Int,
        expectedSource: Int,
    ): Int? {
        for (frame in frames(response, command)) {
            val index = frame.indexOf(0x7F.toByte())
            if (!validIso(frame, index, expectedSource) || frame.size != index + 4) continue
            if (frame[index + 1].toInt() and 0xFF == requestedMode) {
                return frame[index + 2].toInt() and 0xFF
            }
        }
        return null
    }

    fun dtcs(payloads: List<ByteArray>): List<String> {
        val result = linkedSetOf<String>()
        for (payload in payloads) {
            for (index in 0 until payload.size - 1 step 2) {
                val first = payload[index].toInt() and 0xFF
                val second = payload[index + 1].toInt() and 0xFF
                if (first == 0 && second == 0) continue
                val family = "PCBU"[(first ushr 6) and 3]
                result += "%c%X%X%02X".format(family, (first ushr 4) and 3, first and 15, second)
            }
        }
        return result.toList()
    }

    fun readiness(payload: ByteArray): Map<String, Any> {
        if (payload.size < 4) return emptyMap()
        fun u(index: Int) = payload[index].toInt() and 0xFF
        val supported = mutableListOf<String>()
        val incomplete = mutableListOf<String>()
        val continuous = listOf(
            Triple(0x01, 0x10, "misfire"),
            Triple(0x02, 0x20, "fuel_system"),
            Triple(0x04, 0x40, "components"),
        )
        for ((supportBit, incompleteBit, name) in continuous) {
            if (u(1) and supportBit != 0) {
                supported += name
                if (u(1) and incompleteBit != 0) incomplete += name
            }
        }
        val compressionIgnition = u(1) and 0x08 != 0
        val nonContinuous = if (compressionIgnition) {
            listOf(
                0x80 to "egr_or_vvt",
                0x40 to "particulate_filter",
                0x20 to "exhaust_gas_sensor",
                0x08 to "boost_pressure",
                0x02 to "nox_or_scr",
                0x01 to "nmhc_catalyst",
            )
        } else {
            listOf(
                0x80 to "egr_or_vvt",
                0x40 to "oxygen_sensor_heater",
                0x20 to "oxygen_sensor",
                0x10 to "ac_refrigerant",
                0x08 to "secondary_air",
                0x04 to "evaporative_system",
                0x02 to "heated_catalyst",
                0x01 to "catalyst",
            )
        }
        for ((bit, name) in nonContinuous) {
            if (u(2) and bit != 0) {
                supported += name
                if (u(3) and bit != 0) incomplete += name
            }
        }
        return mapOf(
            "mil_on" to (u(0) and 0x80 != 0),
            "confirmed_dtc_count" to (u(0) and 0x7F),
            "ignition_type" to if (compressionIgnition) "compression" else "spark",
            "supported" to supported,
            "incomplete" to incomplete,
            "complete" to incomplete.isEmpty(),
        )
    }

    fun mode09Supported(response: String, expectedSource: Int): Set<Int>? {
        val frame = mode09Frames(response, "0900", 0, expectedSource).firstOrNull() ?: return null
        if (frame.size < 4) return null
        val mask = frame.take(4).fold(0L) { value, byte ->
            (value shl 8) or (byte.toLong() and 0xFF)
        }
        return (1..32).filter { mask and (1L shl (32 - it)) != 0L }.toSet()
    }

    fun mode09DirectProbePids(): Set<Int> {
        // 0900 is advisory on the target ECU. These two probes are bounded/read-only and
        // known to return useful values even when the paired bitmap bits are unusual.
        return setOf(0x03, 0x04, 0x05, 0x06)
    }

    fun mode09Count(response: String, pid: Int, expectedSource: Int): Int? {
        require(pid in setOf(0x03, 0x05))
        return mode09Frames(response, "09%02X".format(pid), pid, expectedSource)
            .firstOrNull()
            ?.firstOrNull()
            ?.toInt()
            ?.and(0xFF)
    }

    fun mode09Text(response: String, command: String, pid: Int, expectedSource: Int): String? {
        val chunks = sortedMapOf<Int, ByteArray>()
        for (payload in mode09Frames(response, command, pid, expectedSource)) {
            if (payload.size < 2) continue
            val sequence = payload[0].toInt() and 0xFF
            val chunk = payload.copyOfRange(1, payload.size)
            if (chunks[sequence]?.contentEquals(chunk) == false) return null
            chunks[sequence] = chunk
        }
        if (chunks.isEmpty() || chunks.keys.toList() != (1..chunks.lastKey()).toList()) return null
        return chunks.values.flatMap(ByteArray::toList).toByteArray()
            .filter { it.toInt() != 0 }
            .toByteArray()
            .toString(Charsets.US_ASCII)
            .trim()
            .ifEmpty { null }
    }

    fun mode09Cvns(response: String, expectedSource: Int): List<String> {
        val chunks = sortedMapOf<Int, ByteArray>()
        for (payload in mode09Frames(response, "0906", 6, expectedSource)) {
            if (payload.size < 5) continue
            chunks[payload[0].toInt() and 0xFF] = payload.copyOfRange(1, payload.size)
        }
        if (chunks.isEmpty() || chunks.keys.toList() != (1..chunks.lastKey()).toList()) return emptyList()
        val value = chunks.values.flatMap(ByteArray::toList).toByteArray()
        if (value.size % 4 != 0) return emptyList()
        return value.toList().chunked(4).map { bytes ->
            bytes.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
        }
    }

    private fun mode09Frames(
        response: String,
        command: String,
        pid: Int,
        expectedSource: Int,
    ): List<ByteArray> = buildList {
        for (frame in frames(response, command)) {
            val marker = byteArrayOf(0x49, pid.toByte())
            val index = frame.indices.firstOrNull { at ->
                at + 1 < frame.size && frame[at] == marker[0] && frame[at + 1] == marker[1]
            } ?: continue
            if (!validIso(frame, index, expectedSource)) continue
            if (frame.size > index + 3) add(frame.copyOfRange(index + 2, frame.size - 1))
        }
    }

    fun decode(pid: Int, bytes: ByteArray): Map<String, Any> {
        fun u(index: Int) = bytes[index].toInt() and 0xFF
        fun percentage(value: Int) = value * 100.0 / 255.0
        fun trim(value: Int): Double? = if (value == 0xFF) null else value * 100.0 / 128.0 - 100.0
        return when (pid) {
            0x03 -> mapOf("fuel_system_1" to fuelSystem(u(0)))
            0x04 -> mapOf("engine_load" to percentage(u(0)))
            0x05 -> mapOf("coolant_temperature" to (u(0) - 40.0))
            0x06 -> mapOf("short_term_fuel_trim_bank_1" to (trim(u(0)) ?: return emptyMap()))
            0x07 -> mapOf("long_term_fuel_trim_bank_1" to (trim(u(0)) ?: return emptyMap()))
            0x0C -> mapOf("engine_rpm" to ((u(0) * 256 + u(1)) / 4.0))
            0x0D -> mapOf("vehicle_speed" to u(0).toDouble())
            0x0E -> mapOf("timing_advance" to (u(0) / 2.0 - 64.0))
            0x0F -> mapOf("intake_air_temperature" to (u(0) - 40.0))
            0x10 -> mapOf("mass_air_flow" to ((u(0) * 256 + u(1)) / 100.0))
            0x11 -> mapOf("throttle_position" to percentage(u(0)))
            0x13 -> mapOf(
                "oxygen_sensors_present" to (0 until 8)
                    .filter { u(0) and (1 shl it) != 0 }
                    .map { it + 1 },
            )
            0x14, 0x15 -> {
                val sensor = pid - 0x13
                buildMap {
                    put("oxygen_sensor_${sensor}_voltage", u(0) / 200.0)
                    trim(u(1))?.let { put("oxygen_sensor_${sensor}_short_term_fuel_trim", it) }
                }
            }
            0x1C -> mapOf("obd_standard" to (obdStandards[u(0)] ?: "Unknown (${u(0)})"))
            0x21 -> mapOf("distance_with_mil" to (u(0) * 256 + u(1)).toDouble())
            else -> emptyMap()
        }
    }

    private fun fuelSystem(value: Int): String {
        val states = listOf(
            0x01 to "open_loop_insufficient_temperature",
            0x02 to "closed_loop",
            0x04 to "open_loop_engine_load_or_deceleration",
            0x08 to "open_loop_system_failure",
            0x10 to "closed_loop_with_fault",
        ).filter { value and it.first != 0 }.map { it.second }
        return states.joinToString().ifEmpty { "not_available" }
    }

    fun estimates(values: Map<String, Any>): Map<String, Double> {
        val maf = (values["mass_air_flow"] as? Number)?.toDouble()?.takeIf { it >= 0 }
            ?: return emptyMap()
        val rate = maf * 3600.0 / (14.7 * 745.0)
        val speed = (values["vehicle_speed"] as? Number)?.toDouble()
        return buildMap {
            put("estimated_fuel_rate", rate)
            if (speed != null && speed >= 5) put("estimated_fuel_consumption", rate * 100 / speed)
        }
    }
}
