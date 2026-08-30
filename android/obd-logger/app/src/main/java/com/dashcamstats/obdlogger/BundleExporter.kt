package com.dashcamstats.obdlogger

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.LinkOption
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.time.ZoneId
import java.util.zip.CRC32
import java.util.zip.GZIPOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

data class ExportedBundle(val file: File, val sha256: String)

internal const val RETAIN_RECENT_EXPORTED_DRIVES = 16
internal const val RETENTION_CANDIDATE_BATCH = 16
internal const val RETENTION_DELETE_BATCH = 4

private val LEGACY_MANIFEST_FIELDS = setOf(
    "schema_version", "bundle_format", "drive_id", "vehicle_id", "adapter_id", "logger_id",
    "logger_version", "start_time_utc", "finish_time_utc", "original_timezone", "start_reason",
    "stop_reason", "obd_protocol", "completion_status", "clean_end", "error_count",
    "sample_count", "diagnostic_count", "created_at_utc", "included_filenames", "units", "files",
)
private val HARDENED_MANIFEST_FIELDS = LEGACY_MANIFEST_FIELDS + setOf(
    "last_sample_at_utc", "last_successful_obd_response_at_utc", "termination_noticed_at_utc",
    "finalised_at_utc", "interruption_reason", "poll_plan_version",
)
private val LEGACY_SUMMARY_FIELDS = setOf(
    "schema_version", "drive_id", "start_time_utc", "finish_time_utc", "duration_s",
    "distance_km", "average_speed_kmh", "maximum_speed_kmh", "average_rpm", "maximum_rpm",
    "idle_duration_s", "estimated_fuel_used_l", "average_fuel_consumption_l_per_100km",
    "maximum_coolant_temperature_c", "maximum_engine_load_pct", "dtcs_observed", "sample_count",
    "missing_data_duration_s", "expected_sample_count", "received_sample_percentage", "clean_end",
)
private val HARDENED_SUMMARY_FIELDS = LEGACY_SUMMARY_FIELDS + setOf(
    "last_sample_at_utc", "termination_noticed_at_utc", "finalised_at_utc", "completion_status",
    "interruption_reason",
)

