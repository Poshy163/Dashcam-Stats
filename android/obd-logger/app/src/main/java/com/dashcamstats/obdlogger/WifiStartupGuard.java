package com.dashcamstats.obdlogger;

import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Parcel;
import android.os.SystemClock;
import android.util.Log;

import java.io.File;
import java.io.RandomAccessFile;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.channels.FileLock;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Shell-UID entry point launched by the server from a private copy of the companion APK.
 * The ordinary app UID does not have NETWORK_SETTINGS. No radio power, AP configuration,
 * saved network, credential, or global ignition value is ever changed here.
 * No wake lock: Android suspends this process with the head unit.
 */
public final class WifiStartupGuard {
    private static final File ROOT = new File("/data/local/tmp/dashcam_wifi_startup");
    private static final File ENABLED = new File(ROOT, "enabled");
    private static final File LEASE = new File(ROOT, "lease");
    private static final File READY = new File(ROOT, "ready");
    private static final File BOOT = new File("/proc/sys/kernel/random/boot_id");
    private static final String TAG = "WifiStartupGuard";
    private final Object wifi;
    private final Class<?> api;
    private final Method allow;

    private WifiStartupGuard() throws Exception {
        Object binder = Class.forName("android.os.ServiceManager")
                .getMethod("getService", String.class).invoke(null, "wifi");
        api = Class.forName("android.net.wifi.IWifiManager");
        wifi = Class.forName("android.net.wifi.IWifiManager$Stub")
                .getMethod("asInterface", IBinder.class).invoke(null, binder);
        allow = api.getMethod("allowAutojoinGlobal", boolean.class, String.class, Bundle.class);
    }

    private void allow(boolean allowed) throws Exception {
        allow.invoke(wifi, allowed, "com.android.shell", new Bundle());
    }

