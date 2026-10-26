package com.dashcamstats.obdlogger

import android.content.Context
import android.os.SystemClock
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID

internal const val TEMPERATURE_CAPTURE_INTERVAL_MS = 30_000L
internal const val TEMPERATURE_CAPTURE_SOURCE = "companion_temperature_v1"
internal const val TEMPERATURE_SENSOR = "soc-thmzone"
internal const val TEMPERATURE_CHUNK_READINGS = 256
internal const val TEMPERATURE_MAX_CHUNKS = 32
internal const val TEMPERATURE_MAX_READINGS = TEMPERATURE_CHUNK_READINGS * TEMPERATURE_MAX_CHUNKS
internal const val TEMPERATURE_MAX_CHUNK_BYTES = 128 * 1024
internal const val TEMPERATURE_MAX_MANIFEST_BYTES = 16 * 1024
private const val MAX_SAFE_INTEGER = 9_007_199_254_740_991L
private val CHUNK_NAME = Regex("samples-([1-9][0-9]{0,15})\\.json")
private val SESSION_ID = Regex("[0-9a-f]{32}")
private val FIRST_CAPTURE_CLOCK = Instant.parse("2020-01-01T00:00:00Z")
private val LAST_CAPTURE_CLOCK = Instant.parse("2100-01-01T00:00:00Z")

internal data class TemperatureReading(
    val sequence: Long,
    val sessionId: String,
    val capturedAtUtc: String,
    val elapsedRealtimeMs: Long,
    val socMillic: Int?,
) {
    fun json(): JSONObject = JSONObject()
        .put("sequence", sequence)
        .put("session_id", sessionId)
        .put("captured_at_utc", capturedAtUtc)
        .put("elapsed_realtime_ms", elapsedRealtimeMs)
        .put("soc_millic", socMillic ?: JSONObject.NULL)
        .put("quality", if (socMillic == null) "unavailable" else "valid")
}

internal data class TemperatureCaptureSnapshot(
    val state: String = "starting",
    val latest: TemperatureReading? = null,
    val retainedReadings: Int = 0,
    val error: String? = null,
) {
    private val captureClockUsable: Boolean get() = latest?.capturedAtUtc?.let {
        runCatching { Instant.parse(it) in FIRST_CAPTURE_CLOCK..LAST_CAPTURE_CLOCK }.getOrDefault(false)
    } == true && state != "clock_unavailable"

    fun json(): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("source", TEMPERATURE_CAPTURE_SOURCE)
        .put("sensor", TEMPERATURE_SENSOR)
        .put("state", state)
        .put("last_captured_at_utc", latest?.capturedAtUtc?.takeIf { captureClockUsable } ?: JSONObject.NULL)
        .put("last_captured_elapsed_ms", latest?.elapsedRealtimeMs?.takeIf { captureClockUsable } ?: JSONObject.NULL)
        .put("interval_ms", TEMPERATURE_CAPTURE_INTERVAL_MS)
        .put("retained_readings", retainedReadings)
        .put("error", error ?: JSONObject.NULL)
}

/** Small local files only. This does not acquire a wake lock or consult OBD/BLE state. */
internal fun readDeviceSocMillic(root: File = File("/sys/class/thermal")): Int? = runCatching {
    root.listFiles()?.asSequence()?.filter { it.name.matches(Regex("thermal_zone[0-9]+")) }
        ?.sortedBy { if (it.name == "thermal_zone0") 0 else 1 }
        ?.take(128)?.firstNotNullOfOrNull { zone ->
            runCatching {
                // An unreadable unrelated zone must not hide the actual SoC sensor.
                // sysfs can report zero or 4096 bytes regardless of actual content.
                fun text(file: File): String = file.inputStream().use { input ->
                    val bytes = ByteArray(129)
                    val count = input.read(bytes)
                    require(count in 1..128)
                    String(bytes, 0, count, Charsets.US_ASCII).trim()
                }
                if (text(File(zone, "type")) == TEMPERATURE_SENSOR) {
                    text(File(zone, "temp")).toIntOrNull()?.takeIf { it in -40_000..200_000 }
                } else null
            }.getOrNull()
        }
}.getOrNull()

