"""Portable launcher, including the preview supervisor's host/port arguments."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=3000)
parser.add_argument('--strictPort', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
bundled = root / '.venv' / 'lib' / ('python' + str(sys.version_info.major) + '.' + str(sys.version_info.minor)) / 'site-packages'
if args.strictPort and (bundled / 'uvicorn').is_dir():
    # A mirrored checkout can contain a complete dependency environment whose
    # interpreter symlink targets another host. Use this host's matching Python.
    os.environ['PYTHONPATH'] = str(bundled)
    os.environ.setdefault('PUBLIC_BASE_URL', 'http://terminal.local:' + str(args.port))
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app:app', '--host', args.host, '--port', str(args.port), '--workers', '1', '--no-access-log'])
environment = Path(tempfile.gettempdir()) / 'onnivation-portal-dev-runtime' if args.strictPort else root / '.venv'
python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if not python.exists():
    subprocess.run([sys.executable, '-m', 'venv', '--copies', str(environment)], check=True)
stamp=environment / 'portal-requirements.sha256'
digest=hashlib.sha256((root / 'requirements.lock').read_bytes()).hexdigest()
if not stamp.exists() or stamp.read_text()!=digest:
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(root / 'requirements.lock')], check=True)
    stamp.write_text(digest)
os.chdir(root)
os.environ.setdefault('PUBLIC_BASE_URL', ('http://terminal.local:' if args.strictPort else 'http://localhost:') + str(args.port))
os.execv(str(python), [str(python), '-m', 'uvicorn', 'app:app', '--host', args.host, '--port', str(args.port), '--workers', '1', '--no-access-log'])
