"""Check the embedded JavaScript without browser installation."""
import re
import subprocess
import tempfile
from pathlib import Path

html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
with tempfile.TemporaryDirectory() as directory:
    script = Path(directory) / 'frontend.js'
    script.write_text('\n'.join(scripts))
    subprocess.run(['node', '--check', str(script)], check=True)
print('Frontend JavaScript syntax passed')
