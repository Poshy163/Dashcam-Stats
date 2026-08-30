package com.dashcamstats.obdlogger

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) {
            return
        }
        val config = LoggerPreferences.load(context)
        if (!config.canRun) {
            runCatching {
                StatusPublisher.error(
                    context,
                    config.ownershipTransferred,
                    "disabled",
                    "logger is disabled, ownership is not transferred, or adapter address is invalid",
                )
            }
            return
        }
        if (!hasLoggerPermissions(context)) {
            LoggerPreferences.disable(context)
            runCatching {
                StatusPublisher.error(
                    context,
                    config.ownershipTransferred,
                    "permission_required",
                    "Nearby Devices or notification permission is missing; open the logger to re-enable it",
                )
            }
            return
        }
        val service = Intent(context, ObdLoggerService::class.java).apply {
            putExtra(
                EXTRA_STARTUP_RECOVERY_REASON,
                if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
                    "device_restart"
                } else {
                    "process_terminated"
                },
            )
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service)
        } else {
            context.startService(service)
        }
    }
}
