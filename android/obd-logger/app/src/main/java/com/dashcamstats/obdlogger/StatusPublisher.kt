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
    val adapterReachable: Boolean = false,
    val adapterConnected: Boolean = false,
    val ecuConnected: Boolean = false,
    val engineRunning: Boolean = false,
    val vehicleState: String = state,
    val batteryVoltage: Double? = null,
    val batteryVoltageSource: String? = null,
    val batteryVoltageSampleAtUtc: String? = null,
    val batteryVoltageFresh: Boolean = false,
    val batteryVoltageRawResponse: String? = null,
    val batteryVoltageQuality: String = "unavailable",
    val bleOwner: String = if (ownershipEnabled) "dashcam_full_obd" else "unowned",
    val headUnitState: String = "awake",
    val voltageOnlyMode: Boolean = false,
    val lastError: String? = null,
    val lastErrorAtUtc: String? = null,
)

internal const val PUBLIC_STATUS_SCHEMA_VERSION = 4

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
            .put("schema_version", PUBLIC_STATUS_SCHEMA_VERSION)
            .put("app_version_name", BuildConfig.VERSION_NAME)
            .put("app_version_code", BuildConfig.VERSION_CODE)
            .put("poll_plan_version", ObdPollPlan.VERSION)
            .put("build_git_sha", BuildConfig.BUILD_GIT_SHA)
            .put(
                "capabilities",
                org.json.JSONArray(
                    listOf(
                        "ingestion_quiesce_v1",
                        "voltage_only_audit_v1",
                        "controlled_voltage_only_mode_v1",
                    ),
                ),
            )
            .put("state", status.state)
            .put("ownership_enabled", status.ownershipEnabled)
            .put("current_drive_id", status.currentDriveId ?: JSONObject.NULL)
            .put("last_drive_id", status.lastDriveId ?: JSONObject.NULL)
            .put("last_drive_finished_at_utc", status.lastDriveFinishedAtUtc ?: JSONObject.NULL)
            .put("pending_bundle_count", pendingCount.coerceAtLeast(0))
            .put("ingestion_request_id", status.ingestionRequestId ?: JSONObject.NULL)
            .put("last_sample_at_utc", status.lastSampleAtUtc ?: JSONObject.NULL)
            .put("metrics", status.metrics.toJson())
            .put("adapter_reachable", status.adapterReachable)
            .put("adapter_connected", status.adapterConnected)
            .put("ecu_connected", status.ecuConnected)
            .put("engine_running", status.engineRunning)
            .put("vehicle_state", status.vehicleState)
            .put("battery_voltage", status.batteryVoltage ?: JSONObject.NULL)
            .put("battery_voltage_source", status.batteryVoltageSource ?: JSONObject.NULL)
            .put("battery_voltage_sample_at_utc", status.batteryVoltageSampleAtUtc ?: JSONObject.NULL)
            .put("battery_voltage_fresh", status.batteryVoltageFresh)
            .put("battery_voltage_raw_response", status.batteryVoltageRawResponse ?: JSONObject.NULL)
            .put("battery_voltage_quality", status.batteryVoltageQuality)
            .put("ble_owner", status.bleOwner)
            .put("head_unit_state", status.headUnitState)
            .put("voltage_only_mode", status.voltageOnlyMode)
            .put("updated_at_utc", Instant.now().toString())
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
