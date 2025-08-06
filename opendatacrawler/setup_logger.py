import logging
import threading
import os
import textwrap
from datetime import datetime

log_lock = threading.Lock()

def logger(tag=None, text="", error=None, indent=None, level="log"):
    icon = ""
    message = ""

    if tag:
        tag = tag.upper()
        if tag == "OK":
            icon = "✅"
        elif tag == "WARNING":
            icon = "⚠️"
        elif tag == "ERROR":
            icon = "❌"
        elif tag == "...":
            icon = "🔎"
        message = f"[{icon}][{tag}]" if icon else f"[{tag}]"

    final_message = f"{message} {text}".strip()
    if error:
        final_message += f" : {error}"

    if indent:
        final_message = textwrap.indent(final_message, " " * indent)

    with log_lock:
        if tag == "ERROR":
            logger_obj.error(final_message)
        elif tag == "WARNING":
            logger_obj.warning(final_message)
        elif tag == "OK":
            logger_obj.info(final_message)
        else:
            logger_obj.debug(final_message)

    #if level == "print" or tag in ["ERROR", "WARNING"]:
    if level == "print":
        print(f"{final_message}")

logger_obj = logging.getLogger("myLogger")
logger_obj.setLevel(logging.DEBUG)

log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)

today = datetime.now().strftime("%Y%m%d")
log_file_path = os.path.join(log_dir, f"debug_{today}.log")

file_handler = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

logger_obj.addHandler(file_handler)