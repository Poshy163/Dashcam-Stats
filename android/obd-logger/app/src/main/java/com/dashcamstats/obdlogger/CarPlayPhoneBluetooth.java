package com.dashcamstats.obdlogger;

import android.annotation.SuppressLint;
import android.app.Instrumentation;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.provider.Settings;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Shell entry point for an already diagnosed failed handoff on the Q30's Android 14 build.
 * Checks or retries one previously identified smartphone. Only the outcome is printed.
 */
@SuppressLint({"MissingPermission", "NewApi"})
public final class CarPlayPhoneBluetooth implements BluetoothProfile.ServiceListener {
    // System BluetoothProfile constants omitted from the public SDK stubs.
    private static final int A2DP_SINK = 11;
    private static final int HEADSET_CLIENT = 16;
    static final int UNKNOWN_PEER_STATE = -1;
    private static final Path LOCAL_TMP = Paths.get("/data/local/tmp");
    private final CountDownLatch profilesReady = new CountDownLatch(2);
    private volatile BluetoothProfile headset;
    private volatile BluetoothProfile sink;

    @Override public void onServiceConnected(int id, BluetoothProfile proxy) {
        if (id == HEADSET_CLIENT) headset = proxy;
        else if (id == A2DP_SINK) sink = proxy;
        else return;
        profilesReady.countDown();
    }

    @Override public void onServiceDisconnected(int id) {
        if (id == HEADSET_CLIENT) headset = null;
        if (id == A2DP_SINK) sink = null;
    }

    private static boolean isBondedPhone(BluetoothDevice phone) {
        int type = phone.getType();
        BluetoothClass deviceClass = phone.getBluetoothClass();
        return phone.getBondState() == BluetoothDevice.BOND_BONDED
                && (type == BluetoothDevice.DEVICE_TYPE_CLASSIC || type == BluetoothDevice.DEVICE_TYPE_DUAL)
                && deviceClass != null && deviceClass.getDeviceClass() == BluetoothClass.Device.PHONE_SMART;
    }

    private static boolean isOnlyBondedPhone(BluetoothAdapter adapter, BluetoothDevice phone) {
        Set<BluetoothDevice> bonded = adapter.getBondedDevices();
        if (bonded == null || !bonded.contains(phone) || !isBondedPhone(phone)) return false;
        for (BluetoothDevice other : bonded) {
            if (other.equals(phone) || other.getType() == BluetoothDevice.DEVICE_TYPE_LE) continue;
            BluetoothClass kind = other.getBluetoothClass();
            if (kind == null || kind.getMajorDeviceClass() == BluetoothClass.Device.Major.PHONE) return false;
        }
        return true;
    }

    private static boolean classicConnected(Method handle, BluetoothDevice phone) throws Exception {
        int value = (int) handle.invoke(phone, BluetoothDevice.TRANSPORT_BREDR);
        return validHandle(value);
    }

    private static boolean validHandle(int value) {
        // HCI connection handles are 12-bit, with 0xF00 through 0xFFF reserved.
        return value >= 0 && value <= 0xEFF;
    }

    private static boolean settled(int state) {
        return state == BluetoothProfile.STATE_CONNECTED || state == BluetoothProfile.STATE_DISCONNECTED;
    }

    private static boolean onlyEligiblePeer(BluetoothProfile profile, BluetoothDevice phone, int state) {
        List<BluetoothDevice> devices = profile.getConnectedDevices();
        if (devices == null || !settled(state)) return false;
        if (state == BluetoothProfile.STATE_DISCONNECTED) return devices.isEmpty();
        return devices.size() == 1 && phone.equals(devices.get(0));
    }

