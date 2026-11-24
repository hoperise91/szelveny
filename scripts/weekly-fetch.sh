#!/bin/bash
# Weekly fetch helper (runs on the owner's Mac via launchd, see deploy/hu.szelveny.fetch.plist).
# Some public servers refuse CI machines (Geofabrik at times), so the Mac downloads fresh
# OpenStreetMap once a week and saves it to the private R2 bucket; the GitHub weekly refresh
# uses this copy when a server refuses it.
# launchd starts this every few hours; it does the work at most once every 6 days.
set -euo pipefail
cd "$(dirname "$0")/.."
stamp=data/out/fetch/last_success
log=data/out/fetch/fetch.log
mkdir -p data/out/fetch
if [ -f "$stamp" ] && [ -z "$(find "$stamp" -mtime +6)" ]; then
  exit 0   # fetched less than 6 days ago
fi
# Not on a hotspot: macOS flags iPhone Personal Hotspot as expensive, and any Wi-Fi network with
# Low Data Mode on (System Settings → Wi-Fi → network → Details) as low-data. Skip, retry in 3 h.
netcheck=data/out/fetch/netcheck
if [ ! -x "$netcheck" ] || [ scripts/netcheck.swift -nt "$netcheck" ]; then
  swiftc -O scripts/netcheck.swift -o "$netcheck" 2>> "$log"
fi
net=$("$netcheck")
case "$net" in
  online\ normal\ unlimited) ;;
  *) echo "$(date '+%F %T') skipped: connection is '$net'" >> "$log"; exit 0 ;;
esac
{
  echo "=== $(date '+%F %T') weekly fetch"
  make download SOURCES=osm
  make snapshot-raw
  touch "$stamp"
  echo "=== done"
} >> "$log" 2>&1
