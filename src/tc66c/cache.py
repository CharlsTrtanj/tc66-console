"""Best-effort, per-user storage for a BLE device identifier, valid for 24 hours."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile


def cache_path() -> Path:
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "Caches"
    else:
        configured = os.environ.get("XDG_CACHE_HOME", "")
        directory = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".cache"
    return directory / "tc66c" / "ble-device.json"


def load_device() -> str | None:
    try:
        record = json.loads(cache_path().read_text(encoding="utf-8"))
        address = record["device"]
        saved_at = datetime.fromisoformat(record["saved_at"])
        age = (datetime.now(timezone.utc) - saved_at).total_seconds()
        if isinstance(address, str) and address.strip() and 0 <= age < 24 * 60 * 60:
            return address
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def save_device(address: str) -> None:
    temporary = None
    try:
        path = cache_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A private temporary file plus replacement prevents partially written JSON.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({"device": address, "saved_at": datetime.now(timezone.utc).isoformat()}, output)
            output.write("\n")
        temporary.replace(path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def forget_device() -> None:
    try:
        cache_path().unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
