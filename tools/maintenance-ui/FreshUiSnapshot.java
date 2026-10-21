package com.dashcamstats.maintenance;

import android.app.UiAutomation;
import android.graphics.Point;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.File;

/** Read-only fresh accessibility snapshot. Never sends input or changes power policy. */
public final class FreshUiSnapshot {
    public static void main(String[] args) throws Exception {
        if (args.length != 1 || !args[0].matches(
                "/data/local/tmp/\\.dashcam_stats_maintenance_ui\\.[a-f0-9]{32}\\.xml")) {
            throw new IllegalArgumentException("invalid_output_path");
        }
        Class<?> wrapperClass = Class.forName(
                "com.android.uiautomator.core.UiAutomationShellWrapper");
        Object wrapper = wrapperClass.getConstructor().newInstance();
        wrapperClass.getMethod("connect").invoke(wrapper);
        try {
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
                    new File(args[0]), display.getRotation(), size.x, size.y);
        } finally {
            wrapperClass.getMethod("disconnect").invoke(wrapper);
        }
    }
}
