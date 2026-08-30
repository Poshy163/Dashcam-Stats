package com.dashcamstats.obdlogger

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class MainActivity : Activity() {
    private lateinit var address: EditText
    private lateinit var vehicleId: EditText
    private lateinit var loggerId: EditText
    private lateinit var voltageOn: EditText
    private lateinit var voltageOff: EditText
    private lateinit var offGraceSeconds: EditText
    private lateinit var parkedIntervalSeconds: EditText
    private lateinit var voltageOnlyMode: CheckBox
    private lateinit var ownership: CheckBox
    private lateinit var enabled: CheckBox
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val current = LoggerPreferences.load(this)
        val padding = (16 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        layout.addView(TextView(this).apply {
            text = "Dashcam OBD logger"
            textSize = 24f
        })
        layout.addView(TextView(this).apply {
            text = "Before enabling: turn off switch.nissan_tiida_obd2_connection and stop all phone OBD apps. The adapter permits one owner."
        })
        address = field(layout, "BLE adapter address", current.adapterAddress).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
        }
        vehicleId = field(layout, "Vehicle ID", current.vehicleId)
        loggerId = field(layout, "Logger ID", current.loggerId)
        voltageOn = decimalField(layout, "Engine-on voltage (10.0–16.0 V)", current.voltageOn)
        voltageOff = decimalField(layout, "Engine-off voltage (10.0–16.0 V)", current.voltageOff)
        offGraceSeconds = field(
            layout,
            "Engine-off grace (0–300 seconds)",
            current.offGraceSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        parkedIntervalSeconds = field(
            layout,
            "Parked voltage interval (15–3600 seconds)",
            current.parkedIntervalSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        voltageOnlyMode = CheckBox(this).apply {
            text = "Controlled voltage-only mode (ATRV only; never initialise the ECU)"
            isChecked = current.voltageOnlyMode
        }
        layout.addView(voltageOnlyMode)
        ownership = CheckBox(this).apply {
            text = "I explicitly transferred adapter ownership from Home Assistant and phones"
            isChecked = current.ownershipTransferred
        }
        enabled = CheckBox(this).apply {
            text = "Start logger now and after boot"
            isChecked = current.enabled
        }
        layout.addView(ownership)
        layout.addView(enabled)
        status = TextView(this)
        layout.addView(Button(this).apply {
            text = "Save and apply"
            setOnClickListener { saveAndApply() }
        })
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
    }

    private fun field(parent: LinearLayout, hint: String, value: String): EditText {
        return EditText(this).also {
            it.hint = hint
            it.setText(value)
            parent.addView(
                it,
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
        }
    }

    private fun decimalField(parent: LinearLayout, hint: String, value: Double): EditText =
        field(parent, hint, value.toString()).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        }

    private fun saveAndApply() {
        val config = LoggerConfig(
            enabled = enabled.isChecked,
            ownershipTransferred = ownership.isChecked,
            adapterAddress = address.text.toString().trim(),
            vehicleId = vehicleId.text.toString().trim(),
            loggerId = loggerId.text.toString().trim(),
            voltageOn = voltageOn.text.toString().toDoubleOrNull() ?: Double.NaN,
            voltageOff = voltageOff.text.toString().toDoubleOrNull() ?: Double.NaN,
            offGraceSeconds = offGraceSeconds.text.toString().toLongOrNull() ?: -1,
            parkedIntervalSeconds = parkedIntervalSeconds.text.toString().toLongOrNull() ?: -1,
            voltageOnlyMode = voltageOnlyMode.isChecked,
        )
        if (!config.thresholdConfigurationValid) {
            rejectConfiguration(
                "Use 10.0–16.0 V, engine-off below engine-on, 0–300 seconds grace, " +
                    "and a 15–3600 second parked interval",
            )
            return
        }
        if (config.enabled && config.ownershipTransferred && !config.canRun) {
            rejectConfiguration("A valid adapter address, vehicle ID and logger ID are required")
            return
        }
        LoggerPreferences.save(this, config)
        if (config.enabled && config.ownershipTransferred) {
            requestNeededPermissions()
        } else {
            stopService(Intent(this, ObdLoggerService::class.java))
            runCatching {
                StatusPublisher.publish(this, PublicStatus("disabled", config.ownershipTransferred))
            }
            status.text = "Logger disabled"
        }
    }

    private fun rejectConfiguration(message: String) {
        LoggerPreferences.disable(this)
        enabled.isChecked = false
        stopService(Intent(this, ObdLoggerService::class.java))
        runCatching {
            StatusPublisher.error(this, ownership.isChecked, "invalid_configuration", message)
        }
        status.text = "$message; logger disabled"
    }

    private fun requestNeededPermissions() {
        val wanted = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            wanted += Manifest.permission.BLUETOOTH_CONNECT
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (wanted.any { checkSelfPermission(it) != android.content.pm.PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(wanted.toTypedArray(), 100)
        } else {
            startLogger()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 100 && hasLoggerPermissions(this)) {
            startLogger()
        } else if (requestCode == 100) {
            LoggerPreferences.disable(this)
            enabled.isChecked = false
            runCatching {
                StatusPublisher.error(
                    this,
                    ownership.isChecked,
                    "permission_required",
                    "Nearby Devices permission was denied; logger remains disabled",
                )
            }
            status.text = "Nearby Devices and notification permissions are required; logger disabled"
        }
    }

    private fun startLogger() {
        val config = LoggerPreferences.load(this)
        if (!config.canRun) {
            status.text = "Ownership, valid adapter address, vehicle ID and logger ID are required"
            return
        }
        val service = Intent(this, ObdLoggerService::class.java).setAction(
            ObdLoggerService.ACTION_RELOAD_CONFIGURATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(service)
        else startService(service)
        status.text = "Logger starting"
    }
}
