import json
import os
import logging
from threading import Lock

log = logging.getLogger(__name__)

STORE_PATH = os.environ.get("SUBSCRIBER_FILE", "/tmp/subscribers.json")


class SubscriberStore:
    """Simple file-backed subscriber list (thread-safe)."""

    def __init__(self):
        self._lock = Lock()
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(STORE_PATH):
            self._write([])

    def _read(self) -> list:
        try:
            with open(STORE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _write(self, data: list):
        with open(STORE_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, number: str):
        with self._lock:
            subs = self._read()
            # Normalise: strip whatsapp: prefix for storage
            number = number.replace("whatsapp:", "")
            if number not in subs:
                subs.append(number)
                self._write(subs)
                log.info(f"Subscriber added: {number}")

    def remove(self, number: str):
        with self._lock:
            number = number.replace("whatsapp:", "")
            subs   = [s for s in self._read() if s != number]
            self._write(subs)
            log.info(f"Subscriber removed: {number}")

    def get_all(self) -> list:
        with self._lock:
            return [s for s in self._read()]

    def count(self) -> int:
        return len(self.get_all())
