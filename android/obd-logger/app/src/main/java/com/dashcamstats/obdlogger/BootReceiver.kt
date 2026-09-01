package com.dashcamstats.obdlogger

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.util.UUID

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) {
            return
        }
        val pendingResult = goAsync()
        val eventScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        eventScope.launch {
            try {
                AppEventJournal(context.applicationContext).append(
                    sessionId = UUID.randomUUID().toString(),
                    kind = "app.boot",
                    level = "info",
                    outcome = "started",
                    reasonCode = if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
                        "boot_completed"
                    } else {
                        "package_replaced"
                    },
                )
            } finally {
                pendingResult.finish()
                eventScope.cancel()
            }
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
