import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "state" / "monitor_state.json"
DEFAULT_DONATIONS_STATE_FILE = Path(__file__).resolve().parent / "state" / "donations_state.json"


def state_file() -> Path:
    raw = os.environ.get("MONITOR_STATE_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_STATE_FILE


def donations_state_file() -> Path:
    raw = os.environ.get("DONATIONS_STATE_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DONATIONS_STATE_FILE


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or state_file()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"State: cannot read {path}: {exc!r}", flush=True)
        return {}
    if not isinstance(data, dict):
        print(f"State: {path} does not contain a JSON object, ignoring", flush=True)
        return {}
    return data


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    path = path or state_file()
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        return True
    except OSError as exc:
        print(f"State: cannot write {path}: {exc!r}", flush=True)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False


def chat_ids_from_env() -> list[int]:
    raw = os.environ.get("MONITOR_CHAT_ID", "")
    chat_ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            chat_ids.append(int(value))
        except ValueError:
            print(f"State: MONITOR_CHAT_ID entry {value!r} is not a number, skipping", flush=True)
    return chat_ids
