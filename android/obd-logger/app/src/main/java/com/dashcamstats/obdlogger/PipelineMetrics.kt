package com.dashcamstats.obdlogger

import org.json.JSONObject

/** One bounded latency distribution retained in memory for operational evidence. */
private class RollingTiming(private val sampleLimit: Int = 256) {
    private val samples = ArrayDeque<Long>()
    private var totalMillis = 0L
    private var maximumMillis = 0L

    @Synchronized
    fun observe(durationMillis: Long) {
        val bounded = durationMillis.coerceIn(0L, 1_000_000_000_000L)
        samples.addLast(bounded)
        if (samples.size > sampleLimit) samples.removeFirst()
        totalMillis = (totalMillis + bounded).coerceAtMost(1_000_000_000_000L)
        maximumMillis = maxOf(maximumMillis, bounded)
    }

    @Synchronized
    fun snapshot(): TimingSummary {
        val ordered = samples.sorted()
        val median = when {
            ordered.isEmpty() -> null
            ordered.size % 2 == 1 -> ordered[ordered.size / 2].toDouble()
            else -> {
                val upper = ordered.size / 2
                (ordered[upper - 1].toDouble() + ordered[upper].toDouble()) / 2.0
            }
        }
        return TimingSummary(
            sampleCount = samples.size.toLong(),
            medianMillis = median,
            maximumMillis = maximumMillis,
            totalMillis = totalMillis,
        )
    }
}

data class TimingSummary(
    val sampleCount: Long,
    val medianMillis: Double?,
    val maximumMillis: Long,
    val totalMillis: Long,
)

data class VoltageProbeSnapshot(
    val result: String,
    val sampleAtUtc: String,
    val parsedVoltage: Double? = null,
    val sanitizedRawResponse: String? = null,
)

/**
 * Fixed-schema, saturating metrics for the direct synchronous OBD pipeline.
 *
 * Arbitrary response bodies, adapter identity, exception text, PID values and addresses never
 * enter this object. The only retained response token is a successfully validated numeric ATRV
 * token such as ``12.7 V``. Latency samples are bounded in count and emitted as aggregates.
 */
