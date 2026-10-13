"""Per-guild channel settings, saved atomically across process restarts."""
import json
from pathlib import Path
from threading import RLock


class ChannelSettings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = RLock()
        self.channels = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not key.isdecimal() or type(value) is not int or value <= 0
                for key, value in data.items()
            ):
                raise ValueError("Invalid channel settings; refusing to discard restrictions")
            self.channels = data

    def get(self, guild_id):
        with self._lock:
            return self.channels.get(str(guild_id))

    def allows(self, guild_id, channel_id):
        return guild_id is None or self.get(guild_id) in (None, channel_id)

    def set(self, guild_id, channel_id):
        with self._lock:
            updated = dict(self.channels)
            if channel_id is None:
                updated.pop(str(guild_id), None)
            else:
                updated[str(guild_id)] = channel_id
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(updated), encoding="utf-8")
            temporary.replace(self.path)
            self.channels = updated