package com.dashcamstats.obdlogger

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardCopyOption
import java.time.Duration
import java.time.Instant

data class IngestionRequest(
    val requestId: String,
    val requestedAtUtc: String,
    val deadlineAtUtc: String,
)

data class IngestionAckMetadata(
    val driveId: String? = null,
    val lastSampleAtUtc: String? = null,
    val bundleFilename: String? = null,
    val bundleSha256: String? = null,
)

sealed interface IngestionRequestRead {
    data object Absent : IngestionRequestRead
    data class Valid(val request: IngestionRequest) : IngestionRequestRead
    data class Invalid(val reason: String) : IngestionRequestRead
}

enum class IngestionQuiesceDecision { RESUME, QUIESCE, BLOCK_INVALID_REQUEST }

internal const val INGESTION_MAXIMUM_LEASE_SECONDS = 600L
internal const val INGESTION_CLOCK_SKEW_SECONDS = 60L

/**
 * The request deadline is the overall radio/ingestion hold lease. Small clock skew is tolerated,
 * but a materially future request, a forward clock jump, or an overlong lease fails open only
 * after the correlated control files have been removed.
 */
internal fun ingestionLeaseExpiryReason(
    request: IngestionRequest,
    nowUtc: Instant,
): String? {
    val requested = Instant.parse(request.requestedAtUtc)
    val deadline = Instant.parse(request.deadlineAtUtc)
    val duration = Duration.between(requested, deadline)
    if (duration < Duration.ofSeconds(1) || duration > Duration.ofSeconds(INGESTION_MAXIMUM_LEASE_SECONDS)) {
        return "ingestion request lease duration is outside the bounded range"
    }
    if (requested.isAfter(nowUtc.plusSeconds(INGESTION_CLOCK_SKEW_SECONDS))) {
        return "ingestion request time is materially in the future"
    }
    if (nowUtc.isAfter(deadline.plusSeconds(INGESTION_CLOCK_SKEW_SECONDS))) {
        return "ingestion request lease expired"
    }
    return null
}

internal fun boundedRedactedError(
    raw: String?,
    maximum: Int = 240,
    fallback: String = "operation failed",
): String {
    require(maximum in 1..512)
    return (raw ?: fallback)
        .replace(Regex("[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"), "<adapter>")
        .replace(
            Regex("(?i)(api[_-]?key|authorization|token)\\s*[:=]\\s*\\S+"),
        ) { match -> "${match.groupValues[1]}=<redacted>" }
        .map { character -> if (character.code < 32 || character.code == 127) ' ' else character }
        .joinToString("")
        .take(maximum)
        .ifEmpty { fallback.take(maximum) }
}

internal fun ingestionQuiesceDecision(read: IngestionRequestRead): IngestionQuiesceDecision =
    when (read) {
        IngestionRequestRead.Absent -> IngestionQuiesceDecision.RESUME
        is IngestionRequestRead.Valid -> IngestionQuiesceDecision.QUIESCE
        is IngestionRequestRead.Invalid -> IngestionQuiesceDecision.BLOCK_INVALID_REQUEST
    }

/** Strict, fixed-path, atomic coordination with the dashcam ingestion controller. */
object IngestionQuiesceFiles {
    private const val REQUEST_NAME = "ingestion-request.json"
    private const val ACK_NAME = "ingestion-ack.json"
    private const val EXPIRED_REQUEST_NAME = "$REQUEST_NAME.expired"
    private const val BOOT_EXPIRED_REQUEST_NAME = "$REQUEST_NAME.boot-expired"
    private const val MAXIMUM_CONTROL_BYTES = 2_048L
    private val requestKeys = setOf(
        "schema_version",
        "request_id",
        "action",
        "requested_at_utc",
        "deadline_at_utc",
    )
    private val ackKeys = setOf(
        "schema_version",
        "request_id",
        "state",
        "ready_at_utc",
        "drive_id",
        "last_sample_at_utc",
        "bundle_filename",
        "bundle_sha256",
        "error",
    )
    private val safeRequestId = Regex("^[A-Za-z0-9_-]{1,64}$")
    private val jsonKeyPattern = Regex("\"(?:[^\"\\\\]|\\\\.)*\"\\s*:")

    fun controlRoot(deviceRoot: File): File = File(deviceRoot, "control")
    fun requestFile(deviceRoot: File): File = File(controlRoot(deviceRoot), REQUEST_NAME)
    fun acknowledgementFile(deviceRoot: File): File = File(controlRoot(deviceRoot), ACK_NAME)