    @SuppressWarnings("deprecation") // The stock Android 14 HFP client can use SCO.
    private boolean audioIdle(Context context, BluetoothDevice phone, boolean hasConnection, boolean waiting)
            throws Exception {
        BluetoothProfile hfp = headset;
        BluetoothProfile a2dp = sink;
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (hfp == null || a2dp == null || audio == null || audio.getMode() != AudioManager.MODE_NORMAL
                || audio.isMusicActive() || audio.isBluetoothScoOn()) return false;
        int hfpState = hfp.getConnectionState(phone);
        int a2dpState = a2dp.getConnectionState(phone);
        if (hasConnection != (hfpState == BluetoothProfile.STATE_CONNECTED
                || a2dpState == BluetoothProfile.STATE_CONNECTED)) return false;
        if (!onlyEligiblePeer(hfp, phone, hfpState) || !onlyEligiblePeer(a2dp, phone, a2dpState)) return false;
        if (waiting) {
            int[] transitions = { BluetoothProfile.STATE_CONNECTING, BluetoothProfile.STATE_DISCONNECTING };
            List<BluetoothDevice> hfpChanging = hfp.getDevicesMatchingConnectionStates(transitions);
            List<BluetoothDevice> sinkChanging = a2dp.getDevicesMatchingConnectionStates(transitions);
            if (hfpChanging == null || !hfpChanging.isEmpty() || sinkChanging == null || !sinkChanging.isEmpty()) {
                return false;
            }
        }
        if (hfpState == BluetoothProfile.STATE_CONNECTED) {
            Class<?> api = hfp.getClass();
            Object calls = api.getMethod("getCurrentCalls", BluetoothDevice.class).invoke(hfp, phone);
            if (!(calls instanceof List) || !((List<?>) calls).isEmpty()
                    || (int) api.getMethod("getAudioState", BluetoothDevice.class).invoke(hfp, phone) != 0) return false;
        }
        return !(boolean) a2dp.getClass().getMethod("isAudioPlaying", BluetoothDevice.class).invoke(a2dp, phone)
                && headset == hfp && sink == a2dp;
    }

    private static String veto(Context context) {
        Path guard = LOCAL_TMP.resolve("dashcam_wifi_startup");
        if (!Files.isRegularFile(guard.resolve("enabled"), LinkOption.NOFOLLOW_LINKS)
                || !Files.isRegularFile(guard.resolve("zlink_reconnect_enabled"), LinkOption.NOFOLLOW_LINKS)) {
            return "disabled";
        }
        for (String suffix : new String[]{"", ".watchdog_lease", ".watchdog_active", ".watchdog_ready"}) {
            Path marker = LOCAL_TMP.resolve(".dashcam_analyser_radios" + suffix);
            if (!Files.notExists(marker, LinkOption.NOFOLLOW_LINKS)) return "backup_or_unknown";
        }
        if (!"1".equals(Settings.Global.getString(context.getContentResolver(), "acc_status"))) return "ignition_off";
        String projection = Settings.System.getString(context.getContentResolver(), "persist.sys.carplay.connect.status");
        return "0".equals(projection) ? null : "projection_or_unknown";
    }

    private static String restore(BluetoothAdapter adapter, BluetoothDevice phone, Method connect) {
        try {
            if (!adapter.isEnabled() || !isBondedPhone(phone)) return "peer_changed_after_disconnect";
            int result = (int) connect.invoke(phone);
            return result == BluetoothStatusCodes.SUCCESS ? "reconnect_requested" : "reconnect_declined";
        } catch (ReflectiveOperationException | RuntimeException error) {
            return "reconnect_uncertain";
        }
    }

    private static String cycle(BluetoothAdapter adapter, BluetoothDevice phone, Method handle,
            Method connect, Method disconnect) {
        boolean accepted = false;
        String restored;
        try {
            accepted = (int) disconnect.invoke(phone) == BluetoothStatusCodes.SUCCESS;
            if (accepted) {
                long deadline = SystemClock.elapsedRealtime() + 3_000;
                while (SystemClock.elapsedRealtime() < deadline && classicConnected(handle, phone)) {
                    SystemClock.sleep(150);
                }
            }
        } catch (Exception uncertain) {
            // A lost binder reply can follow a successful operation. Restore in either case.
        } finally {
            restored = restore(adapter, phone, connect);
        }
        if (!accepted && "reconnect_requested".equals(restored)) return "disconnect_uncertain_restore_requested";
        return restored;
    }

    // A missing BR/EDR handle alone can also mean a binder failure. Require independent
    // link and profile reads, all on the unique eligible phone, before a connect-only request.
    int waitingPeer(Context context, BluetoothAdapter adapter, BluetoothDevice phone,
            Method handle, Method isConnected) throws Exception {
        if (!adapter.isEnabled() || !isOnlyBondedPhone(adapter, phone)) return UNKNOWN_PEER_STATE;
        int value = (int) handle.invoke(phone, BluetoothDevice.TRANSPORT_BREDR);
        boolean connected = validHandle(value);
        if ((!connected && value != BluetoothDevice.ERROR) || (boolean) isConnected.invoke(phone) != connected
                || !audioIdle(context, phone, connected, true)) return UNKNOWN_PEER_STATE;
        return value;
    }

