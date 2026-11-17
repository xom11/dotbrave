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
bundle_id="${BUNDLE:-$(/usr/bin/basename "$DEST" .plist)}"
if [ -n "$bundle_id" ]; then
  /usr/bin/osascript -l JavaScript -e '
function run(argv) {
  ObjC.import("Foundation");
  ObjC.bindFunction("_CFPreferencesManagementStatusChangedForDomains", ["void", ["id"]]);
  ObjC.bindFunction("_CFPreferencesFlushCachesForIdentifier", ["void", ["id", "id"]]);
  var d = $(argv[0]);
  $._CFPreferencesManagementStatusChangedForDomains($([d]));
  $._CFPreferencesFlushCachesForIdentifier(d, $("kCFPreferencesAnyUser"));
  $._CFPreferencesFlushCachesForIdentifier(d, $("kCFPreferencesCurrentUser"));
}' "$bundle_id" >/dev/null 2>&1
fi
/bin/echo "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ) healed" >> "$LOG"
# Wake any running browser watching per-login subdirectories under DEST's parent.
uid=$(/usr/bin/stat -f%u /dev/console 2>/dev/null)
console_user=""
if [ -n "$uid" ] && [ "$uid" -ge 501 ] 2>/dev/null; then
  console_user=$(/usr/bin/id -un "$uid" 2>/dev/null)
fi
dest_dir="$(dirname "$DEST")"
dest_name="$(/usr/bin/basename "$DEST")"
created_files=""
created_dirs=""
for login_name in root $console_user; do
  [ -n "$login_name" ] || continue
  udir="$dest_dir/$login_name"
  ufile="$udir/$dest_name"
  if [ -e "$ufile" ]; then
    /usr/bin/touch "$ufile" 2>/dev/null
    continue
  fi
  if [ ! -d "$udir" ]; then
    /bin/mkdir -p -m 0755 "$udir" 2>/dev/null && created_dirs="$created_dirs
$udir"
  fi
  if /usr/bin/plutil -create binary1 "$ufile" 2>/dev/null; then
    /bin/chmod 0644 "$ufile" 2>/dev/null
    created_files="$created_files
$ufile"
  fi
done
if [ -n "$created_files" ]; then
  /bin/sleep 0.15
  printf '%s\n' "$created_files" | while IFS= read -r f; do
    [ -n "$f" ] && /bin/rm -f "$f" 2>/dev/null
  done
fi
if [ -n "$created_dirs" ]; then
  printf '%s\n' "$created_dirs" | while IFS= read -r d; do
    [ -n "$d" ] && /bin/rmdir "$d" 2>/dev/null
  done
fi
# Before login /dev/console belongs to root, so there is nobody to tell;
# the log above already caught it.
if [ -n "$uid" ] && [ "$uid" -ge 501 ] 2>/dev/null; then
  /bin/launchctl asuser "$uid" /usr/bin/osascript \
    -e "display notification \"Managed PWA policy for ${BUNDLE:-the browser} was restored; a running browser reloads it automatically.\" with title \"dotbrave\"" \
    2>/dev/null
fi
exit 0
