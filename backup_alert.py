"""Sends a Telegram alert through notify.py when the backup fails.
Loads .env itself because the backup runs from a systemd timer, outside the engine."""
import os
import sys

sys.path.insert(0, "/home/azureuser")
try:
    with open("/home/azureuser/.env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except OSError:
    pass

import notify  # noqa: E402

detail = sys.argv[1] if len(sys.argv) > 1 else "unknown error"
# send_sync, not send(): this script exits right after this call, which used to kill send()'s
# background worker thread before it ever got a chance to actually reach Telegram -- a failed
# nightly backup was alerting nobody. See notify.send_sync's docstring.
ok = notify.send_sync(f"[ALERT] AETHER nightly backup FAILED: {detail}. Check: journalctl -u aether-backup")
if not ok:
    print(f"backup_alert: could not notify (Telegram disabled or unreachable). Backup failure: {detail}",
          file=sys.stderr)
