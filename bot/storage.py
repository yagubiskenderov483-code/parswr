from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Any:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(self.default))

    def save(self, data: Any) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)


class SubscriptionStore:
    def __init__(self, path: Path):
        self._store = JsonStore(path, {})

    def all(self) -> dict[str, list[str]]:
        return self._store.load()

    def get(self, user_id: int) -> set[str]:
        return set(self.all().get(str(user_id), []))

    def set(self, user_id: int, keys: set[str]) -> None:
        data = self.all()
        if keys:
            data[str(user_id)] = sorted(keys)
        else:
            data.pop(str(user_id), None)
        self._store.save(data)

    def toggle(self, user_id: int, key: str) -> bool:
        keys = self.get(user_id)
        if key in keys:
            keys.remove(key)
            enabled = False
        else:
            keys.add(key)
            enabled = True
        self.set(user_id, keys)
        return enabled

    def subscribers_for(self, key: str) -> list[int]:
        return [int(uid) for uid, keys in self.all().items() if key in keys]


class SeenLotsStore:
    def __init__(self, path: Path):
        self._store = JsonStore(path, {"ids": []})

    def load_ids(self) -> set[str]:
        return set(self._store.load().get("ids", []))

    def save_ids(self, ids: set[str], limit: int = 30000) -> None:
        trimmed = list(ids)[-limit:]
        self._store.save({"ids": trimmed})