    private boolean query() throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        boolean[] value = {false};
        Binder binder = new Binder() {
            @Override protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
                if (code != IBinder.FIRST_CALL_TRANSACTION) return false;
                data.enforceInterface("android.net.wifi.IBooleanListener");
                value[0] = data.readInt() != 0;
                latch.countDown();
                return true;
            }
        };
        Class<?> listener = Class.forName("android.net.wifi.IBooleanListener");
        Object callback = Proxy.newProxyInstance(listener.getClassLoader(), new Class<?>[]{listener},
                (proxy, method, args) -> method.getName().equals("asBinder") ? binder : null);
        api.getMethod("queryAutojoinGlobal", listener).invoke(wifi, callback);
        if (!latch.await(2, TimeUnit.SECONDS)) throw new IllegalStateException("query_timeout");
        return value[0];
    }

    private boolean awaitValue(boolean expected) throws Exception {
        for (int i = 0; i < 10; i++) {
            if (query() == expected) return true;
            Thread.sleep(50);
        }
        return false;
    }

    private static String read(File file) {
        try (java.io.InputStream input = Files.newInputStream(file.toPath())) {
            byte[] bytes = new byte[161];
            int count = input.read(bytes);
            if (count < 0 || count > 160) return "";
            return new String(bytes, 0, count, StandardCharsets.US_ASCII).trim();
        } catch (Exception ignored) { return ""; }
    }

    private static void write(File file, String text) throws Exception {
        File tmp = new File(file.getPath() + ".tmp");
        Files.write(tmp.toPath(), text.getBytes(StandardCharsets.US_ASCII));
        if (!tmp.renameTo(file)) throw new IllegalStateException("state_write_failed");
    }

    private static void event(String text) {
        // Fixed vocabulary only. Never log exception messages, settings contents or identities.
        Log.e(TAG, text);
        try {
            File log = new File(ROOT, "events.log");
            if (log.length() > 32_768) {
                Files.move(log.toPath(), new File(ROOT, "events.previous.log").toPath(),
                        java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
            try (java.io.FileWriter out = new java.io.FileWriter(log, true)) {
                out.write(System.currentTimeMillis() + " " + text + "\n");
            }
        } catch (Exception ignored) { /* Diagnostics must never delay recovery. */ }
    }

    private boolean restore(String lease) throws Exception {
        try (RandomAccessFile file = new RandomAccessFile(new File(ROOT, "lease.lock"), "rw");
             FileLock lock = file.getChannel().lock()) {
            if (!read(LEASE).equals(lease)) return true;
            String[] fields = lease.split(" ");
            if (fields.length != 3 || !fields[2].equals(read(BOOT))) {
                Files.deleteIfExists(LEASE.toPath());
                Files.deleteIfExists(READY.toPath());
                event("stale_lease_discarded");
                return true;
            }
            allow(true);
            if (!awaitValue(true)) return false;
            // Parent and recovery process can both restore; both write the same original value.
            Files.deleteIfExists(LEASE.toPath());
            Files.deleteIfExists(READY.toPath());
            event("autojoin_restored");
            return true;
        }
    }

    private String begin(long deadline) throws Exception {
        String boot = read(BOOT);
        if (boot.isEmpty()) throw new IllegalStateException("boot_identity_unavailable");
        String lease = UUID.randomUUID() + " " + deadline + " " + boot;
        try (RandomAccessFile file = new RandomAccessFile(new File(ROOT, "lease.lock"), "rw");
             FileLock lock = file.getChannel().lock()) {
            if (LEASE.isFile()) throw new IllegalStateException("recovery_pending");
            if (!query()) {
                event("hold_skipped_autojoin_already_disabled");
                return "";
            }
            write(LEASE, lease);
        }
        java.lang.Process recovery = new ProcessBuilder("/system/bin/app_process", "/system/bin",
                WifiStartupGuard.class.getName(), "recover", lease)
                .redirectOutput(new File("/dev/null")).redirectError(new File("/dev/null")).start();
        long readyDeadline = SystemClock.elapsedRealtime() + 3_000;
        while (!read(READY).equals(lease) && recovery.isAlive()
                && SystemClock.elapsedRealtime() < readyDeadline) Thread.sleep(25);
        if (!read(READY).equals(lease) || !recovery.isAlive()
                || SystemClock.elapsedRealtime() >= deadline) {
            restore(lease);
            throw new IllegalStateException("recovery_not_ready");
        }
        try {
            try (RandomAccessFile file = new RandomAccessFile(new File(ROOT, "lease.lock"), "rw");
                 FileLock lock = file.getChannel().lock()) {
                // Serialize the disable with recovery: a delayed binder call must never apply
                // false after the watchdog has already restored true and removed its lease.
                if (!ENABLED.isFile() || !read(LEASE).equals(lease)
                        || SystemClock.elapsedRealtime() >= deadline) {
                    throw new IllegalStateException("hold_cancelled_before_apply");
                }
                allow(false);
                if (!awaitValue(false)) throw new IllegalStateException("hold_not_verified");
                if (!Boolean.TRUE.equals(api.getMethod("disconnect", String.class)
                        .invoke(wifi, "com.android.shell"))) {
                    throw new IllegalStateException("disconnect_refused");
                }
                event("autojoin_paused_30s");
            }
            return lease;
        } catch (Exception failure) {
            restore(lease);
            throw failure;
        }
    }

    private void recover(String lease) throws Exception {
        String[] fields = lease.split(" ");
        if (fields.length != 3 || !read(LEASE).equals(lease)) return;
        long deadline = Long.parseLong(fields[1]);
        long remaining = deadline - SystemClock.elapsedRealtime();
        if (remaining <= 0 || remaining > StartupWifiPolicy.HOLD_MILLIS) {
            restore(lease);
            return;
        }
        write(READY, lease);
        while (read(LEASE).equals(lease)) {
            if (!ENABLED.isFile() || SystemClock.elapsedRealtime() >= deadline) {
                try { if (restore(lease)) return; } catch (Exception ignored) { }
            }
            Thread.sleep(100);
        }
    }

    /** Same external-provider path as Android's shell `settings get`, without spawning a process. */
    private static final class AccReader implements AutoCloseable {
        private final Object manager;
        private final Class<?> managerApi = Class.forName("android.app.IActivityManager");
        private final Binder token = new Binder();
        private final Object provider;
        private final Object attribution;
        private final Method call;

        AccReader() throws Exception {
            manager = Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
            Object holder = managerApi.getMethod("getContentProviderExternal", String.class,
                    int.class, IBinder.class, String.class).invoke(manager, "settings", 0, token, TAG);
            provider = holder.getClass().getField("provider").get(holder);
            Class<?> source = Class.forName("android.content.AttributionSource");
            Class<?> builderClass = Class.forName("android.content.AttributionSource$Builder");
            Object builder = builderClass.getConstructor(int.class).newInstance(2000);
            builderClass.getMethod("setPackageName", String.class).invoke(builder, "com.android.shell");
            attribution = builderClass.getMethod("build").invoke(builder);
            call = Class.forName("android.content.IContentProvider").getMethod("call", source,
                    String.class, String.class, String.class, Bundle.class);
        }

        Boolean read() {
            try {
                Bundle result = (Bundle) call.invoke(provider, attribution, "settings",
                        "GET_global", "acc_status", new Bundle());
                String value = result.getString("value");
                if ("0".equals(value)) return false;
                if ("1".equals(value)) return true;
            } catch (Exception ignored) { }
            return null;
        }

        @Override public void close() throws Exception {
            managerApi.getMethod("removeContentProviderExternalAsUser", String.class, IBinder.class,
                    int.class).invoke(manager, "settings", token, 0);
        }
    }

    private void watch(boolean test) throws Exception {
        // A file lock, not PID matching, prevents duplicate controllers and PID reuse races.
        try (RandomAccessFile file = new RandomAccessFile(new File(ROOT, "watch.lock"), "rw");
             FileLock lock = file.getChannel().tryLock();
             AccReader acc = new AccReader()) {
            if (lock == null) return;
            write(new File(ROOT, "watch.pid"), Integer.toString(android.os.Process.myPid()));
            String stale = read(LEASE);
            if (!stale.isEmpty() && !restore(stale)) return;
            StartupWifiPolicy policy = new StartupWifiPolicy();
            String lease = "";
            long attemptedDeadline = 0;
            long sampledDeadline = 0;
            long testDeadline = 0;
            if (test) {
                if (!Boolean.FALSE.equals(acc.read())) {
                    event("parked_test_refused_ignition_not_off");
                    return;
                }
                testDeadline = SystemClock.elapsedRealtime() + StartupWifiPolicy.HOLD_MILLIS;
            }
            event(test ? "parked_test_started" : "watching_ignition");
            try {
                while (ENABLED.isFile()) {
                    long now = SystemClock.elapsedRealtime();
                    long deadline = test ? (now < testDeadline ? testDeadline : 0)
                            : policy.observe(acc.read(), now);
                    if (deadline == 0 && !lease.isEmpty()) {
                        if (restore(lease)) lease = "";
                    } else if (deadline != 0 && lease.isEmpty() && deadline != attemptedDeadline) {
                        attemptedDeadline = deadline;
                        lease = begin(deadline);
                    }
                    if (!lease.isEmpty() && sampledDeadline != deadline
                            && now >= deadline - StartupWifiPolicy.HOLD_MILLIS + 5_000) {
                        sampledDeadline = deadline;
                        event("hold_sample ap_up=" + "up".equals(read(new File("/sys/class/net/wlan2/operstate")))
                                + " sta_up=" + "up".equals(read(new File("/sys/class/net/wlan0/operstate"))));
                    }
                    if (test && deadline == 0 && lease.isEmpty()) return;
                    Thread.sleep(250);
                }
            } finally {
                if (!lease.isEmpty()) restore(lease);
                Files.deleteIfExists(new File(ROOT, "watch.pid").toPath());
            }
        }
    }

    public static void main(String[] args) {
        if (android.os.Process.myUid() != 2000 || !ROOT.isDirectory()) return;
        try {
            WifiStartupGuard guard = new WifiStartupGuard();
            if (args.length == 2 && args[0].equals("recover")) guard.recover(args[1]);
            else if (args.length == 1 && args[0].equals("watch")) guard.watch(false);
            else if (args.length == 1 && args[0].equals("test-parked")) guard.watch(true);
            else if (args.length == 1 && args[0].equals("status")) {
                try (AccReader acc = new AccReader()) {
                    System.out.println("autojoin=" + guard.query() + " enabled=" + ENABLED.isFile()
                            + " hold=" + LEASE.isFile() + " acc=" + acc.read());
                }
            }
        } catch (Exception failure) {
            Throwable cause = failure.getCause() == null ? failure : failure.getCause();
            event("controller_error_recovery_retained type=" + cause.getClass().getSimpleName());
        }
    }
}
