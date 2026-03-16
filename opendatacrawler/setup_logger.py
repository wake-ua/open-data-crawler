import logging
import threading
import os
import textwrap
from datetime import datetime
import re

SPECIAL_TAGS_ICONS = {
    "OK": "✅",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "NET": "🌐",
    "...": "🔎",
    "INFO": "ℹ️",
    "WORK": "🧩",
    "SKIP": "⏭️",
    "SAVE": "💾",
    "FIX": "🛠️",
    "DEL": "🗑️",
    "STATS": "📊",
    "MNKY": "🐒",
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
        self._handler.addFilter(self._thread_name_filter())
        self._logger.addHandler(self._handler)

    def _thread_name_filter(self):
        class ThreadNameFilter(logging.Filter):
            def filter(self, record):
                if record.threadName == "MainThread":
                    record.threadName = "[_______]"
                else:
                    match = re.match(r"(.+?)_(\d+)(?:_(\d+))?$", record.threadName)
                    if match:
                        prefix = match.group(1)
                        subidx = match.group(3)

                        idx_str = f"{int(match.group(2)):02d}"
                        if subidx is not None:
                            subidx_str = f"{int(subidx):02d}"
                            record.threadName = f"[{prefix}_{idx_str}_{subidx_str}]"
                        else:
                            filler = "_" * (7 - len(prefix) - 3)
                            record.threadName = f"[{prefix}_{idx_str}{filler}]"
                    else:
                        record.threadName = f"[{record.threadName}]"
                return True
        return ThreadNameFilter()

    def log(self, tag=None, text="", error=None, indent=None, level="log", traceback=True):
        icon = SPECIAL_TAGS_ICONS.get(tag.upper() if tag else "  ", "  ")

        message = f"[{icon}][{tag.upper()}]" if tag else ""
        final_message = f"{message} {text}".strip()
        if error:
            if isinstance(error, list):
                if traceback:
                    final_message += f" :\n{error[1]}"
                else:
                    final_message += f" : {error[0]}"
            else:
                final_message += f" : {error}"

        if indent:
            final_message = textwrap.indent(" " + final_message, "-" * indent)

        with self._log_lock:
            if tag == "ERROR":
                self._logger.error(final_message)
            elif tag in {"WARNING", "SKIP"}:
                self._logger.warning(final_message)
            elif tag in {"OK", "SAVE", "FIX", "DEL"}:
                self._logger.info(final_message)
            else:
                self._logger.debug(final_message)

        if level == "print":
            print(final_message)

    def clean_unused_logs(self, min_lines=2):
        try:
            deleted = 0
            for file in os.listdir(self._log_dir):
                file_path = os.path.join(self._log_dir, file)
                if os.path.isfile(file_path) and file.endswith(".log"):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            lines = [line for line in f if line.strip()]

                        if len(lines) <= min_lines:
                            os.remove(file_path)
                            deleted += 1
                    except Exception as inner_e:
                        self.log("ERROR", f"Could not read or delete log: {file}", inner_e)

            if deleted:
                self.log("OK", f"Cleaned {deleted} orphan/empty log file(s)")
        except Exception as e:
            self.log("ERROR", "Failed during orphan log cleanup", e)

    def move_to_domain(self, domain, move_file=True):
        target_dir = os.path.join(self._log_dir, domain)
        os.makedirs(target_dir, exist_ok=True)

        target_path = os.path.join(target_dir, self._log_file_name)

        self._logger.removeHandler(self._handler)
        self._handler.close()

        if move_file:
            if os.path.exists(target_path):
                with open(self._log_file_path, "r", encoding="utf-8") as src, open(target_path, "a", encoding="utf-8") as dst:
                    dst.writelines(src.readlines())
                os.remove(self._log_file_path)
            else:
                os.rename(self._log_file_path, target_path)

            self._log_file_path = target_path
        else:
            target_path = self._log_file_path

        new_handler = logging.FileHandler(target_path, mode="a", encoding="utf-8")
        new_handler.setFormatter(self._formatter)
        new_handler.addFilter(self._thread_name_filter())

        self._logger.addHandler(new_handler)
        self._handler = new_handler

        self.log(None, "=" * 80)

log_manager = LogManager()
