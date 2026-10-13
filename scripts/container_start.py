"""Prepare a newly mounted volume, then drop root before starting the portal."""
import os
from pathlib import Path

data = Path(os.environ.get('DATA_DIR', '/data'))
data.mkdir(parents=True, exist_ok=True)
if os.getuid() == 0:
    for directory, subdirs, files in os.walk(data):
        os.chown(directory, 10001, 10001)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
os.umask(0o077)
os.execvp('uvicorn', ['uvicorn', 'app:app', '--host', '0.0.0.0', '--port', os.environ.get('PORT', '3000'), '--workers', '1', '--no-access-log'])