    fun readRequest(
        deviceRoot: File,
        nowUtc: Instant = Instant.now(),
    ): IngestionRequestRead {
        val control = controlRoot(deviceRoot)
        val request = requestFile(deviceRoot)
        if (!Files.exists(request.toPath(), LinkOption.NOFOLLOW_LINKS)) {
            cleanupExpiredMarkers(control)
            return IngestionRequestRead.Absent
        }
        return runCatching { parseRequestFile(request, control) }.fold(
            onSuccess = { parsed ->
                val expiry = ingestionLeaseExpiryReason(parsed, nowUtc)
                if (expiry == null) {
                    IngestionRequestRead.Valid(parsed)
                } else if (clearExpiredLease(deviceRoot, parsed)) {
                    IngestionRequestRead.Absent
                } else {
                    IngestionRequestRead.Invalid("expired ingestion request could not be cleared")
                }
            },
            onFailure = { error ->
                if (clearStaleInvalidLease(deviceRoot, request, nowUtc)) {
                    IngestionRequestRead.Absent
                } else {
                    IngestionRequestRead.Invalid(
                        boundedRedactedError(
                            error.message,
                            maximum = 120,
                            fallback = "invalid request",
                        ),
                    )
                }
            },
        )
    }

    private fun parseRequestFile(request: File, control: File): IngestionRequest {
        requireSafeControlFile(request, control)
        val text = readBoundedUtf8(request)
        check(jsonKeyPattern.findAll(text).count() == requestKeys.size) {
            "request has duplicate or extra fields"
        }
        val body = JSONObject(text)
        check(body.keys().asSequence().toSet() == requestKeys) {
            "request fields do not match schema version 1"
        }
        check(body.get("schema_version").let { it is Number && it.toString() == "1" }) {
            "schema_version must be the integer 1"
        }
        check(body.get("request_id") is String)
        val requestId = body.getString("request_id")
        check(safeRequestId.matches(requestId)) { "request_id is not a safe bounded token" }
        check(body.get("action") is String && body.getString("action") == "prepare_for_ingest") {
            "action must be prepare_for_ingest"
        }
        check(body.get("requested_at_utc") is String)
        check(body.get("deadline_at_utc") is String)
        val requestedAt = Instant.parse(body.getString("requested_at_utc"))
        val deadlineAt = Instant.parse(body.getString("deadline_at_utc"))
        return IngestionRequest(
            requestId = requestId,
            requestedAtUtc = requestedAt.toString(),
            deadlineAtUtc = deadlineAt.toString(),
        )
    }

    fun publishReady(
        deviceRoot: File,
        request: IngestionRequest,
        metadata: IngestionAckMetadata = IngestionAckMetadata(),
        readyAtUtc: String = Instant.now().toString(),
    ) {
        metadata.driveId?.let { check(isSafeDriveId(it)) }
        metadata.lastSampleAtUtc?.let(Instant::parse)
        check((metadata.bundleFilename == null) == (metadata.bundleSha256 == null)) {
            "bundle filename and digest must be supplied together"
        }
        metadata.bundleFilename?.let { name ->
            check(name == "${metadata.driveId}.obd2.zip") { "bundle filename does not match drive" }
        }
        metadata.bundleSha256?.let { check(Regex("^[0-9a-f]{64}$").matches(it)) }
        publishAcknowledgement(
            deviceRoot = deviceRoot,
            requestId = request.requestId,
            state = "ready",
            readyAtUtc = readyAtUtc,
            metadata = metadata,
            error = null,
        )
    }

    fun publishFailed(
        deviceRoot: File,
        request: IngestionRequest,
        error: String,
        failedAtUtc: String = Instant.now().toString(),
    ) {
        val redacted = boundedRedactedError(error, fallback = "quiesce failed")
        publishAcknowledgement(
            deviceRoot = deviceRoot,
            requestId = request.requestId,
            state = "failed",
            readyAtUtc = failedAtUtc,
            metadata = IngestionAckMetadata(),
            error = redacted,
        )
    }