internal fun atomicTemperatureWrite(target: File, bytes: ByteArray) {
    check(target.parentFile?.mkdirs() == true || target.parentFile?.isDirectory == true)
    val partial = File(target.parentFile, "${target.name}.partial")
    try {
        FileOutputStream(partial).use { stream -> stream.write(bytes); stream.fd.sync() }
        Files.move(partial.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    } catch (failure: Exception) {
        partial.delete()
        throw failure
    }
}

/**
 * A bounded private journal is authoritative. Its public mirror is immutable closed chunks
 * plus one atomically replaced active chunk and an index published last. Existing closed
 * chunks are never rewritten merely because a new temperature was captured.
 */
internal class TemperatureJournal(
    private val privateDirectory: File,
    private val mirrorDirectory: () -> File?,
    private val write: (File, ByteArray) -> Unit = ::atomicTemperatureWrite,
    private val chunkReadings: Int = TEMPERATURE_CHUNK_READINGS,
    private val maxChunks: Int = TEMPERATURE_MAX_CHUNKS,
    private val read: (File, Int) -> ByteArray = ::readBounded,
) {
    private data class Chunk(val file: String, val readings: List<TemperatureReading>, val bytes: ByteArray) {
        val digest = sha256(bytes)
        fun descriptor(): JSONObject = JSONObject().put("file", file)
            .put("first_sequence", readings.first().sequence).put("last_sequence", readings.last().sequence)
            .put("size_bytes", bytes.size).put("sha256", digest)
    }

    private var loaded = false
    private var highWatermark = 0L
    private var chunks = emptyList<Chunk>()
    private data class VerifiedMirror(val digest: String, val size: Long, val modified: Long)
    private var mirrorIdentity: String? = null
    private val verifiedMirror = mutableMapOf<String, VerifiedMirror>()
    val latest: TemperatureReading? get() = chunks.lastOrNull()?.readings?.lastOrNull()
    val retainedReadings: Int get() = chunks.sumOf { it.readings.size }

    init {
        require(chunkReadings in 1..TEMPERATURE_CHUNK_READINGS)
        require(maxChunks in 1..TEMPERATURE_MAX_CHUNKS)
    }

    fun append(sessionId: String, at: Instant, elapsedMs: Long, millic: Int?): TemperatureReading {
        load()
        require(SESSION_ID.matches(sessionId) && elapsedMs in 0..MAX_SAFE_INTEGER)
        require(millic == null || millic in -40_000..200_000)
        check(highWatermark < MAX_SAFE_INTEGER)
        val sequence = highWatermark + 1
        // Reserve before writing a row. A failed/torn write can leave a sequence gap,
        // but never causes a previously published sample identity to be reused.
        write(File(privateDirectory, "sequence"), "$sequence\n".toByteArray(Charsets.US_ASCII))
        highWatermark = sequence
        val reading = TemperatureReading(sequence, sessionId, at.toString(), elapsedMs, millic)
        val active = chunks.lastOrNull()?.takeIf { it.readings.size < chunkReadings }
        val readings = active?.readings.orEmpty() + reading
        val file = active?.file ?: "samples-$sequence.json"
        val bytes = chunkBytes(readings)
        check(bytes.size <= TEMPERATURE_MAX_CHUNK_BYTES)
        val next = Chunk(file, readings, bytes)
        write(File(privateDirectory, file), bytes)
        val all = if (active != null) chunks.dropLast(1) + next else chunks + next
        chunks = all.takeLast(maxChunks)
        all.dropLast(chunks.size).forEach { File(privateDirectory, it.file).delete() }
        return reading
    }

    fun publish(state: String, at: Instant) {
        load()
        require(state in setOf("running", "sensor_unavailable", "storage_unavailable", "clock_unavailable"))
        val directory = mirrorDirectory() ?: throw RemovableStorageUnavailableException()
        check(directory.mkdirs() || directory.isDirectory)
        val identity = directory.canonicalPath + ":" + runCatching {
            Files.readAttributes(directory.toPath(), BasicFileAttributes::class.java).fileKey()?.toString()
        }.getOrNull()
        if (identity != mirrorIdentity) {
            verifiedMirror.clear()
            mirrorIdentity = identity
        }
        // Write every referenced object before replacing the manifest. If a reader races
        // an active-chunk replacement its hash will fail and it can retry safely.
        for (chunk in chunks) {
            val destination = File(directory, chunk.file)
            val stamp = VerifiedMirror(chunk.digest, destination.length(), destination.lastModified())
            val same = destination.isFile && destination.length() == chunk.bytes.size.toLong() &&
                (verifiedMirror[chunk.file] == stamp || runCatching {
                    sha256(read(destination, TEMPERATURE_MAX_CHUNK_BYTES)) == chunk.digest
                }.getOrDefault(false))
            if (!same) write(destination, chunk.bytes)
            verifiedMirror[chunk.file] = VerifiedMirror(chunk.digest, destination.length(), destination.lastModified())
        }
        val manifest = JSONObject().put("schema_version", 1).put("source", TEMPERATURE_CAPTURE_SOURCE)
            .put("sensor", TEMPERATURE_SENSOR).put("capture_interval_ms", TEMPERATURE_CAPTURE_INTERVAL_MS)
            .put("generated_at_utc", at.toString()).put("collector_state", state)
            .put("retained_readings", retainedReadings)
            .put("chunks", JSONArray(chunks.map { it.descriptor() }))
            .put("latest", latest?.json() ?: JSONObject.NULL)
        val bytes = manifest.toString().toByteArray(Charsets.UTF_8)
        check(bytes.size <= TEMPERATURE_MAX_MANIFEST_BYTES)
        write(File(directory, "manifest.json"), bytes)
        val retained = chunks.map { it.file }.toSet()
        verifiedMirror.keys.retainAll(retained)
        directory.listFiles()?.take(128)?.filter { CHUNK_NAME.matches(it.name) && it.name !in retained }?.forEach { it.delete() }
    }

    private fun load() {
        if (loaded) return
        check(privateDirectory.mkdirs() || privateDirectory.isDirectory)
        val entries = privateDirectory.listFiles().orEmpty()
        check(entries.size <= 256)
        val candidates = entries.filter { CHUNK_NAME.matches(it.name) }
        check(candidates.size <= 128)
        val ordered = candidates.sortedBy { CHUNK_NAME.matchEntire(it.name)!!.groupValues[1].toLong() }
        var recoveredWatermark = 0L
        val recovered = mutableListOf<Chunk>()
        for (file in ordered) {
            val first = CHUNK_NAME.matchEntire(file.name)!!.groupValues[1].toLong()
            check(first in 1..MAX_SAFE_INTEGER)
            val chunk = runCatching { readChunk(file, first) }.getOrNull()
            if (chunk == null) {
                // A damaged active chunk could have contained any of its bounded 256
                // rows. Skip that identity range rather than reuse rows already imported.
                recoveredWatermark = maxOf(recoveredWatermark, minOf(MAX_SAFE_INTEGER, first + chunkReadings - 1))
            } else {
                recoveredWatermark = maxOf(recoveredWatermark, chunk.readings.last().sequence)
                if (chunk.readings.first().sequence > (recovered.lastOrNull()?.readings?.last()?.sequence ?: 0)) {
                    recovered += chunk
                }
            }
        }
        val checkpoint = File(privateDirectory, "sequence")
        val storedWatermark = runCatching {
            val raw = read(checkpoint, 32).toString(Charsets.US_ASCII).trim()
            raw.takeIf { it.matches(Regex("[1-9][0-9]{0,15}")) }?.toLong()?.takeIf { it <= MAX_SAFE_INTEGER }
        }.getOrNull() ?: 0L
        highWatermark = maxOf(recoveredWatermark, storedWatermark)
        // A second crash after this load must not forget identities reserved for a
        // damaged/overlapping chunk that is about to be discarded. Persist first.
        if (highWatermark > storedWatermark) {
            write(checkpoint, "$highWatermark\n".toByteArray(Charsets.US_ASCII))
        }
        chunks = recovered.takeLast(maxChunks)
        val retained = chunks.map { it.file }.toSet()
        ordered.filter { it.name !in retained }.forEach { it.delete() }
        entries.filter {
            it.name == "sequence.partial" || (it.name.endsWith(".partial") && CHUNK_NAME.matches(it.name.removeSuffix(".partial")))
        }.forEach { it.delete() }
        loaded = true
    }

    private fun readChunk(file: File, first: Long): Chunk {
        val bytes = read(file, TEMPERATURE_MAX_CHUNK_BYTES)
        val decoder = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
        val text = decoder.decode(ByteBuffer.wrap(bytes)).toString()
        val json = JSONObject(text)
        require(json.keys().asSequence().toSet() == setOf("schema_version", "readings") && exactLong(json, "schema_version") == 1L)
        val rows = json.getJSONArray("readings")
        require(rows.length() in 1..chunkReadings)
        val readings = (0 until rows.length()).map { index -> reading(rows.getJSONObject(index)) }
        require(readings.first().sequence == first && readings.zipWithNext().all { (a, b) -> b.sequence > a.sequence })
        require(readings.groupBy { it.sessionId }.values.all { group -> group.zipWithNext().all { (a, b) -> b.elapsedRealtimeMs > a.elapsedRealtimeMs } })
        // Reject trailing JSON and coercible values by requiring the canonical record.
        require(chunkBytes(readings).contentEquals(bytes))
        return Chunk(file.name, readings, bytes)
    }

    private fun reading(json: JSONObject): TemperatureReading {
        require(json.keys().asSequence().toSet() == setOf("sequence", "session_id", "captured_at_utc", "elapsed_realtime_ms", "soc_millic", "quality"))
        val sequence = exactLong(json, "sequence").also { require(it in 1..MAX_SAFE_INTEGER) }
        val session = (json.get("session_id") as? String).also { require(it != null && SESSION_ID.matches(it)) }!!
        val timestamp = (json.get("captured_at_utc") as? String).also { require(it != null && Instant.parse(it).toString() == it) }!!
        val elapsed = exactLong(json, "elapsed_realtime_ms").also { require(it in 0..MAX_SAFE_INTEGER) }
        val soc = if (json.isNull("soc_millic")) null else exactLong(json, "soc_millic").also { require(it in -40_000..200_000) }.toInt()
        require(json.get("quality") == if (soc == null) "unavailable" else "valid")
        return TemperatureReading(sequence, session, timestamp, elapsed, soc)
    }

    private fun exactLong(json: JSONObject, key: String): Long = when (val value = json.get(key)) {
        is Int -> value.toLong()
        is Long -> value
        else -> throw IllegalArgumentException("invalid temperature integer")
    }

    companion object {
        private fun chunkBytes(readings: List<TemperatureReading>): ByteArray = JSONObject()
            .put("schema_version", 1).put("readings", JSONArray(readings.map { it.json() })).toString().toByteArray(Charsets.UTF_8)

        private fun readBounded(file: File, maximum: Int): ByteArray {
            require(file.isFile && file.length() in 1..maximum.toLong())
            return file.inputStream().use { input ->
                val output = ByteArrayOutputStream()
                val buffer = ByteArray(4096)
                while (true) {
                    val count = input.read(buffer, 0, minOf(buffer.size, maximum + 1 - output.size()))
                    if (count < 0) break
                    output.write(buffer, 0, count)
                    require(output.size() <= maximum)
                }
                output.toByteArray()
            }
        }

        private fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Owns one independently supervised worker for the admitted service lifetime. No alarm,
 * wake lock or receiver is added: Android suspension is allowed to pause observations.
 * A failed child is awaited before retrying. A process-wide mutex also prevents a new
 * service instance overlapping old non-cancellable filesystem IO during teardown.
 */
internal class TemperatureCapture(
    private val scope: CoroutineScope,
    private val journal: TemperatureJournal,
    private val sensor: () -> Int? = ::readDeviceSocMillic,
    private val now: () -> Instant = Instant::now,
    private val elapsed: () -> Long = SystemClock::elapsedRealtime,
    private val intervalMs: Long = TEMPERATURE_CAPTURE_INTERVAL_MS,
    private val ownership: Mutex = processOwnership,
) {
    private val session = UUID.randomUUID().toString().replace("-", "")
    private var job: Job? = null
    @Volatile private var current = TemperatureCaptureSnapshot()
    fun snapshot(): TemperatureCaptureSnapshot = current

    @Synchronized fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            supervisorScope {
                var retryMs = 5_000L
                while (isActive) {
                    val child = async { ownership.withLock { captureLoop { retryMs = 5_000L } } }
                    try {
                        child.await()
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (_: Exception) {
                        current = current.copy(state = "storage_unavailable", error = "journal_unavailable")
                        delay(retryMs)
                        retryMs = (retryMs * 2).coerceAtMost(60_000L)
                    }
                }
            }
        }
    }

    private suspend fun captureLoop(capturedSuccessfully: () -> Unit) {
        while (currentCoroutineContext().isActive) {
            val millic = try {
                sensor()?.takeIf { it in -40_000..200_000 }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                null
            }
            currentCoroutineContext().ensureActive()
            val captured = now()
            val reading = journal.append(session, captured, elapsed(), millic)
            currentCoroutineContext().ensureActive()
            val state = when {
                captured < FIRST_CAPTURE_CLOCK || captured > LAST_CAPTURE_CLOCK -> "clock_unavailable"
                millic == null -> "sensor_unavailable"
                else -> "running"
            }
            current = TemperatureCaptureSnapshot(state, reading, journal.retainedReadings, if (state == "running") null else state)
            try {
                journal.publish(state, captured)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                current = current.copy(state = "storage_unavailable", error = "mirror_unavailable")
            }
            capturedSuccessfully()
            delay(intervalMs)
        }
    }

    @Synchronized fun close() { job?.cancel(); job = null }

    companion object {
        private val processOwnership = Mutex()
        fun create(context: Context, scope: CoroutineScope): TemperatureCapture = TemperatureCapture(
            scope,
            TemperatureJournal(File(context.noBackupFilesDir, "temperature"), {
                DeviceFiles.removableRootOrNull(context)?.let { File(it, "temperature") }
            }),
        )
    }
}
