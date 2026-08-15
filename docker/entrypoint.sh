#!/usr/bin/env bash
#
# Grants the unprivileged app account access to the iGPU, then drops privileges.
#
# The GID that owns /dev/dri/renderD* is a property of the *host*, not the image — it is
# commonly 44 (video), 104/105/107 (render), or something else entirely depending on the
# distribution. Hardcoding it is the usual reason "/dev/dri is mapped but hardware
# acceleration doesn't work". So we read it at start-up and join the group we actually find.
set -euo pipefail

APP_USER="${APP_USER:-dashcam}"
DATA_DIR="${DASHCAM_DATA_DIR:-/data}"
FOOTAGE_DIR="${DASHCAM_FOOTAGE_DIR:-/dashcam}"

log() { printf '[entrypoint] %s\n' "$*"; }

grant_dri_access() {
    shopt -s nullglob
    local nodes=(/dev/dri/render* /dev/dri/card*)
    shopt -u nullglob

    if [ ${#nodes[@]} -eq 0 ]; then
        log "no /dev/dri devices present — running with CPU processing only"
        log "  to enable hardware acceleration add:  devices: [ /dev/dri:/dev/dri ]"
        return
    fi

    local seen=()
    for node in "${nodes[@]}"; do
        local gid
        gid="$(stat -c '%g' "$node")"

        # shellcheck disable=SC2076
        if [[ " ${seen[*]:-} " =~ " ${gid} " ]]; then continue; fi
        seen+=("$gid")

        local group
        group="$(getent group "$gid" | cut -d: -f1 || true)"
        if [ -z "$group" ]; then
            group="dri${gid}"
            groupadd --gid "$gid" "$group" 2>/dev/null || true
        fi

        if usermod --append --groups "$group" "$APP_USER" 2>/dev/null; then
            log "granted ${APP_USER} access to $(basename "$node") via group ${group} (gid ${gid})"
        else
            log "WARNING: could not add ${APP_USER} to group ${group} for $(basename "$node")"
        fi
    done
}

prepare_dirs() {
    mkdir -p "$DATA_DIR"
    # Only /data is ours to own. The footage mount is the user's, is frequently read-only,
    # and must never be chowned or written to.
    if ! chown -R "$APP_USER":"$APP_USER" "$DATA_DIR" 2>/dev/null; then
        log "WARNING: could not take ownership of ${DATA_DIR}; check volume permissions"
    fi

    if [ ! -d "$FOOTAGE_DIR" ]; then
        log "WARNING: footage directory ${FOOTAGE_DIR} is not present."
        log "         Scanning will find nothing and retention will refuse to run."
    elif [ -z "$(ls -A "$FOOTAGE_DIR" 2>/dev/null)" ]; then
        log "NOTE: footage directory ${FOOTAGE_DIR} is empty — is the share mounted?"
        log "      Retention treats an empty footage directory as a fault, never as"
        log "      permission to delete."
    fi
}

prepare_adb_key() {
    # The head unit authorises a *key*, not a container. Keeping it on the data volume
    # means rebuilding the image does not put an "Allow USB debugging?" dialog in front of
    # a car that nobody is sitting in. ANDROID_USER_HOME is honoured by newer adb; the
    # symlink covers builds that still only look at $HOME/.android.
    local key_dir="${DATA_DIR}/.android"
    mkdir -p "$key_dir"
    chown -R "$APP_USER":"$APP_USER" "$key_dir" 2>/dev/null || true
    chmod 700 "$key_dir" 2>/dev/null || true

    local home
    home="$(getent passwd "$APP_USER" | cut -d: -f6)"
    if [ -n "$home" ] && [ ! -e "${home}/.android" ]; then
        ln -sfn "$key_dir" "${home}/.android" 2>/dev/null || true
        chown -h "$APP_USER":"$APP_USER" "${home}/.android" 2>/dev/null || true
    fi
}

if [ "$(id -u)" = "0" ]; then
    grant_dri_access
    prepare_dirs
    prepare_adb_key
    exec_prefix=(gosu "$APP_USER")
else
    # Already unprivileged (user: was set in compose); device access is the operator's
    # responsibility via group_add.
    log "running as uid $(id -u); skipping group setup"
    exec_prefix=()
fi

case "${1:-serve}" in
    serve)
        exec "${exec_prefix[@]}" python -m app.main
        ;;
    migrate)
        exec "${exec_prefix[@]}" python -m alembic -c /app/backend/alembic.ini upgrade head
        ;;
    recover-login)
        # The way back in after locking yourself out. Runs as the app account rather than
        # as root, so it cannot leave root-owned WAL files beside the database.
        shift
        exec "${exec_prefix[@]}" python -m app.auth.recover "$@"
        ;;
    shell)
        exec "${exec_prefix[@]}" /bin/bash
        ;;
    *)
        exec "${exec_prefix[@]}" "$@"
        ;;
esac
