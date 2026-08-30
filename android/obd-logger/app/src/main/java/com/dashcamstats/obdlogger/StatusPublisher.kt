package com.dashcamstats.obdlogger

import android.content.Context
import android.os.Environment
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant

class RemovableStorageUnavailableException : IllegalStateException(
    "removable TF storage is not mounted",
)

object DeviceFiles {
    fun removableRootOrNull(context: Context): File? {
        val roots = context.getExternalFilesDirs(null).filterNotNull()
        val states = roots.map {
            StorageVolumeState(
                removable = Environment.isExternalStorageRemovable(it),
                mounted = Environment.getExternalStorageState(it) == Environment.MEDIA_MOUNTED,
            )
        }
        val index = RemovableStoragePolicy.selectIndex(states) ?: return null
        return File(roots[index], "obd")
    }

    fun root(context: Context): File = removableRootOrNull(context)
        ?: throw RemovableStorageUnavailableException()

    fun ready(context: Context): File = File(root(context), "ready")

    fun receipts(context: Context): File = File(root(context), "receipts")

    fun fallbackStatusRoot(context: Context): File = File(
        context.getExternalFilesDir(null) ?: context.filesDir,
        "obd",
    )
}

data class PublicStatus(
    val state: String,
    val ownershipEnabled: Boolean,
    val currentDriveId: String? = null,
    val lastDriveId: String? = null,
    val lastDriveFinishedAtUtc: String? = null,
    val ingestionRequestId: String? = null,
    val lastSampleAtUtc: String? = null,
    val metrics: PipelineMetricsSnapshot = PipelineMetricsSnapshot.EMPTY,
    val lastError: String? = null,
    val lastErrorAtUtc: String? = null,
)

object StatusPublisher {
    fun publish(context: Context, status: PublicStatus) {
        val root = DeviceFiles.removableRootOrNull(context)
        if (root == null) storageUnavailable(context, status)
        else publishAt(root, status, pendingCount = null)
    }

    fun storageUnavailable(context: Context, status: PublicStatus) {
        publishAt(DeviceFiles.fallbackStatusRoot(context), status, pendingCount = 0)
    }

    private fun publishAt(root: File, status: PublicStatus, pendingCount: Int?) {
        root.mkdirs()
        val ready = if (pendingCount == null) File(root, "ready").apply { mkdirs() } else null
        val body = buildStatusJson(status, pendingCount ?: ready?.listFiles()?.count {
            it.isFile && it.name.endsWith(".obd2.zip") && !it.name.endsWith(".partial")
        } ?: 0)
        val partial = File(root, "status.json.partial")
        FileOutputStream(partial).use {
            it.write(body.toString().toByteArray(Charsets.UTF_8))
            it.fd.sync()
        }
        val target = File(root, "status.json")
        try {
            Files.move(
                partial.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (error: Exception) {
            partial.delete()
            throw IllegalStateException("could not atomically publish logger status", error)
        }
    }

    internal fun buildStatusJson(status: PublicStatus, pendingCount: Int): JSONObject = JSONObject()
            .put("schema_version", 2)
            .put("capabilities", org.json.JSONArray(listOf("ingestion_quiesce_v1")))
            .put("state", status.state)
            .put("ownership_enabled", status.ownershipEnabled)
            .put("current_drive_id", status.currentDriveId ?: JSONObject.NULL)
            .put("last_drive_id", status.lastDriveId ?: JSONObject.NULL)
            .put("last_drive_finished_at_utc", status.lastDriveFinishedAtUtc ?: JSONObject.NULL)
            .put("pending_bundle_count", pendingCount.coerceAtLeast(0))
            .put("ingestion_request_id", status.ingestionRequestId ?: JSONObject.NULL)
            .put("last_sample_at_utc", status.lastSampleAtUtc ?: JSONObject.NULL)
            .put("metrics", status.metrics.toJson())
            .put("last_error", status.lastError ?: JSONObject.NULL)
            .put("last_error_at_utc", status.lastErrorAtUtc ?: JSONObject.NULL)

    fun error(context: Context, ownership: Boolean, state: String, message: String) {
        publish(
            context,
            PublicStatus(
                state = state,
                ownershipEnabled = ownership,
                lastError = message.take(240),
                lastErrorAtUtc = Instant.now().toString(),
            ),
        )
    }
}
