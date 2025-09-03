import logging
import threading
import os
import textwrap
from datetime import datetime

SPECIAL_TAGS_ICONS = {
    "OK": "✅",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "NET": "🌐",
    "...": "🔎",
    "MNKY": "🐒"
}

class LogManager:
    def __init__(self):
        self._log_lock = threading.Lock()
        self._log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(self._log_dir, exist_ok=True)

        self._log_file_name = f"debug_{datetime.now().strftime("%Y_%m_%d_%H%M")}.log"
        self._log_file_path = os.path.join(self._log_dir, self._log_file_name)

        self._logger = logging.getLogger("myLogger")
        self._logger.setLevel(logging.DEBUG)

        self._formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s - %(message)s")

        self._handler = logging.FileHandler(self._log_file_path, mode="a", encoding="utf-8")
        self._handler.setFormatter(self._formatter)
        self._handler.addFilter(self._hide_main_thread_filter())
        self._logger.addHandler(self._handler)

    def _hide_main_thread_filter(self):
        class HideMainThreadFilter(logging.Filter):
            def filter(self, record):
                record.threadName = "" if record.threadName == "MainThread" else f"[{record.threadName}]"
                return True
        return HideMainThreadFilter()

    def log(self, tag=None, text="", error=None, indent=None, level="log"):
        icon = SPECIAL_TAGS_ICONS.get(tag.upper() if tag else "  ", "  ")

        message = f"[{icon}][{tag.upper()}]" if tag else ""
        final_message = f"{message} {text}".strip()
        if error:
            final_message += f" : {error}"
        if indent:
            final_message = textwrap.indent(final_message, "-" * indent)

        with self._log_lock:
            if tag == "ERROR":
                self._logger.error(final_message)
            elif tag == "WARNING":
                self._logger.warning(final_message)
            elif tag == "OK":
                self._logger.info(final_message)
            else:
                self._logger.debug(final_message)

        if level == "print":
            print(final_message)

    def move_to_domain(self, domain, move_file=True):
        target_dir = os.path.join(self._log_dir, domain)
        os.makedirs(target_dir, exist_ok=True)

        target_path = os.path.join(target_dir, self._log_file_name)

        self._logger.removeHandler(self._handler)
        self._handler.close()

        if move_file:
            if os.path.exists(target_path):
                with open(self._log_file_path, "r", encoding="utf-8") as src, open(target_path, "a", encoding="utf-8") as dst:
                    self.log(None, "=" * 80)
                    dst.writelines(src.readlines())
                os.remove(self._log_file_path)
            else:
                os.rename(self._log_file_path, target_path)

            self._log_file_path = target_path
        else:
            target_path = self._log_file_path

        new_handler = logging.FileHandler(target_path, mode="a", encoding="utf-8")
        new_handler.setFormatter(self._formatter)
        new_handler.addFilter(self._hide_main_thread_filter())

        self._logger.addHandler(new_handler)
        self._handler = new_handler

log_manager = LogManager()
