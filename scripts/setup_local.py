"""Create local configuration once; preserve existing credentials and runtime data."""
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
env = root / '.env'
if env.exists():
    print('Existing .env preserved. Set PUBLIC_BASE_URL to your actual localhost origin.')
else:
    password = secrets.token_urlsafe(18)
    env.write_text((root / '.env.example').read_text().replace('SET_PORTAL_PASSWORD', password).replace('SET_FERNET_KEY', Fernet.generate_key().decode()))
    env.chmod(0o600)
    print('Local configuration created. Username: finance')
    print('Local portal password:', password)
print('Start: uvicorn app:app --host 127.0.0.1 --port 3000 --workers 1 --no-access-log')
