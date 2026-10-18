# AETHER backups into iCloud Drive (pulled by your Mac)

A Linux server cannot write to iCloud, so the Mac pulls. Each run: download the archives (ssh `list`/`get`, no rsync) into
`iCloud Drive/AETHER-backups`, `gzip -t` the newest, then send a receipt to the server. The dashboard shows
"offsite iCloud (via Mac)" only while the Mac has confirmed the newest archive; otherwise it says "behind"
or "not set". The Mac must be on and online at some point each day; the server keeps 14 days.

## Setup (once)
1. Mac: `ssh-keygen -t ed25519 -N "" -f ~/.ssh/aether_backup_key` then `cat ~/.ssh/aether_backup_key.pub`.
2. Server: append to `~/.ssh/authorized_keys` (from the Mac, so the key text never has to be typed):
   `command="/home/ratelark/ratelark/deploy/offsite_gate.sh",restrict ssh-ed25519 AAAA... aether-backup`
   (the key then cannot open a shell or read anything except the archives).
3. Mac: `mkdir -p ~/aether-backup && cp pull_backups.sh ~/aether-backup/` (from this folder), then run it once:
   `bash ~/aether-backup/pull_backups.sh; tail ~/Library/Logs/aether-icloud-backup.log` (expect `OK ...`).
4. Mac: install the schedule (runs at 08:30 and when the Mac wakes after a missed run):
   `sed "s|HOME_PLACEHOLDER|$HOME|" com.aether.icloud-backup.plist > ~/Library/LaunchAgents/com.aether.icloud-backup.plist`
   `launchctl load ~/Library/LaunchAgents/com.aether.icloud-backup.plist`
If the log says "Operation not permitted", give `/bin/bash` Full Disk Access (System Settings > Privacy & Security).
