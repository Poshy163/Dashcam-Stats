package com.dashcamstats.obdlogger

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.net.Uri
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var address: EditText
    private lateinit var vehicleId: EditText
    private lateinit var loggerId: EditText
    private lateinit var voltageOn: EditText
    private lateinit var voltageOff: EditText
    private lateinit var offGraceSeconds: EditText
    private lateinit var parkedIntervalSeconds: EditText
    private lateinit var voltageOnlyMode: CheckBox
    private lateinit var webhookEnabled: CheckBox
    private lateinit var webhookUrl: EditText
    private lateinit var webhookApiKey: EditText
    private lateinit var backupAwakeSeconds: EditText
    private lateinit var idleAwakeSeconds: EditText
    private lateinit var ownership: CheckBox
    private lateinit var enabled: CheckBox
    private lateinit var status: TextView
    private lateinit var liveStatus: TextView
    private lateinit var metricsToggle: Button
    private lateinit var liveMetrics: TextView
    private lateinit var credentialError: TextView
    private val activityScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var refreshJob: Job? = null
    private var foreground = false
    private var metricsExpanded = false
    private var publishedStatus: JSONObject? = null
    private var credentialLoadError: String? = null
    private var saving = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // The head unit's screen is the only place this status is visible; the activity is
        // opened on purpose and should not dim while someone is reading it.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(TextView(this).apply { text = "Loading logger configuration…" })
        activityScope.launch {
            val current = withContext(Dispatchers.IO) {
                runCatching { LoggerPreferences.load(this@MainActivity) }.getOrNull()
            }
            if (current == null) {
                setContentView(TextView(this@MainActivity).apply {
                    text = "Logger configuration could not be loaded. Reopen the app to try again."
                })
                return@launch
            }
            createConfigurationForm(current)
            if (foreground) startStatusRefresh()
        }
    }

    private fun createConfigurationForm(current: LoggerConfig) {
        credentialLoadError = current.webhookCredentialError
        val padding = (16 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        layout.addView(TextView(this).apply {
            text = "Dashcam OBD logger"
            textSize = 24f
        })
        liveStatus = TextView(this).apply {
            textSize = 18f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.rgb(24, 24, 28))
            setPadding(padding, padding, padding, padding)
            setLineSpacing(0f, 1.2f)
        }
        layout.addView(
            liveStatus,
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        liveMetrics = TextView(this).apply {
            textSize = 16f
            typeface = Typeface.MONOSPACE
            setPadding(padding, padding, padding, padding)
            visibility = View.GONE
        }
        metricsToggle = Button(this).apply {
            text = "Show metrics and sleep details"
            setOnClickListener {
                metricsExpanded = !metricsExpanded
                renderLiveStatus()
            }
        }
        layout.addView(metricsToggle)
        layout.addView(liveMetrics)
        layout.addView(TextView(this).apply {
            text = "On-camera movement report (advisory only). Footage is never excluded or deleted. " +
                "Choose the recorder's DCIM folder to summarise closed clips. GPS is optional; " +
                "only speed, uncertainty and coverage are stored, never coordinates."
        })
        layout.addView(Button(this).apply {
            text = "Choose DCIM folder (read only)"
            setOnClickListener {
                runCatching {
                    startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(
                        Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), 201)
                }.onFailure { status.text = "The system folder picker is unavailable; logging and backup continue." }
            }
        })
        layout.addView(Button(this).apply {
            text = "Remove footage folder access"
            setOnClickListener { MovementTreeAccess.revoke(this@MainActivity); status.text = "Folder access removed." }
        })
        layout.addView(CheckBox(this).apply {
            text = "Use optional passive GPS speed (no active GPS requests)"
            isChecked = getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE)
                .getBoolean(MOVEMENT_GPS_ENABLED, false)
            setOnCheckedChangeListener { _, checked ->
                getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE).edit()
                    .putBoolean(MOVEMENT_GPS_ENABLED, checked).apply()
                if (checked) requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION), 101)
                else if (LoggerPreferences.load(this@MainActivity, false).canRun) startLogger()
            }
        })
        layout.addView(Button(this).apply {
            text = "Optional GPS after reboot: app location settings"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:$packageName")))
            }
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
        webhookEnabled = CheckBox(this).apply {
            text = "Send webhook to Dashcam Analyser on engine-off / sleep"
            isChecked = current.webhookEnabled
        }
        layout.addView(webhookEnabled)
        webhookUrl = field(
            layout,
            "Webhook URL (e.g. http://192.168.1.16:8199/api/ingest/webhook)",
            current.webhookUrl,
        )
        webhookApiKey = field(layout, "Webhook API key (X-API-Key)", current.webhookApiKey).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        credentialError = TextView(this).apply {
            setTextColor(Color.rgb(180, 30, 30))
        }
        layout.addView(credentialError)
        showCredentialError(credentialLoadError)
        backupAwakeSeconds = field(
            layout,
            "Backup awake time (Wi-Fi connected, seconds)",
            current.backupAwakeSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        idleAwakeSeconds = field(
            layout,
            "Idle awake time (non-Wi-Fi, seconds)",
            current.idleAwakeSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        ownership = CheckBox(this).apply {
            text = "I explicitly transferred adapter ownership from every other BLE client"
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

    override fun onResume() {
        super.onResume()
        foreground = true
        MovementActivityVisibility.visible = true
        if (::liveStatus.isInitialized) startStatusRefresh()
    }

    override fun onPause() {
        foreground = false
        MovementActivityVisibility.visible = false
        refreshJob?.cancel()
        refreshJob = null
        super.onPause()
    }

    override fun onDestroy() {
        activityScope.cancel()
        super.onDestroy()
    }

    private fun startStatusRefresh() {
        refreshJob?.cancel()
        refreshJob = activityScope.launch {
            while (isActive) {
                publishedStatus = withContext(Dispatchers.IO) {
                    val roots = listOfNotNull(
                        runCatching { DeviceFiles.removableRootOrNull(this@MainActivity) }.getOrNull(),
                        runCatching { DeviceFiles.fallbackStatusRoot(this@MainActivity) }.getOrNull(),
                    )
                    LiveStatusSummary.readPublishedStatus(roots, System.currentTimeMillis())
                }
                renderLiveStatus()
                delay(LIVE_STATUS_REFRESH_MILLIS)
            }
        }
    }

    private fun renderLiveStatus() {
        liveStatus.text = LiveStatusSummary.lines(publishedStatus, System.currentTimeMillis())
            .joinToString("\n")
        liveMetrics.visibility = if (metricsExpanded) View.VISIBLE else View.GONE
        metricsToggle.text = if (metricsExpanded) "Hide metrics and sleep details"
            else "Show metrics and sleep details"
        if (metricsExpanded) {
            liveMetrics.text = LiveStatusSummary.detailLines(publishedStatus).joinToString("\n")
        }
    }

    private fun showCredentialError(message: String?) {
        credentialError.text = when (message) {
            WEBHOOK_CREDENTIAL_UNAVAILABLE ->
                "The saved webhook API key is unavailable. Re-enter it to restore webhook delivery. Existing logging continues."
            else -> message.orEmpty()
        }
        credentialError.visibility = if (message == null) View.GONE else View.VISIBLE
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
        if (saving) return
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
            webhookEnabled = webhookEnabled.isChecked,
            webhookUrl = webhookUrl.text.toString().trim(),
            webhookApiKey = webhookApiKey.text.toString().trim(),
            webhookCredentialError = credentialLoadError.takeIf { webhookApiKey.text.isBlank() },
            backupAwakeSeconds = backupAwakeSeconds.text.toString().toIntOrNull() ?: -1,
            idleAwakeSeconds = idleAwakeSeconds.text.toString().toIntOrNull() ?: -1,
        )
        if (config.enabled && config.webhookEnabled && config.webhookCredentialError != null) {
            status.text = "Re-enter the webhook API key to save these settings. Existing logging continues."
            showCredentialError(config.webhookCredentialError)
            return
        }
        if (!config.thresholdConfigurationValid) {
            rejectConfiguration(
                "Use 10.0–16.0 V, engine-off below engine-on, 0–300 seconds grace, " +
                    "15–3600 second parked interval, 30–3600s backup awake, and 15–3600s idle awake",
            )
            return
        }
        // Stopping collection must remain possible while the webhook credential is unavailable.
        val webhookError = if (config.enabled) webhookConfigurationError(config) else null
        when (webhookError) {
            "webhook_api_key_required" -> rejectConfiguration("Enter the webhook API key")
            "webhook_api_key_too_long" -> rejectConfiguration("Webhook API key is too long")
            "invalid_webhook_url" -> rejectConfiguration(
                "Use the /api/ingest/webhook HTTPS endpoint, or HTTP with a private LAN IP address",
            )
            null -> Unit
            else -> rejectConfiguration("Webhook configuration is invalid")
        }
        if (webhookError != null) return
        if (config.enabled && config.ownershipTransferred && !config.canRun) {
            rejectConfiguration("A valid adapter address, vehicle ID and logger ID are required")
            return
        }
        saving = true
        status.text = "Saving settings…"
        activityScope.launch {
            try {
                withContext(Dispatchers.IO) { LoggerPreferences.save(this@MainActivity, config) }
            } catch (error: WebhookCredentialStorageException) {
                showCredentialError(error.message)
                status.text = "Settings were not saved. Unlock the device and re-enter the webhook API key to try again. Existing logging continues."
                return@launch
            } finally {
                saving = false
            }
            credentialLoadError = config.webhookCredentialError
            showCredentialError(credentialLoadError)
            if (config.enabled && config.ownershipTransferred) {
                requestNeededPermissions()
            } else {
                stopService(Intent(this@MainActivity, ObdLoggerService::class.java))
                withContext(Dispatchers.IO) {
                    runCatching {
                        StatusPublisher.publish(
                            this@MainActivity, PublicStatus("disabled", config.ownershipTransferred),
                        )
                    }
                }
                status.text = "Logger disabled"
            }
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
        if (requestCode == 101) {
            if (LoggerPreferences.load(this, false).canRun) startLogger()
            status.text = "Passive GPS is optional. Missing permission leaves ACC and OBD movement reporting active."
            return
        }
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
        val config = LoggerPreferences.load(this, includeWebhookCredential = false)
        if (!config.canRun) {
            status.text = "Ownership, valid adapter address, vehicle ID and logger ID are required"
            return
        }
        val service = Intent(this, ObdLoggerService::class.java).setAction(
            ObdLoggerService.ACTION_RELOAD_CONFIGURATION,
        ).putExtra(EXTRA_MOVEMENT_FROM_VISIBLE_ACTIVITY, true)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(service)
        else startService(service)
        status.text = "Logger starting"
    }

    @Deprecated("Activity result API remains supported on the head unit")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 201 || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        runCatching { MovementTreeAccess.save(this, uri, data.flags) }
            .onSuccess { status.text = "Read-only DCIM access saved. Closed clips are indexed within one minute." }
            .onFailure { status.text = "Select the recorder's DCIM folder and allow read access. Logging and backup continue." }
    }

    private companion object {
        const val LIVE_STATUS_REFRESH_MILLIS = 2_000L
    }
}
