package com.dashcamstats.obdlogger

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock

internal const val EXTRA_MOVEMENT_FROM_VISIBLE_ACTIVITY = "movement_from_visible_activity"
internal object MovementActivityVisibility { @Volatile var visible = false }

/** This listener never requests a GPS fix and never retains a Location or its coordinates. */
internal class PassiveMotionObserver(private val context: Context, private val journal: MotionJournal) {
    private val manager = context.getSystemService(LocationManager::class.java)
    @Volatile var state = "disabled"
        private set
    private var subscribed = false
    private var lastFixReceivedElapsedMs: Long? = null
    private val listener = object : LocationListener {
        override fun onLocationChanged(location: Location) {
            val receivedAt = SystemClock.elapsedRealtime()
            if (lastFixReceivedElapsedMs?.let { receivedAt - it < 5_000 } == true) return
            lastFixReceivedElapsedMs = receivedAt
            @Suppress("DEPRECATION")
            val mock = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) location.isMock else location.isFromMockProvider
            if (mock) { journal.gps(null, null, null, null); return }
            // The provider timestamp is monotonic. A cached/wrong-boot/future fix is unknown.
            val age = (SystemClock.elapsedRealtimeNanos() - location.elapsedRealtimeNanos) / 1_000_000
            val speed = if (location.hasSpeed()) location.speed.toDouble() else null
            val uncertainty = if (location.hasSpeedAccuracy()) location.speedAccuracyMetersPerSecond.toDouble() else null
            journal.gps(speed, uncertainty, age,
                if (location.hasAccuracy()) location.accuracy.toDouble() else null)
        }
        @Deprecated("Legacy callback")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
        override fun onProviderEnabled(provider: String) = Unit
        override fun onProviderDisabled(provider: String) { journal.gps(null, null, null, null) }
    }

    fun enabled(): Boolean = context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE)
        .getBoolean(MOVEMENT_GPS_ENABLED, false)

    fun permitted(visibleActivity: Boolean): Boolean {
        val fine = context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val background = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
            context.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED
        return enabled() && fine && ((visibleActivity && MovementActivityVisibility.visible) || background)
    }

    @SuppressLint("MissingPermission") // checked immediately before use; SecurityException remains isolated
    fun start(visibleActivity: Boolean, promote: () -> Unit) {
        stop()
        if (!enabled()) { state = "disabled"; return }
        if (!permitted(visibleActivity)) { state = "permission_required"; return }
        try {
            promote()
            manager.requestLocationUpdates(LocationManager.PASSIVE_PROVIDER, 5_000L, 0f,
                listener, Looper.getMainLooper())
            subscribed = true
            state = "passive"
        } catch (_: RuntimeException) {
            state = "unavailable"
            journal.gps(null, null, null, null)
        }
    }

    fun stop() {
        if (subscribed) runCatching { manager.removeUpdates(listener) }
        subscribed = false
    }
}