class BundleExporter(
    private val context: Context,
    private val database: ObdDatabase,
    private val deviceRoot: () -> File = { DeviceFiles.root(context) },
    private val workRoot: File = context.cacheDir,
) {
    private val members = listOf("manifest.json", "samples.ndjson.gz", "diagnostics.json", "summary.json")
    private val units = linkedMapOf(
        "engine_rpm" to "rpm",
        "vehicle_speed" to "km/h",
        "coolant_temperature" to "°C",
        "intake_air_temperature" to "°C",
        "engine_load" to "%",
        "throttle_position" to "%",
        "timing_advance" to "°",
        "mass_air_flow" to "g/s",
        "short_term_fuel_trim_bank_1" to "%",
        "long_term_fuel_trim_bank_1" to "%",
        "oxygen_sensor_1_voltage" to "V",
        "oxygen_sensor_1_short_term_fuel_trim" to "%",
        "oxygen_sensor_2_voltage" to "V",
        "oxygen_sensor_2_short_term_fuel_trim" to "%",
        "adapter_voltage" to "V",
        "estimated_fuel_rate" to "L/h",
        "estimated_fuel_consumption" to "L/100 km",
        "distance_with_mil" to "km",
    )
    private val unitlessSampleFields = listOf(
        "fuel_system_1",
        "oxygen_sensors_present",
        "obd_standard",
    )

    fun export(driveId: String): ExportedBundle {
        require(isSafeDriveId(driveId)) { "drive ID does not match the v1 filename contract" }
        val ready = File(deviceRoot(), "ready").apply { mkdirs() }
        val final = File(ready, "$driveId.obd2.zip")
        if (final.exists()) {
            validate(final, driveId)
            val digest = hashFile(final)
            database.recordVerifiedExport(driveId, digest)
            return ExportedBundle(final, digest)
        }
        val partial = File(ready, "$driveId.obd2.zip.partial")
        val work = File(workRoot, "obd-export-$driveId-${System.nanoTime()}").apply { mkdirs() }
        try {
            val drive = database.drive(driveId)
            check(drive.getString("status") in TERMINAL_DRIVE_STATUSES) {
                "only terminal drives can be exported"
            }
            check(drive.getLong("sample_count") > 0) {
                "zero-sample drives are retained locally and cannot be exported"
            }
            val diagnostics = database.diagnostics(driveId)
            val sampleFile = File(work, "samples.ndjson.gz")
            val rawSamples = FileOutputStream(sampleFile)
            val compressedSamples = GZIPOutputStream(rawSamples)
            val sampleWriter = BufferedWriter(OutputStreamWriter(compressedSamples, Charsets.UTF_8))
            var sampleCount = 0
            try {
                sampleCount = database.forEachSample(driveId) { sample ->
                    sampleWriter.write(sampleForExport(sample).toString())
                    sampleWriter.newLine()
                }
                sampleWriter.flush()
                compressedSamples.finish()
                compressedSamples.flush()
                rawSamples.fd.sync()
            } finally {
                sampleWriter.close()
            }
            val diagnosticFile = File(work, "diagnostics.json")
            writeSynced(
                diagnosticFile,
                JSONObject()
                    .put("schema_version", 1)
                    .put("drive_id", driveId)
                    .put("events", JSONArray(diagnostics)),
            )
            val summaryFile = File(work, "summary.json")
            writeSynced(summaryFile, summary(drive, diagnostics))

            val counts = mapOf(
                "samples.ndjson.gz" to sampleCount,
                "diagnostics.json" to diagnostics.size,
                "summary.json" to 1,
            )
            val files = JSONObject()
            for ((name, count) in counts) {
                val file = File(work, name)
                files.put(
                    name,
                    JSONObject()
                        .put("size_bytes", file.length())
                        .put("sha256", hashFile(file))
                        .put("record_count", count),
                )
            }
            val createdAtUtc = exportCreatedAtUtc(drive)
            val manifest = JSONObject()
                .put("schema_version", 1)
                .put("bundle_format", "dashcam-obd")
                .put("drive_id", driveId)
                .put("vehicle_id", drive.getString("vehicle_id"))
                .put("adapter_id", drive.optNullable("adapter_id"))
                .put("logger_id", drive.getString("logger_id"))
                .put("logger_version", drive.getString("logger_version"))
                .put("poll_plan_version", ObdPollPlan.VERSION)
                .put("start_time_utc", drive.getString("start_time_utc"))
                .put("finish_time_utc", drive.getString("finish_time_utc"))
                .put("last_sample_at_utc", drive.optNullable("last_sample_at_utc"))
                .put(
                    "last_successful_obd_response_at_utc",
                    drive.optNullable("last_successful_response_at_utc"),
                )
                .put(
                    "termination_noticed_at_utc",
                    drive.optNullable("termination_noticed_at_utc"),
                )
                .put("finalised_at_utc", drive.optNullable("finalised_at_utc"))
                .put("original_timezone", drive.optNullable("original_timezone"))
                .put("start_reason", drive.getString("start_reason"))
                .put("stop_reason", drive.optNullable("stop_reason"))
                .put("obd_protocol", drive.optNullable("obd_protocol"))
                .put(
                    "completion_status",
                    completionStatus(
                        if (drive.isNull("stop_reason")) null else drive.getString("stop_reason"),
                        drive.getString("status"),
                    ),
                )
                .put("interruption_reason", drive.optNullable("interruption_reason"))
                .put("clean_end", drive.optInt("clean_end") == 1)
                .put("error_count", drive.getLong("error_count"))
                .put("sample_count", sampleCount)
                .put("diagnostic_count", diagnostics.size)
                .put("created_at_utc", createdAtUtc)
                .put("included_filenames", JSONArray(members))
                .put("units", JSONObject(units as Map<*, *>))
                .put("files", files)
            writeSynced(File(work, "manifest.json"), manifest)

            if (partial.exists() && !partial.delete()) error("could not clear stale partial bundle")
            writeStoredZip(partial, members.map { name -> name to File(work, name) })
            validate(partial, driveId)
            try {
                Files.move(partial.toPath(), final.toPath(), StandardCopyOption.ATOMIC_MOVE)
            } catch (error: Exception) {
                throw IllegalStateException("could not atomically publish OBD bundle", error)
            }
            val digest = hashFile(final)
            database.recordVerifiedExport(driveId, digest)
            return ExportedBundle(final, digest)
        } finally {
            work.deleteRecursively()
        }
    }

    fun enforceRetention(): Int {
        val root = deviceRoot()
        val ready = File(root, "ready")
        val receipts = File(root, "receipts")
        val candidates = database.exportedRetentionCandidates(
            retainMostRecent = RETAIN_RECENT_EXPORTED_DRIVES,
            maximumCandidates = RETENTION_CANDIDATE_BATCH,
        )
        val verified = retentionReceiptsSafeToPrune(
            candidates = candidates,
            maximumDeletes = RETENTION_DELETE_BATCH,
            bundleFile = { driveId -> File(ready, "$driveId.obd2.zip") },
            receiptFile = { driveId -> File(receipts, "$driveId.verified.json") },
            validatePresentBundle = { file, driveId -> validate(file, driveId) },
            hashPresentBundle = ::hashFile,
            validateReceipt = { file, candidate ->
                isExactServerVerificationReceipt(file, receipts, candidate)
            },
        )
        val deleted = database.pruneVerifiedExportedDrives(
            verified = verified,
            retainAtLeast = RETAIN_RECENT_EXPORTED_DRIVES,
            maximumDeletes = RETENTION_DELETE_BATCH,
        )
        for (candidate in deleted) {
            val receipt = File(receipts, "${candidate.driveId}.verified.json")
            runCatching {
                val receiptCandidate = ExportedDriveRetentionCandidate(
                    driveId = candidate.driveId,
                    finishedAtUtc = "1970-01-01T00:00:00Z",
                    bundleSha256 = candidate.bundleSha256,
                )
                if (isExactServerVerificationReceipt(receipt, receipts, receiptCandidate)) {
                    Files.deleteIfExists(receipt.toPath())
                }
            }
        }
        return deleted.size
    }

    /**
     * Re-arm an export only when both durable proofs are absent. A present archive is never
     * overwritten here, even if malformed: it remains visible for operator recovery rather
     * than being silently replaced. The normal export path recreates only an absent file.
     */
    fun reconcileMissingExports(): Int {
        val root = deviceRoot()
        val ready = File(root, "ready")
        val receipts = File(root, "receipts")
        var reset = 0
        var afterFinishedAtUtc: String? = null
        var afterDriveId: String? = null
        while (true) {
            val candidates = database.exportedRecoveryCandidates(
                maximumCandidates = 64,
                afterFinishedAtUtc = afterFinishedAtUtc,
                afterDriveId = afterDriveId,
            )
            if (candidates.isEmpty()) break
            for (candidate in candidates) {
                val bundle = File(ready, "${candidate.driveId}.obd2.zip")
                if (!Files.exists(bundle.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                    val receipt = File(receipts, "${candidate.driveId}.verified.json")
                    if (
                        !isExactServerVerificationReceipt(receipt, receipts, candidate) &&
                        database.resetMissingExport(candidate.driveId, candidate.bundleSha256)
                    ) {
                        reset += 1
                    }
                }
            }
            val last = candidates.last()
            afterFinishedAtUtc = last.finishedAtUtc
            afterDriveId = last.driveId
            if (candidates.size < 64) break
        }
        return reset
    }

    private fun sampleForExport(row: JSONObject): JSONObject {
        val result = JSONObject()
        for (key in listOf("sample_id", "drive_id", "timestamp_utc", "sequence", "ecu_data_status")) {
            result.put(key, row.get(key))
        }
        result.put("quality", JSONObject(row.getString("quality_json")))
        for (key in units.keys + unitlessSampleFields) {
            if (!row.has(key) || row.isNull(key)) continue
            if (key == "oxygen_sensors_present") {
                result.put(key, JSONArray(row.getString(key)))
            } else {
                result.put(key, row.get(key))
            }
        }
        return result
    }

    private fun summary(
        drive: JSONObject,
        diagnostics: List<JSONObject>,
    ): JSONObject {
        val start = Instant.parse(drive.getString("start_time_utc"))
        val finish = Instant.parse(drive.getString("finish_time_utc"))
        var distance = 0.0
        var idle = 0.0
        var fuel = 0.0
        var missing = 0.0
        var distanceIntervals = 0
        var idleEvidence = false
        var fuelEvidence = false
        var speedSum = 0.0
        var speedCount = 0
        var maximumSpeed: Double? = null
        var rpmSum = 0.0
        var rpmCount = 0
        var maximumRpm: Double? = null
        var maximumCoolant: Double? = null
        var maximumLoad: Double? = null
        var previous: JSONObject? = null
        val sampleCount = database.forEachSample(drive.getString("drive_id")) { current ->
            current.number("vehicle_speed")?.let { speed ->
                speedSum += speed
                speedCount += 1
                maximumSpeed = maximumSpeed?.let { maxOf(it, speed) } ?: speed
            }
            current.number("engine_rpm")?.let { rpm ->
                rpmSum += rpm
                rpmCount += 1
                maximumRpm = maximumRpm?.let { maxOf(it, rpm) } ?: rpm
            }
            current.number("coolant_temperature")?.let { value ->
                maximumCoolant = maximumCoolant?.let { maxOf(it, value) } ?: value
            }
            current.number("engine_load")?.let { value ->
                maximumLoad = maximumLoad?.let { maxOf(it, value) } ?: value
            }
            previous?.let { first ->
                val gap = java.time.Duration.between(
                    Instant.parse(first.getString("timestamp_utc")),
                    Instant.parse(current.getString("timestamp_utc")),
                ).toMillis() / 1000.0
                if (gap > 0) {
                    if (gap > 5) missing += gap - 5
                    if (gap <= 15) {
                        val firstSpeed = first.number("vehicle_speed")
                        val secondSpeed = current.number("vehicle_speed")
                        if (firstSpeed != null && secondSpeed != null) {
                            distance += (firstSpeed + secondSpeed) / 2 * gap / 3600
                            distanceIntervals += 1
                        }
                        val firstRpm = first.number("engine_rpm")
                        if (firstRpm != null && firstSpeed != null) {
                            idleEvidence = true
                            if (firstRpm > 300 && firstSpeed < 1) idle += gap
                        }
                        first.number("estimated_fuel_rate")?.takeIf { it >= 0 }?.let {
                            fuelEvidence = true
                            fuel += it * gap / 3600
                        }
                    }
                }
            }
            previous = current
        }
        val duration = java.time.Duration.between(start, finish).toMillis().coerceAtLeast(0) / 1000.0
        val expected = maxOf(if (duration > 0) (duration / 5).toInt() + 1 else 0, sampleCount)
        val dtcs = sortedSetOf<String>()
        for (event in diagnostics) {
            if (event.getString("kind") in setOf("confirmed_dtcs", "pending_dtcs", "permanent_dtcs")) {
                val codes = event.getJSONObject("payload").optJSONArray("codes") ?: continue
                for (index in 0 until codes.length()) dtcs += codes.getString(index)
            }
        }
        return JSONObject()
            .put("schema_version", 1)
            .put("drive_id", drive.getString("drive_id"))
            .put("start_time_utc", start.toString())
            .put("finish_time_utc", finish.toString())
            .put("last_sample_at_utc", drive.optNullable("last_sample_at_utc"))
            .put("termination_noticed_at_utc", drive.optNullable("termination_noticed_at_utc"))
            .put("finalised_at_utc", drive.optNullable("finalised_at_utc"))
            .put("completion_status", completionStatus(
                if (drive.isNull("stop_reason")) null else drive.getString("stop_reason"),
                drive.getString("status"),
            ))
            .put("interruption_reason", drive.optNullable("interruption_reason"))
            .put("duration_s", duration)
            .put("distance_km", if (distanceIntervals > 0) distance else JSONObject.NULL)
            .put("average_speed_kmh", if (speedCount > 0) speedSum / speedCount else JSONObject.NULL)
            .put("maximum_speed_kmh", maximumSpeed ?: JSONObject.NULL)
            .put("average_rpm", if (rpmCount > 0) rpmSum / rpmCount else JSONObject.NULL)
            .put("maximum_rpm", maximumRpm ?: JSONObject.NULL)
            .put("idle_duration_s", if (idleEvidence) idle else JSONObject.NULL)
            .put(
                "estimated_fuel_used_l",
                if (fuelEvidence) fuel else JSONObject.NULL,
            )
            .put(
                "average_fuel_consumption_l_per_100km",
                if (distance > 0 && fuelEvidence) {
                    fuel * 100 / distance
                } else {
                    JSONObject.NULL
                },
            )
            .put("maximum_coolant_temperature_c", maximumCoolant ?: JSONObject.NULL)
            .put("maximum_engine_load_pct", maximumLoad ?: JSONObject.NULL)
            .put("dtcs_observed", JSONArray(dtcs.toList()))
            .put("sample_count", sampleCount)
            .put("missing_data_duration_s", missing)
            .put("expected_sample_count", expected)
            .put(
                "received_sample_percentage",
                if (expected > 0) (sampleCount * 100.0 / expected).coerceAtMost(100.0) else 0.0,
            )
            .put("clean_end", drive.optInt("clean_end") == 1)
    }

    private fun validate(file: File, expectedDriveId: String) {
        ZipFile(file).use { zip ->
            val entries = zip.entries().toList()
            check(entries.map { it.name }.toSet() == members.toSet() && entries.size == members.size)
            check(entries.all { !it.isDirectory && it.method == ZipEntry.STORED && !it.name.contains('/') })
            val manifest = zip.getInputStream(zip.getEntry("manifest.json")).bufferedReader().use {
                JSONObject(it.readText())
            }
            val manifestFields = manifest.keys().asSequence().toSet()
            check(manifestFields == LEGACY_MANIFEST_FIELDS || manifestFields == HARDENED_MANIFEST_FIELDS) {
                "manifest fields do not match an exact supported v1 shape"
            }
            val hardened = manifestFields == HARDENED_MANIFEST_FIELDS
            check(manifest.getInt("schema_version") == 1)
            check(manifest.getString("drive_id") == expectedDriveId)
            check(manifest.getString("bundle_format") == "dashcam-obd")
            val included = manifest.getJSONArray("included_filenames")
            check(included.length() == members.size)
            check((0 until included.length()).map(included::getString).toSet() == members.toSet())
            val manifestUnits = manifest.getJSONObject("units")
            check(manifestUnits.length() == units.size)
            check(units.all { (key, value) -> manifestUnits.optString(key) == value })
            val files = manifest.getJSONObject("files")
            check(files.keys().asSequence().toSet() == members.drop(1).toSet())
            for (name in members.drop(1)) {
                val entry = zip.getEntry(name)
                val expected = files.getJSONObject(name)
                check(
                    expected.keys().asSequence().toSet() ==
                        setOf("size_bytes", "sha256", "record_count"),
                )
                check(expected.getLong("size_bytes") == entry.size)
                check(expected.getLong("record_count") >= 0)
                val digest = zip.getInputStream(entry).use { hashStream(it) }
                check(expected.getString("sha256") == digest)
            }
            val summary = zip.getInputStream(zip.getEntry("summary.json")).bufferedReader().use {
                JSONObject(it.readText())
            }
            check(
                summary.keys().asSequence().toSet() ==
                    if (hardened) HARDENED_SUMMARY_FIELDS else LEGACY_SUMMARY_FIELDS,
            ) { "summary fields do not match the manifest's exact v1 shape" }
            check(summary.getInt("schema_version") == 1)
            check(summary.getString("drive_id") == expectedDriveId)
            check(summary.getString("start_time_utc") == manifest.getString("start_time_utc"))
            check(summary.getString("finish_time_utc") == manifest.getString("finish_time_utc"))
            check(summary.getLong("sample_count") == manifest.getLong("sample_count"))
            check(summary.getBoolean("clean_end") == manifest.getBoolean("clean_end"))
            validateLifecycleShape(manifest, summary, hardened)
        }
    }

    private fun validateLifecycleShape(
        manifest: JSONObject,
        summary: JSONObject,
        hardened: Boolean,
    ) {
        val start = Instant.parse(manifest.getString("start_time_utc"))
        val finish = Instant.parse(manifest.getString("finish_time_utc"))
        check(!finish.isBefore(start)) { "manifest drive time range is reversed" }
        if (!hardened) return
        check(
            manifest.get("poll_plan_version") is Number &&
                manifest.getInt("poll_plan_version") == ObdPollPlan.VERSION,
        )
        val lastSample = manifest.instantOrNull("last_sample_at_utc")
        val lastResponse = manifest.instantOrNull("last_successful_obd_response_at_utc")
        val noticed = manifest.instantOrNull("termination_noticed_at_utc")
        val finalised = manifest.instantOrNull("finalised_at_utc")
        if (lastSample != null) check(!lastSample.isBefore(start) && !lastSample.isAfter(finish))
        val responseUpper = noticed ?: finalised ?: Instant.parse(manifest.getString("created_at_utc"))
        if (lastResponse != null) {
            check(!lastResponse.isBefore(start) && !lastResponse.isAfter(responseUpper))
        }
        if (noticed != null) check(!noticed.isBefore(finish))
        if (finalised != null && noticed != null) check(!finalised.isBefore(noticed))
        val created = Instant.parse(manifest.getString("created_at_utc"))
        if (finalised != null) check(!created.isBefore(finalised))
        val completion = manifest.getString("completion_status")
        check(completion in setOf("complete", "interrupted", "recovered"))
        val interruption = manifest.stringOrNull("interruption_reason")
        if (manifest.getBoolean("clean_end")) {
            check(completion == "complete" && interruption == null)
        } else {
            check(completion != "complete" && !interruption.isNullOrBlank())
        }
        for (key in HARDENED_SUMMARY_FIELDS - LEGACY_SUMMARY_FIELDS) {
            check(summary.get(key).toString() == manifest.get(key).toString()) {
                "summary $key does not match manifest"
            }
        }
    }

    private fun writeSynced(file: File, body: JSONObject) {
        FileOutputStream(file).use {
            it.write(body.toString().toByteArray(Charsets.UTF_8))
            it.fd.sync()
        }
    }

    private fun exportCreatedAtUtc(drive: JSONObject): String = listOfNotNull(
        Instant.now(),
        Instant.parse(drive.getString("start_time_utc")),
        drive.stringOrNull("finish_time_utc")?.let(Instant::parse),
        drive.stringOrNull("last_sample_at_utc")?.let(Instant::parse),
        drive.stringOrNull("last_successful_response_at_utc")?.let(Instant::parse),
        drive.stringOrNull("termination_noticed_at_utc")?.let(Instant::parse),
        drive.stringOrNull("finalised_at_utc")?.let(Instant::parse),
    ).maxOrNull()!!.toString()

    private fun hashFile(file: File): String = file.inputStream().use(::hashStream)
    private fun hashStream(stream: java.io.InputStream): String {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val read = stream.read(buffer)
            if (read < 0) break
            digest.update(buffer, 0, read)
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}

/**
 * A present bundle must still validate and match its locally persisted hash. If it has been
 * removed, an exact server acknowledgement written after durable registration is required.
 */
internal fun retentionReceiptsSafeToPrune(
    candidates: List<ExportedDriveRetentionCandidate>,
    maximumDeletes: Int,
    bundleFile: (String) -> File,
    receiptFile: (String) -> File,
    validatePresentBundle: (File, String) -> Unit,
    hashPresentBundle: (File) -> String,
    validateReceipt: (File, ExportedDriveRetentionCandidate) -> Boolean,
): List<VerifiedExportedDrive> {
    require(maximumDeletes >= 1)
    val verified = mutableListOf<VerifiedExportedDrive>()
    for (candidate in candidates) {
        if (verified.size >= maximumDeletes) break
        val file = bundleFile(candidate.driveId)
        val bundlePath = file.toPath()
        val hasProof = if (Files.exists(bundlePath, LinkOption.NOFOLLOW_LINKS)) {
            val matches = runCatching {
                check(isSafeRegularChild(file, checkNotNull(file.parentFile))) {
                    "retained bundle path is not a safe regular file"
                }
                validatePresentBundle(file, candidate.driveId)
                hashPresentBundle(file) == candidate.bundleSha256
            }.getOrDefault(false)
            matches
        } else {
            runCatching { validateReceipt(receiptFile(candidate.driveId), candidate) }
                .getOrDefault(false)
        }
        if (!hasProof) continue
        verified += VerifiedExportedDrive(candidate.driveId, candidate.bundleSha256)
    }
    return verified
}

internal fun isSafeRegularChild(file: File, expectedParent: File): Boolean = runCatching {
    val parentPath = expectedParent.toPath()
    val path = file.toPath()
    check(!Files.isSymbolicLink(parentPath))
    check(Files.isDirectory(parentPath, LinkOption.NOFOLLOW_LINKS))
    check(!Files.isSymbolicLink(path))
    check(Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
    check(file.canonicalFile.parentFile == expectedParent.canonicalFile)
    true
}.getOrDefault(false)

private val jsonKeyPattern = Regex("\"(?:[^\"\\\\]|\\\\.)*\"\\s*:")
private val receiptKeys = setOf("schema_version", "drive_id", "bundle_sha256")

internal fun isExactServerVerificationReceipt(
    receipt: File,
    receiptsRoot: File,
    candidate: ExportedDriveRetentionCandidate,
): Boolean = runCatching {
    validateExactServerVerificationReceipt(receipt, receiptsRoot, candidate)
    true
}.getOrDefault(false)

internal fun validateExactServerVerificationReceipt(
    receipt: File,
    receiptsRoot: File,
    candidate: ExportedDriveRetentionCandidate,
) {
    check(isSafeDriveId(candidate.driveId))
    val rootPath = receiptsRoot.toPath()
    check(!Files.isSymbolicLink(rootPath))
    check(Files.isDirectory(rootPath, LinkOption.NOFOLLOW_LINKS))
    val path = receipt.toPath()
    check(!Files.isSymbolicLink(path))
    check(Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
    check(receipt.canonicalFile.parentFile == receiptsRoot.canonicalFile)
    val size = Files.size(path)
    check(size in 1..512)
    val bytes = Files.readAllBytes(path)
    check(bytes.size.toLong() == size)
    val text = Charsets.UTF_8.newDecoder()
        .onMalformedInput(CodingErrorAction.REPORT)
        .onUnmappableCharacter(CodingErrorAction.REPORT)
        .decode(ByteBuffer.wrap(bytes))
        .toString()
    check(jsonKeyPattern.findAll(text).count() == 3) { "receipt has duplicate or extra keys" }
    val body = JSONObject(text)
    check(body.keys().asSequence().toSet() == receiptKeys)
    check(
        body.get("schema_version").let { value ->
            value is Number && value.toString() == "1"
        },
    )
    check(body.get("drive_id") is String && body.getString("drive_id") == candidate.driveId)
    check(
        body.get("bundle_sha256") is String &&
            body.getString("bundle_sha256") == candidate.bundleSha256,
    )
}

private val safeDriveId = Regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

internal fun isSafeDriveId(value: String): Boolean = safeDriveId.matches(value)

internal fun completionStatus(stopReason: String?, persistedStatus: String? = null): String = when {
    persistedStatus == "failed" -> "interrupted"
    persistedStatus in TERMINAL_DRIVE_STATUSES -> checkNotNull(persistedStatus)
    stopReason == null -> "complete"
    else -> driveTerminalPolicy(stopReason).status
}

internal fun writeStoredZip(target: File, members: List<Pair<String, File>>) {
    FileOutputStream(target).use { raw ->
        val zip = ZipOutputStream(raw)
        try {
            for ((name, source) in members) addStored(zip, source, name)
            zip.finish()
            zip.flush()
            // ZipOutputStream.close() closes raw. Sync only after finish/flush and while the
            // underlying descriptor is still valid, then let both streams close normally.
            raw.fd.sync()
        } finally {
            zip.close()
        }
    }
}

private fun addStored(zip: ZipOutputStream, source: File, name: String) {
    val crc = CRC32()
    FileInputStream(source).use { input ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            crc.update(buffer, 0, read)
        }
    }
    val entry = ZipEntry(name).apply {
        method = ZipEntry.STORED
        size = source.length()
        compressedSize = source.length()
        this.crc = crc.value
        time = 0
    }
    zip.putNextEntry(entry)
    source.inputStream().use { it.copyTo(zip) }
    zip.closeEntry()
}

private fun JSONObject.optNullable(key: String): Any =
    if (has(key) && !isNull(key)) get(key) else JSONObject.NULL

private fun JSONObject.stringOrNull(key: String): String? =
    if (has(key) && !isNull(key)) getString(key) else null

private fun JSONObject.instantOrNull(key: String): Instant? = stringOrNull(key)?.let(Instant::parse)

private fun JSONObject.number(key: String): Double? =
    if (has(key) && !isNull(key)) optDouble(key).takeUnless(Double::isNaN) else null
