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

    def check(self):
        """Valida token y limpia webhooks (un webhook bloquea los comandos). Devuelve None si todo va bien."""
        if not self.token or not self.chat:
            return "faltan TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID"
        try:
            r = requests.get(f"{self.api}/getMe", timeout=15)
            if not r.ok:
                return f"token inválido (HTTP {r.status_code})"
            requests.get(f"{self.api}/deleteWebhook", timeout=15)
        except Exception as e:
            return f"sin conexión a Telegram: {e}"
        return None

    def send(self, text):
        if not self.token or not self.chat:
            print(text)
            return False
        import re
        for plain in (False, True):
            try:
                body = {"chat_id": self.chat, "text": re.sub(r"<[^>]+>", "", text) if plain else text,
                        "disable_web_page_preview": True}
                if not plain:
                    body["parse_mode"] = "HTML"
                r = requests.post(f"{self.api}/sendMessage", timeout=15, json=body)
                if r.ok:
                    return True
                log.error("Telegram %s: %s%s", r.status_code, r.text[:200],
                          " → ¿escribiste /start a tu bot y es correcto TELEGRAM_CHAT_ID?" if r.status_code in (400, 403) and "chat" in r.text.lower() else "")
                if r.status_code != 400:
                    return False
            except Exception as e:
                log.error("Telegram error: %s", e)
                return False
        return False

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
