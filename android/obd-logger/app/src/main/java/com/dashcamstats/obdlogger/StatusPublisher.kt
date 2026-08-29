package com.dashcamstats.obdlogger

import android.content.Context
import android.os.Environment
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
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
    val lastDriveId: String? = null,
    val lastDriveFinishedAtUtc: String? = null,
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
        val body = JSONObject()
            .put("state", status.state)
            .put("ownership_enabled", status.ownershipEnabled)
            .put("last_drive_id", status.lastDriveId ?: JSONObject.NULL)
            .put("last_drive_finished_at_utc", status.lastDriveFinishedAtUtc ?: JSONObject.NULL)
            .put(
                "pending_bundle_count",
                pendingCount ?: ready?.listFiles()?.count {
                    it.isFile && it.name.endsWith(".obd2.zip") && !it.name.endsWith(".partial")
                } ?: 0,
            )
            .put("last_error", status.lastError ?: JSONObject.NULL)
            .put("last_error_at_utc", status.lastErrorAtUtc ?: JSONObject.NULL)
        val partial = File(root, "status.json.partial")
        FileOutputStream(partial).use {
            it.write(body.toString().toByteArray(Charsets.UTF_8))
            it.fd.sync()
        }
        val target = File(root, "status.json")
        if (!partial.renameTo(target)) {
            partial.delete()
            throw IllegalStateException("could not atomically publish logger status")
        }
    }

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