class PipelineMetrics(
    private val maximumCounter: Long = Int.MAX_VALUE.toLong(),
    private val monotonicMillis: () -> Long = { System.nanoTime() / 1_000_000L },
) {
    init {
        require(maximumCounter >= 1)
    }

    private val startedAtMillis = monotonicMillis()
    private val counters = linkedMapOf(
        "commands_requested" to 0L,
        "commands_blocked" to 0L,
        "commands_sent" to 0L,
        "commands_completed" to 0L,
        "command_timeouts" to 0L,
        "adapter_local_commands" to 0L,
        "vehicle_bus_commands" to 0L,
        "ble_connection_attempts" to 0L,
        "adapter_targets_resolved" to 0L,
        "gatt_connections_established" to 0L,
        "notification_subscriptions_enabled" to 0L,
        "ble_connection_successes" to 0L,
        "ble_connection_failures" to 0L,
        "voltage_reads_successful" to 0L,
        "voltage_reads_failed" to 0L,
        "invalid_voltage_responses" to 0L,
        "notifications_received" to 0L,
        "notification_fragments_received" to 0L,
        "frames_assembled" to 0L,
        "checksum_failures" to 0L,
        "parse_failures" to 0L,
        "samples_created" to 0L,
        "samples_queued" to 0L,
        "samples_persisted" to 0L,
        "samples_dropped" to 0L,
        "database_write_failures" to 0L,
        "ble_disconnects" to 0L,
        "reconnect_attempts" to 0L,
        "radio_shutdowns" to 0L,
    )
    private val connectionTiming = RollingTiming()
    private val commandTiming = RollingTiming()
    private val voltageCommandTiming = RollingTiming()
    private val connectedTiming = RollingTiming()
    private var queueDepth = 0L
    private var maximumQueueDepth = 0L
    private var lastVoltageProbe: VoltageProbeSnapshot? = null

    @Synchronized
    private fun increment(name: String) {
        counters[name] = ((counters[name] ?: error("unknown metric $name")) + 1)
            .coerceAtMost(maximumCounter)
    }

    fun commandRequested(category: ElmCommandCategory? = null) {
        increment("commands_requested")
        when (category) {
            ElmCommandCategory.ADAPTER_LOCAL -> increment("adapter_local_commands")
            ElmCommandCategory.VEHICLE_BUS -> increment("vehicle_bus_commands")
            null -> Unit
        }
    }

    fun commandCompleted(durationMillis: Long? = null, voltageCommand: Boolean = false) {
        increment("commands_completed")
        durationMillis?.let(commandTiming::observe)
        if (voltageCommand && durationMillis != null) voltageCommandTiming.observe(durationMillis)
    }

    fun commandTimedOut() = increment("command_timeouts")

    fun commandSent() = increment("commands_sent")

    fun commandBlocked() = increment("commands_blocked")

    fun connectionAttempted() = increment("ble_connection_attempts")

    fun adapterTargetResolved() = increment("adapter_targets_resolved")

    fun gattConnectionEstablished() = increment("gatt_connections_established")

    fun notificationSubscriptionEnabled() = increment("notification_subscriptions_enabled")

    fun connectionSucceeded(durationMillis: Long) {
        increment("ble_connection_successes")
        connectionTiming.observe(durationMillis)
    }

    fun connectionFailed() = increment("ble_connection_failures")

    fun connectedSessionClosed(durationMillis: Long) = connectedTiming.observe(durationMillis)

    @Synchronized
    fun voltageReadSucceeded(response: String, voltage: Double, sampleAtUtc: String) {
        increment("voltage_reads_successful")
        lastVoltageProbe = VoltageProbeSnapshot(
            result = "valid",
            sampleAtUtc = sampleAtUtc,
            parsedVoltage = voltage,
            sanitizedRawResponse = ElmProtocol.sanitizedVoltageResponse(response),
        )
    }

    @Synchronized
    fun voltageReadInvalid(sampleAtUtc: String) {
        increment("invalid_voltage_responses")
        lastVoltageProbe = VoltageProbeSnapshot(
            result = "invalid",
            sampleAtUtc = sampleAtUtc,
        )
    }

    @Synchronized
    fun voltageReadFailed(sampleAtUtc: String) {
        increment("voltage_reads_failed")
        lastVoltageProbe = VoltageProbeSnapshot(
            result = "failed",
            sampleAtUtc = sampleAtUtc,
        )
    }

    fun notificationReceived() {
        increment("notifications_received")
        increment("notification_fragments_received")
    }

    fun frameAssembled() = increment("frames_assembled")

    fun parserFailure(checksumFailure: Boolean) {
        increment("parse_failures")
        if (checksumFailure) increment("checksum_failures")
    }

    fun sampleCreated() = increment("samples_created")

    @Synchronized
    fun sampleQueued() {
        increment("samples_queued")
        queueDepth = (queueDepth + 1).coerceAtMost(1)
        maximumQueueDepth = maxOf(maximumQueueDepth, queueDepth)
    }

    @Synchronized
    fun samplePersisted() {
        increment("samples_persisted")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    @Synchronized
    fun sampleDropped() {
        increment("samples_dropped")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    @Synchronized
    fun databaseWriteFailed() {
        increment("database_write_failures")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    fun bleDisconnected() = increment("ble_disconnects")
    fun reconnectAttempted() = increment("reconnect_attempts")
    fun radioShutdownObserved() = increment("radio_shutdowns")

    @Synchronized
    fun snapshot(): PipelineMetricsSnapshot {
        val observationWindow = (monotonicMillis() - startedAtMillis).coerceAtLeast(1L)
        val timings = linkedMapOf(
            "connection_time" to connectionTiming.snapshot(),
            "command_response_time" to commandTiming.snapshot(),
            "voltage_response_time" to voltageCommandTiming.snapshot(),
            "connected_time" to connectedTiming.snapshot(),
        )
        val dutyCycle = (
            timings.getValue("connected_time").totalMillis.toDouble() /
                observationWindow.toDouble() * 100.0
            ).coerceIn(0.0, 100.0)
        return PipelineMetricsSnapshot(
            values = LinkedHashMap(counters),
            queueDepth = queueDepth,
            maximumQueueDepth = maximumQueueDepth,
            timings = timings,
            observationWindowMillis = observationWindow,
            pollingDutyCyclePercent = dutyCycle,
            lastVoltageProbe = lastVoltageProbe,
        )
    }
}

data class PipelineMetricsSnapshot(
    val values: Map<String, Long>,
    val queueDepth: Long,
    val maximumQueueDepth: Long,
    val timings: Map<String, TimingSummary>,
    val observationWindowMillis: Long,
    val pollingDutyCyclePercent: Double,
    val lastVoltageProbe: VoltageProbeSnapshot?,
) {
    fun toJson(): JSONObject = JSONObject().also { body ->
        for ((key, value) in values) body.put(key, value)
        body.put("queue_depth", queueDepth)
        body.put("maximum_queue_depth", maximumQueueDepth)
        body.put("observation_window_ms", observationWindowMillis)
        body.put("polling_duty_cycle_percent", pollingDutyCyclePercent)
        for ((name, timing) in timings) {
            body.put("${name}_samples", timing.sampleCount)
            timing.medianMillis?.let { body.put("median_${name}_ms", it) }
            body.put("maximum_${name}_ms", timing.maximumMillis)
            body.put("total_${name}_ms", timing.totalMillis)
        }
    }

    companion object {
        val EMPTY = PipelineMetrics().snapshot()
    }
}
