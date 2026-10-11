"""Persistencia mínima en JSON (usa un Volume de Railway montado en /data)."""
import json
import os


class Store:
    def __init__(self, path):
        self.path = path
        self.d = {"loss_streak": 0, "traded_day": {}, "positions": {}, "paused": False, "trades": []}
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self.d.update(json.load(f))
            except Exception:
                pass
        if not isinstance(self.d.get("traded_day"), dict):
            self.d["traded_day"] = {}
        if not isinstance(self.d.get("positions"), dict):
            self.d["positions"] = {}

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)
