#!/bin/sh
# dotbrave self-healing managed-policy script. Managed automatically; do not edit.
#
# Inputs, all from the environment:
#   SRC     source-of-truth plist (root-owned, read-only)
#   DEST    managed plist macOS prunes at boot
#   LOG     append-only evidence file
#   BUNDLE  bundle id, used in the user-facing notification
#
# Idempotent: if DEST already matches SRC it exits without writing, which
# prevents a WatchPaths write->notify->write loop. Writing is a
# lift-write-pin dance because DEST carries schg, which blocks this
# script's own cp just as surely as it blocks the reconcile it defeats.
[ -n "$SRC" ] && [ -n "$DEST" ] && [ -n "$LOG" ] || exit 0
[ -f "$SRC" ] || exit 0
if cmp -s "$SRC" "$DEST"; then exit 0; fi
/bin/mkdir -p "$(dirname "$DEST")"
/bin/mkdir -p "$(dirname "$LOG")"
/usr/bin/chflags noschg "$DEST" 2>/dev/null
# Bail rather than log a heal that did not happen -- the log is evidence,
# and evidence that lies is worse than none.
/bin/cp "$SRC" "$DEST" || exit 1
/usr/bin/chflags schg "$DEST" 2>/dev/null
/usr/bin/killall cfprefsd 2>/dev/null
/bin/echo "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ) healed" >> "$LOG"
# Before login /dev/console belongs to root, so there is nobody to tell;
# the log above already caught it.
uid=$(/usr/bin/stat -f%u /dev/console 2>/dev/null)
if [ -n "$uid" ] && [ "$uid" -ge 501 ] 2>/dev/null; then
  /bin/launchctl asuser "$uid" /usr/bin/osascript \
    -e "display notification \"Managed PWA policy for ${BUNDLE:-the browser} was restored. Restart the browser to reinstall the apps.\" with title \"dotbrave\"" \
    2>/dev/null
fi
exit 0
