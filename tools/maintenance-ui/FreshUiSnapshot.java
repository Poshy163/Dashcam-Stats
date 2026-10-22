package com.dashcamstats.maintenance;

import android.app.UiAutomation;
import android.graphics.Point;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;

/** Read-only fresh accessibility snapshot. Never sends input or changes power policy. */
public final class FreshUiSnapshot {
    private static final File LOCK = new File(
            "/data/local/tmp/.dashcam_stats_maintenance_ui.lock");

    /** Kernel ownership disappears on timeout/process death; no stale lock directory. */
    private static FileLock acquireSnapshotLock(FileChannel channel) throws IOException {
        FileLock lock;
        try {
            lock = channel.tryLock();
        } catch (OverlappingFileLockException busy) {
            throw new IllegalStateException("ui_snapshot_busy");
        }
        if (lock == null) throw new IllegalStateException("ui_snapshot_busy");
        return lock;
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1 || !args[0].matches(
                "/data/local/tmp/\\.dashcam_stats_maintenance_ui\\.[a-f0-9]{32}\\.xml")) {
            throw new IllegalArgumentException("invalid_output_path");
        }
        if (!LOCK.getCanonicalFile().equals(LOCK.getAbsoluteFile())) {
            throw new IllegalStateException("invalid_lock_path");
        }
        // Android allows one UiAutomation owner. Do not connect while another
        // snapshot owns it: a second connect can evict a healthy heartbeat reader.
        try (RandomAccessFile file = new RandomAccessFile(LOCK, "rw");
                FileChannel channel = file.getChannel();
                FileLock lock = acquireSnapshotLock(channel)) {
            writeSnapshot(args[0]);
        } catch (IllegalStateException refused) {
            if ("ui_snapshot_busy".equals(refused.getMessage())) System.exit(75);
            throw refused;
        }
    }

    private static void writeSnapshot(String output) throws Exception {
        Class<?> wrapperClass = Class.forName(
                "com.android.uiautomator.core.UiAutomationShellWrapper");
        Object wrapper = wrapperClass.getConstructor().newInstance();
        try {
            wrapperClass.getMethod("connect").invoke(wrapper);
            wrapperClass.getMethod("setCompressedLayoutHierarchy", boolean.class)
                    .invoke(wrapper, true);
            UiAutomation automation = (UiAutomation) wrapperClass.getMethod("getUiAutomation")
                    .invoke(wrapper);
            // A new connection requests the current root. Waiting for a full second
            // without events would never finish on the one-second native countdown.
            AccessibilityNodeInfo root = null;
            for (int attempt = 0; attempt < 10 && root == null; attempt++) {
                root = automation.getRootInActiveWindow();
                if (root == null) Thread.sleep(100);
            }
            if (root == null) throw new IllegalStateException("no_root");
            Class<?> managerClass = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object manager = managerClass.getMethod("getInstance").invoke(null);
            Display display = (Display) managerClass.getMethod("getRealDisplay", int.class)
                    .invoke(manager, 0);
            Point size = new Point();
            display.getSize(size);
            Class<?> dumperClass = Class.forName(
                    "com.android.uiautomator.core.AccessibilityNodeInfoDumper");
            dumperClass.getMethod("dumpWindowToFile", AccessibilityNodeInfo.class,
                    File.class, int.class, int.class, int.class).invoke(null, root,
                    new File(output), display.getRotation(), size.x, size.y);
        } finally {
            // The outer try-with-resources also releases the kernel lock if a
            // partial connect or disconnect throws; process exit closes its binder.
            wrapperClass.getMethod("disconnect").invoke(wrapper);
        }
    }
}
