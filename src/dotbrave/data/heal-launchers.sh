#!/bin/sh
# dotbrave PWA launcher heal (Linux). Managed automatically; do not edit.
#
# Inputs, all from the environment:
#   APPS  the applications dir Brave writes PWA launchers into
#   SNAP  last known-good copy of each launcher, kept by this script
#   LOG   append-only record of every launcher it had to fix
#
# Why it exists: the managed [pwa] policy applies to every --user-data-dir,
# so a Brave started on a throwaway profile force-installs the same apps and
# writes brave-<app-id>-<Profile>.desktop -- the same file names the real
# profile uses -- with its own --user-data-dir in Exec. Every launcher then
# opens the empty throwaway profile, and nothing reports it.
#
# A launcher without --user-data-dir is good and refreshes the snapshot. A
# launcher with it is restored from the snapshot. One with no snapshot is an
# app id only the throwaway profile has -- its manifest resolved differently
# there, so the same URL hashed to another id with the URL as its name -- and
# is deleted with its icons. Except on the first run, before any launcher was
# seen good: then it could be a real app's only launcher, so the flag is
# stripped instead. Idempotent: a second run writes nothing, so the path unit
# that runs it does not loop on its own writes.
[ -n "$APPS" ] && [ -n "$SNAP" ] && [ -n "$LOG" ] || exit 0
[ -d "$APPS" ] || exit 0
mkdir -p "$SNAP" "$(dirname "$LOG")" || exit 1
established=0
[ -f "$SNAP/.established" ] && established=1
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

for f in "$APPS"/brave*.desktop; do
  [ -f "$f" ] || continue
  grep -q -- '--app-id=' "$f" || continue
  name=${f##*/}
  if grep -q -- '--user-data-dir=' "$f"; then
    if [ -f "$SNAP/$name" ]; then
      cp "$SNAP/$name" "$f" || exit 1
      echo "$(now) restored $name" >> "$LOG"
    elif [ "$established" = 1 ]; then
      rm -f "$f" "${APPS%/applications}"/icons/hicolor/*/apps/"${name%.desktop}".png
      echo "$(now) removed $name (app id the real profile does not have)" >> "$LOG"
    else
      sed -i -e 's/ "--user-data-dir=[^"]*"//g' -e 's/ --user-data-dir=[^ ]*//g' "$f" || exit 1
      echo "$(now) stripped $name (first run, no known-good copy)" >> "$LOG"
    fi
  else
    # Brave adds NoDisplay=true whenever it rewrites a launcher that already
    # existed -- measured on a placeholder reinstall and on three apps that
    # gained the policy source on top of a user install -- which drops a
    # managed app from the menu while it stays installed. No fresh install
    # measured so far has carried it.
    if grep -q '^NoDisplay=true$' "$f"; then
      sed -i '/^NoDisplay=true$/d' "$f" || exit 1
      echo "$(now) unhid $name" >> "$LOG"
    fi
    cmp -s "$f" "$SNAP/$name" || cp "$f" "$SNAP/$name" || exit 1
  fi
done
[ "$established" = 1 ] || : > "$SNAP/.established"
# Snapshots are never pruned when a launcher disappears: Brave rewrites a
# launcher by deleting and recreating it, and a run landing in that gap once
# forgot a real app's snapshot -- the next run took the hijacked copy for a
# throwaway-only id and deleted it. A stale snapshot is harmless: a
# throwaway profile only writes ids the policy installs, which the real
# profile then has too.
exit 0
