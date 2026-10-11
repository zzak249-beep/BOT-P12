"""Telegram: envío de mensajes y comandos (/status /pause /resume /close /help)."""
import logging
import threading
import time

import requests

log = logging.getLogger("tg")


class NullNotifier:
    def send(self, text): pass
    def start_commands(self, handlers): pass


class Notifier:
    def __init__(self, token, chat_id):
        self.token, self.chat = token, str(chat_id)
        self.api = f"https://api.telegram.org/bot{token}"

    def send(self, text):
        if not self.token or not self.chat:
            print(text)
            return
        try:
            r = requests.post(f"{self.api}/sendMessage", timeout=15, json={
                "chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            if not r.ok:
                log.warning("Telegram %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Telegram error: %s", e)

    def start_commands(self, handlers):
        if not self.token:
            return
        threading.Thread(target=self._loop, args=(handlers,), daemon=True).start()

    def _loop(self, handlers):
        offset = 0
        while True:
            try:
                r = requests.get(f"{self.api}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=35).json()
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    if str(m.get("chat", {}).get("id")) != self.chat:
                        continue  # ignora a cualquiera que no seas tú
                    parts = (m.get("text") or "").split()
                    if not parts:
                        continue
                    h = handlers.get(parts[0].lower().split("@")[0])
                    if h:
                        self.send(h(" ".join(parts[1:])))
            except Exception as e:
                log.warning("getUpdates: %s", e)
                time.sleep(5)