    // Reuse the signed watcher's read-only predicate after profile proxy setup. A successful
    // callback must still refer to this wake, ZLink process, historical peer and waiting page.
    private static boolean stillWaiting(String edge, String phone, String pid, String token) throws Exception {
        String command = "unzip -p \"$CLASSPATH\" assets/zlink_reconnect.sh | /system/bin/sh -s -- \"$@\"";
        java.lang.Process probe = new ProcessBuilder("/system/bin/sh", "-c", command, "verify",
                edge, "waiting-verify", phone, pid, token).redirectError(new File("/dev/null")).start();
        try (BufferedReader reply = new BufferedReader(new InputStreamReader(probe.getInputStream(),
                java.nio.charset.StandardCharsets.US_ASCII))) {
            return probe.waitFor(15, TimeUnit.SECONDS) && probe.exitValue() == 0
                    && "waiting_ready".equals(reply.readLine()) && reply.readLine() == null;
        } finally {
            probe.destroyForcibly();
        }
    }

    static String actIfStillWaiting(Callable<Integer> peer, Callable<Boolean> waiting,
            Callable<Boolean> claim, Callable<String> connect, Callable<String> cycle) throws Exception {
        int first = peer.call();
        if (first != BluetoothDevice.ERROR && !validHandle(first)) return "peer_unavailable";
        if (!waiting.call()) return "no_longer_waiting";
        // An intervening disconnect or reconnect changes the handle. Leave that attempt alone.
        if (peer.call() != first) return "peer_changed";
        if (!claim.call()) return "wake_or_gate_changed";
        if (first == BluetoothDevice.ERROR) {
            String result = connect.call();
            return "reconnect_requested".equals(result) ? "waiting_connect_requested" : result;
        }
        String result = cycle.call();
        return "reconnect_requested".equals(result) ? "waiting_cycle_requested" : result;
    }

