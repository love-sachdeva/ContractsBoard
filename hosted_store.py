"""Small encrypted Redis store for hosted OAuth grants and portal workflow metadata."""
import contextlib
import json
import os
import secrets
import time

import requests
from cryptography.fernet import Fernet


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, url=None, token=None, key=None, prefix="contractsboard"):
        self.url = (url or os.getenv("UPSTASH_REDIS_REST_URL", "")).rstrip("/")
        self.token = token or os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        self.prefix = prefix
        if not self.url.startswith("https://") or not self.token:
            raise StoreError("Configure Upstash Redis REST URL and token before starting the hosted portal.")
        self.cipher = Fernet((key or os.getenv("FERNET_KEY", "")).encode())

    def command(self, *args):
        try:
            result = requests.post(self.url, headers={"Authorization": "Bearer " + self.token}, json=list(args), timeout=20)
            result.raise_for_status()
            payload = result.json()
            if payload.get("error"):
                raise StoreError("Persistent store rejected an operation.")
            return payload.get("result")
        except (requests.RequestException, ValueError) as exc:
            raise StoreError("Persistent store is unavailable. No change was made.") from exc

    def _key(self, key):
        return self.prefix + ":" + key

    def get(self, key, default=None):
        value = self.command("GET", self._key(key))
        if value is None:
            return default
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except Exception as exc:
            raise StoreError("Stored data could not be decrypted. Restore the original FERNET_KEY.") from exc

    def put(self, key, value, ttl=None, only_new=False):
        data = self.cipher.encrypt(json.dumps(value).encode()).decode()
        args = ["SET", self._key(key), data]
        if only_new:
            args.append("NX")
        if ttl:
            args.extend(("EX", int(ttl)))
        return self.command(*args) == "OK"

    def pop(self, key):
        value = self.command("GETDEL", self._key(key))
        if value is None:
            return None
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except Exception as exc:
            raise StoreError("Stored flow could not be decrypted.") from exc

    def delete(self, key):
        return self.command("DEL", self._key(key))

    @contextlib.contextmanager
    def lock(self, key, seconds=600, wait=30):
        """A cross-function lock; only its owner can release it."""
        lock_key = self._key("lock:" + key)
        marker = secrets.token_urlsafe(24)
        deadline = time.monotonic() + wait
        while self.command("SET", lock_key, marker, "NX", "EX", seconds) != "OK":
            if time.monotonic() >= deadline:
                raise StoreError("Another operation is in progress. Try again shortly.")
            time.sleep(0.3)
        try:
            yield
        finally:
            # Atomic compare-and-delete prevents a slow request releasing another owner's lock.
            self.command("EVAL", "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end", 1, lock_key, marker)
