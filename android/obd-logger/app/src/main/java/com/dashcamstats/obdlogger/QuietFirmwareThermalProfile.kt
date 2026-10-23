package com.dashcamstats.obdlogger

/** A narrow compatibility profile, not a thermal-service override.
 * The Q30 firmware's HAL maps SoC 55C to SEVERE and 65C to CRITICAL, while its
 * aggregate service reported SEVERE at a measured 79.735C on 23 October 2026.
 * Identity comes from docs/head-unit-reference.md and the retained native snapshot.
 * Only this exact release may use the measured SoC 95C admission/100C running limits
 * with aggregate SEVERE. CRITICAL+, unknown status, battery and memory still refuse.
 * Every other build retains the generic SEVERE refusal; firmware throttling is untouched.
 */
internal object QuietFirmwareThermalProfile {
    fun matches(display: String?, model: String?, hardware: String?, sdk: Int,
                fingerprint: String?): Boolean =
        display == "QC_Q30_1025HJYEFPSL_U_Q30_MB_1025_CarCharger_user_2026060112" &&
            model == "Q30_1025HJYEFPSL_U" && hardware == "uis7861_6h10" && sdk == 34 &&
            fingerprint != null && fingerprint.startsWith(
                "UNISOC/uis7861_6h10_Natv/uis7861_6h10:14/UP1A.231005.007/") &&
            fingerprint.endsWith(":user/release-keys") &&
            fingerprint.substringAfter("UP1A.231005.007/").substringBefore(":user/release-keys")
                .matches(Regex("[A-Za-z0-9._-]{1,128}"))
}