    private String run(Context context, String[] args) throws Exception {
        boolean waiting = "waiting".equals(args[0]);
        boolean retry = "retry".equals(args[0]);
        String address = args[1].toUpperCase(Locale.ROOT);
        String token = args.length > 2 ? args[args.length - 1] : null;
        if (token != null && !ReconnectWake.isCurrent(token)) return "wake_or_gate_changed";
        if (!"com.android.shell".equals(context.getAttributionSource().getPackageName())
                || context.getAttributionSource().getUid() != Process.SHELL_UID) return "wrong_identity";
        for (String permission : new String[]{"BLUETOOTH_CONNECT", "BLUETOOTH_PRIVILEGED", "MODIFY_PHONE_STATE"}) {
            if (context.checkSelfPermission("android.permission." + permission) != PackageManager.PERMISSION_GRANTED)
                return "missing_permission";
        }
        BluetoothManager manager = context.getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null) return "bluetooth_service_unavailable";
        if (!adapter.isEnabled()) return "bluetooth_off";
        BluetoothDevice phone = adapter.getRemoteDevice(address);
        // These are public Android System APIs. Use normal public method lookup; an unavailable
        // API makes this feature unavailable. Resolve both operations before any disconnect.
        Method handle = BluetoothDevice.class.getMethod("getConnectionHandle", int.class);
        Method connect = BluetoothDevice.class.getMethod("connect");
        Method disconnect = BluetoothDevice.class.getMethod("disconnect");
        Class<?> hfpApi = Class.forName("android.bluetooth.BluetoothHeadsetClient");
        hfpApi.getMethod("getCurrentCalls", BluetoothDevice.class);
        hfpApi.getMethod("getAudioState", BluetoothDevice.class);
        Class.forName("android.bluetooth.BluetoothA2dpSink").getMethod("isAudioPlaying", BluetoothDevice.class);
        if (!isBondedPhone(phone)) return "ineligible_peer";
        if (!isOnlyBondedPhone(adapter, phone)) return "ambiguous_peer";
        try {
            if (!adapter.getProfileProxy(context, this, HEADSET_CLIENT)
                    || !adapter.getProfileProxy(context, this, A2DP_SINK)
                    || !profilesReady.await(3, TimeUnit.SECONDS)) return "profiles_unavailable";
            if (waiting) {
                Method isConnected = BluetoothDevice.class.getMethod("isConnected");
                return actIfStillWaiting(() -> waitingPeer(context, adapter, phone, handle, isConnected),
                        () -> stillWaiting(args[2], address, args[3], token),
                        () -> veto(context) == null && ReconnectWake.claim(token),
                        () -> restore(adapter, phone, connect),
                        () -> cycle(adapter, phone, handle, connect, disconnect));
            }
            if (!classicConnected(handle, phone)) {
                if (!retry) {
                    // Disconnected profiles can return null calls / idle audio. Only exercise
                    // these reads to surface an unsupported API or rejected caller identity.
                    BluetoothProfile hfp = headset;
                    BluetoothProfile a2dp = sink;
                    if (hfp == null || a2dp == null) return "profiles_unavailable";
                    hfpApi.getMethod("getCurrentCalls", BluetoothDevice.class).invoke(hfp, phone);
                    hfpApi.getMethod("getAudioState", BluetoothDevice.class).invoke(hfp, phone);
                    a2dp.getClass().getMethod("isAudioPlaying", BluetoothDevice.class).invoke(a2dp, phone);
                    if (headset != hfp || sink != a2dp) return "profiles_unavailable";
                }
                return "no_classic_link";
            }
            if (!audioIdle(context, phone, true, false)) return "audio_or_profile_busy";
            if (!isOnlyBondedPhone(adapter, phone) || !classicConnected(handle, phone)) return "peer_changed";
            if (!retry) return "ready";
            String reason = veto(context);
            if (reason != null) return reason;
            if (token != null && !ReconnectWake.claim(token)) return "wake_or_gate_changed";
            return cycle(adapter, phone, handle, connect, disconnect);
        } finally {
            if (headset != null) adapter.closeProfileProxy(HEADSET_CLIENT, headset);
            if (sink != null) adapter.closeProfileProxy(A2DP_SINK, sink);
        }
    }

    private static Context shellContext() throws Exception {
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        // app_process skips ActivityThread.main(), which normally registers the per-process
        // framework module managers before any Context requests a Bluetooth service.
        activityThread.getMethod("initializeMainlineModules").invoke(null);
        Object thread = activityThread.getMethod("systemMain").invoke(null);
        Context system = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
        ApplicationInfo shell = system.getPackageManager().getApplicationInfo("com.android.shell", 0);
        if (shell.uid != Process.myUid()) throw new SecurityException();
        // createPackageContext inherits the system context's AppOps attribution. Build the
        // shell package's own base application context instead; load only its resources and
        // ask Android for the default Application, never executing shell application code.
        Class<?> compat = Class.forName("android.content.res.CompatibilityInfo");
        Object loaded = activityThread.getMethod("getPackageInfo", ApplicationInfo.class, compat, int.class)
                .invoke(thread, shell, compat.getField("DEFAULT_COMPATIBILITY_INFO").get(null), 0);
        return (Context) loaded.getClass().getMethod("makeApplication", boolean.class, Instrumentation.class)
                .invoke(loaded, true, null);
    }

    private static boolean validArgs(String[] args) {
        if (args.length < 2 || !BluetoothAdapter.checkBluetoothAddress(args[1].toUpperCase(Locale.ROOT))) return false;
        switch (args[0]) {
            case "check": return args.length == 2;
            case "retry": return args.length == 2 || (args.length == 3 && ReconnectWake.validToken(args[2]));
            case "waiting":
                return args.length == 5 && args[2].matches("[1-9][0-9]{0,10}")
                        && args[3].matches("[1-9][0-9]{0,9}") && ReconnectWake.validToken(args[4]);
            default: return false;
        }
    }

    @SuppressWarnings("deprecation") // app_process does not supply an application main looper.
    public static void main(String[] args) {
        if (Process.myUid() != Process.SHELL_UID) finish("wrong_identity");
        if (Build.VERSION.SDK_INT != 34) finish("unsupported");
        if (!validArgs(args)) finish("invalid_args");
        try {
            Looper.prepareMainLooper();
            Context shell = shellContext();
            new Thread(() -> {
                try {
                    finish(new CarPlayPhoneBluetooth().run(shell, args));
                } catch (InvocationTargetException error) {
                    finish(error.getCause() instanceof SecurityException ? "permission_or_attribution_denied" : "api_error");
                } catch (SecurityException denied) {
                    finish("permission_or_attribution_denied");
                } catch (ReflectiveOperationException | LinkageError unavailable) {
                    finish("api_unavailable");
                } catch (Exception error) {
                    finish("check_failed");
                }
            }, "CarPlayPhoneBluetooth").start();
            Looper.loop();
        } catch (Exception | LinkageError unavailable) {
            finish("context_unavailable");
        }
    }

    private static void finish(String outcome) {
        System.out.println(outcome);
        boolean accepted = "ready".equals(outcome) || "reconnect_requested".equals(outcome)
                || "waiting_connect_requested".equals(outcome) || "waiting_cycle_requested".equals(outcome);
        System.exit(accepted ? 0 : 1);
    }
}