    private fun publishAcknowledgement(
        deviceRoot: File,
        requestId: String,
        state: String,
        readyAtUtc: String,
        metadata: IngestionAckMetadata,
        error: String?,
    ) {
        check(safeRequestId.matches(requestId))
        check(state == "ready" || state == "failed")
        val control = controlRoot(deviceRoot).apply { mkdirs() }
        check(control.isDirectory && !Files.isSymbolicLink(control.toPath())) {
            "control root is not a safe directory"
        }
        val body = JSONObject()
            .put("schema_version", 1)
            .put("request_id", requestId)
            .put("state", state)
            .put("ready_at_utc", Instant.parse(readyAtUtc).toString())
            .put("drive_id", metadata.driveId ?: JSONObject.NULL)
            .put("last_sample_at_utc", metadata.lastSampleAtUtc ?: JSONObject.NULL)
            .put("bundle_filename", metadata.bundleFilename ?: JSONObject.NULL)
            .put("bundle_sha256", metadata.bundleSha256 ?: JSONObject.NULL)
            .put("error", error ?: JSONObject.NULL)
        val partial = File(control, "$ACK_NAME.partial")
        FileOutputStream(partial).use { output ->
            output.write(body.toString().toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        val target = acknowledgementFile(deviceRoot)
        try {
            Files.move(
                partial.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (error: Exception) {
            Files.deleteIfExists(partial.toPath())
            throw IllegalStateException("could not atomically publish ingestion acknowledgement", error)
        }
        check(validateAcknowledgement(deviceRoot, requestId, state)) {
            "published ingestion acknowledgement did not validate"
        }
    }

    fun isReadyFor(deviceRoot: File, requestId: String): Boolean =
        validateAcknowledgement(deviceRoot, requestId, "ready")

    fun isFailedFor(deviceRoot: File, requestId: String): Boolean =
        validateAcknowledgement(deviceRoot, requestId, "failed")

    /**
     * A reboot invalidates every previous radio hold. Atomically removing the request path is the
     * resume boundary; the old ACK and bounded tombstone are then removed before polling starts.
     */
    fun clearLeaseAfterBoot(deviceRoot: File): Boolean {
        val control = controlRoot(deviceRoot)
        if (!Files.exists(control.toPath(), LinkOption.NOFOLLOW_LINKS)) return false
        checkSafeControlDirectory(control)
        val request = requestFile(deviceRoot)
        val marker = File(control, BOOT_EXPIRED_REQUEST_NAME)
        Files.deleteIfExists(marker.toPath())
        val removedRequest = if (Files.exists(request.toPath(), LinkOption.NOFOLLOW_LINKS)) {
            Files.move(request.toPath(), marker.toPath(), StandardCopyOption.ATOMIC_MOVE)
            true
        } else {
            false
        }
        val removedAck = clearAcknowledgement(deviceRoot)
        Files.deleteIfExists(marker.toPath())
        cleanupExpiredMarkers(control)
        return removedRequest || removedAck
    }

    /** Atomically hide only the parsed request generation, then remove its correlated ACK. */
    private fun clearExpiredLease(deviceRoot: File, expectedRequest: IngestionRequest): Boolean =
        runCatching {
            check(safeRequestId.matches(expectedRequest.requestId))
            val control = controlRoot(deviceRoot)
            checkSafeControlDirectory(control)
            val request = requestFile(deviceRoot)
            val marker = File(control, EXPIRED_REQUEST_NAME)
            Files.deleteIfExists(marker.toPath())
            Files.move(request.toPath(), marker.toPath(), StandardCopyOption.ATOMIC_MOVE)
            val moved = parseRequestFile(marker, control)
            if (moved != expectedRequest) {
                if (!Files.exists(request.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                    Files.move(marker.toPath(), request.toPath(), StandardCopyOption.ATOMIC_MOVE)
                }
                return@runCatching false
            }
            clearAcknowledgementIfCorrelated(deviceRoot, expectedRequest.requestId)
            Files.deleteIfExists(marker.toPath())
            true
        }.getOrDefault(false)

    private fun clearStaleInvalidLease(
        deviceRoot: File,
        request: File,
        nowUtc: Instant,
    ): Boolean = runCatching {
        val control = controlRoot(deviceRoot)
        requireSafeControlFile(request, control)
        val modified = Files.getLastModifiedTime(request.toPath(), LinkOption.NOFOLLOW_LINKS).toInstant()
        val staleAfter = INGESTION_MAXIMUM_LEASE_SECONDS + INGESTION_CLOCK_SKEW_SECONDS
        val stale = nowUtc.isAfter(modified.plusSeconds(staleAfter)) ||
            modified.isAfter(nowUtc.plusSeconds(INGESTION_CLOCK_SKEW_SECONDS))
        if (!stale) return@runCatching false
        val expectedBytes = Files.readAllBytes(request.toPath())
        val marker = File(control, EXPIRED_REQUEST_NAME)
        Files.deleteIfExists(marker.toPath())
        Files.move(request.toPath(), marker.toPath(), StandardCopyOption.ATOMIC_MOVE)
        if (!Files.readAllBytes(marker.toPath()).contentEquals(expectedBytes)) {
            if (!Files.exists(request.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                Files.move(marker.toPath(), request.toPath(), StandardCopyOption.ATOMIC_MOVE)
            }
            return@runCatching false
        }
        clearAcknowledgement(deviceRoot)
        Files.deleteIfExists(marker.toPath())
        true
    }.getOrDefault(false)

    private fun clearAcknowledgementIfCorrelated(deviceRoot: File, requestId: String) {
        val control = controlRoot(deviceRoot)
        val acknowledgement = acknowledgementFile(deviceRoot)
        val partial = File(control, "$ACK_NAME.partial")
        if (!Files.exists(acknowledgement.toPath(), LinkOption.NOFOLLOW_LINKS)) {
            Files.deleteIfExists(partial.toPath())
            return
        }
        val correlated = runCatching {
            requireSafeControlFile(acknowledgement, control)
            val body = JSONObject(readBoundedUtf8(acknowledgement))
            body.get("request_id") is String && body.getString("request_id") == requestId
        }.getOrDefault(false)
        if (correlated) Files.deleteIfExists(acknowledgement.toPath())
        Files.deleteIfExists(partial.toPath())
    }

    private fun validateAcknowledgement(
        deviceRoot: File,
        requestId: String,
        expectedState: String,
    ): Boolean = runCatching {
        check(safeRequestId.matches(requestId))
        val control = controlRoot(deviceRoot)
        val acknowledgement = acknowledgementFile(deviceRoot)
        requireSafeControlFile(acknowledgement, control)
        val text = readBoundedUtf8(acknowledgement)
        check(jsonKeyPattern.findAll(text).count() == ackKeys.size)
        val body = JSONObject(text)
        check(body.keys().asSequence().toSet() == ackKeys)
        check(body.get("schema_version").let { it is Number && it.toString() == "1" })
        check(body.get("request_id") is String && body.getString("request_id") == requestId)
        check(body.get("state") is String && body.getString("state") == expectedState)
        check(body.get("ready_at_utc") is String)
        Instant.parse(body.getString("ready_at_utc"))
        for (key in listOf("drive_id", "last_sample_at_utc", "bundle_filename", "bundle_sha256", "error")) {
            check(body.isNull(key) || body.get(key) is String)
        }
        if (!body.isNull("drive_id")) check(isSafeDriveId(body.getString("drive_id")))
        if (!body.isNull("last_sample_at_utc")) Instant.parse(body.getString("last_sample_at_utc"))
        if (!body.isNull("bundle_filename")) {
            check(!body.isNull("drive_id"))
            check(body.getString("bundle_filename") == "${body.getString("drive_id")}.obd2.zip")
        }
        if (!body.isNull("bundle_sha256")) {
            check(Regex("^[0-9a-f]{64}$").matches(body.getString("bundle_sha256")))
        }
        check(body.isNull("bundle_filename") == body.isNull("bundle_sha256"))
        if (expectedState == "ready") check(body.isNull("error"))
        else check(!body.isNull("error") && body.getString("error").length in 1..240)
        true
    }.getOrDefault(false)

    /** Request removal is the only resume signal; stale acknowledgements never keep polling off. */
    fun clearAcknowledgement(deviceRoot: File): Boolean {
        val control = controlRoot(deviceRoot)
        val target = acknowledgementFile(deviceRoot)
        val partial = File(control, "$ACK_NAME.partial")
        val removed = Files.deleteIfExists(target.toPath())
        Files.deleteIfExists(partial.toPath())
        return removed
    }

    private fun cleanupExpiredMarkers(control: File) {
        if (!Files.exists(control.toPath(), LinkOption.NOFOLLOW_LINKS)) return
        if (!runCatching { checkSafeControlDirectory(control) }.isSuccess) return
        Files.deleteIfExists(File(control, EXPIRED_REQUEST_NAME).toPath())
        Files.deleteIfExists(File(control, BOOT_EXPIRED_REQUEST_NAME).toPath())
    }

    private fun checkSafeControlDirectory(control: File) {
        val controlPath = control.toPath()
        check(!Files.isSymbolicLink(controlPath))
        check(Files.isDirectory(controlPath, LinkOption.NOFOLLOW_LINKS))
    }

    private fun requireSafeControlFile(file: File, control: File) {
        checkSafeControlDirectory(control)
        val path = file.toPath()
        check(!Files.isSymbolicLink(path))
        check(Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
        check(file.canonicalFile.parentFile == control.canonicalFile)
        check(Files.size(path) in 1..MAXIMUM_CONTROL_BYTES)
    }

    private fun readBoundedUtf8(file: File): String {
        val size = Files.size(file.toPath())
        check(size in 1..MAXIMUM_CONTROL_BYTES)
        val bytes = Files.readAllBytes(file.toPath())
        check(bytes.size.toLong() == size)
        return Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes))
            .toString()
    }
}
