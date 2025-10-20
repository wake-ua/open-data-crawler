import os
import json
import mimetypes
import hashlib
import chardet
import olefile
import re
import xml.etree.ElementTree as ET
from io import BytesIO
import configparser
import gc
import time
import tempfile
import mmap
import codecs
import statistics
import socket
from datetime import datetime
from collections import Counter
from ftfy.badness import is_bad
from ftfy import fix_text
from w3lib.url import url_query_cleaner
from bs4 import BeautifulSoup
from url_normalize import url_normalize
import requests
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

ENCODING_CANDIDATES = ["utf-8", "iso-8859-1", "windows-1252", "windows-1250", "cp850"]
NOT_ALLOWED_DELIMITERS = [":", " ",'"', "'", "_", "(", ")", "<", ">", "[", "]", "{", "}", "-", ".", "+", "*", "=", "/", "\\", "�", "@", "”", "#", "“"]
QUOTE_CHAR = '"'

PERMANENT_UNAVAILABLE_TAGS = {"resource_removed", "unresolvable_domain", "method_not_allowed", "missing_resource", "forbidden_resource", "ssl_error", "invalid_request"}

RAW_SIGNATURES = [
    (b"\x37\x7A\xBC\xAF\x27\x1C", "application/x-7z-compressed"),
    (b"\x52\x61\x72\x21\x1A\x07", "application/x-rar-compressed"),
    (b"PK0", "application/zip-empty"),
    (b"<?xml", "text/xml"),
    (b"<!", "text/html"),
    (b"<!DOCTYPE html>", "text/html"),
    (b"<html", "text/html"),
]

GENERIC_MIME_TYPES = {"application/octet-stream", "text/plain", "application/force-download"}

# ==============================
# Filesystem functions
# ==============================

def create_folder(path):
    if os.path.exists(path):
        logger("...", f"The directory '{path}' already exists, skipping creation...")
        return True

    try:
        os.makedirs(path, exist_ok=False)
        logger("OK", f"Successfully created the directory '{path}'")
        return True
    except OSError as e:
        logger("ERROR", f"Failed to create the directory '{path}'", e)
        return False

def delete_tempfiles(file_paths, keep_path=None):
    for path in file_paths:
        if path != keep_path and path and os.path.exists(path):
            os.remove(path)

def is_empty_file(path):
    try:
        return os.path.getsize(path) == 0
    except Exception as e:
        logger("ERROR", f"Could not check file size for '{path}'", e)
        return True

# ==============================
# Type and structure detection functions
# ==============================

def is_url(text):
    return isinstance(text, str) and (text.startswith("http://") or text.startswith("https://"))

def is_json(field):
    if not isinstance(field, str):
        return False
    try:
        json.loads(field)
        return True
    except json.JSONDecodeError:
        return False

def is_geojson(field):
    if isinstance(field, dict):
        return "type" in field and ("coordinates" in field or "features" in field)
    if isinstance(field, str) and is_json(field):
        try:
            return is_geojson(json.loads(field))
        except Exception:
            return False
    return False

# ==============================
# Request functions
# ==============================

def fix_url(url):
    url = url.strip().replace(" ", "%20")
    if not is_url(url):
        url = f"https://{url}"
    return url

def normalize_domain(domain):
    if "gbif.org" in domain and not domain.startswith("http://api.gbif.org"):
        return "http://api.gbif.org"

    lang_suffix = re.compile(r"/[a-z]{2}$", re.IGNORECASE)
    domain = lang_suffix.sub("", domain)

    return domain

def get_user_agent_list(current_agent):
    return [current_agent] + [ua for ua in USER_AGENTS if ua != current_agent] if current_agent else USER_AGENTS

def make_request(url, current_agent, headers=None, params=None, max_sec=None, stream=False, sleep_time=3, return_tag=False):
    headers = headers.copy() if headers else {}

    all_forbidden = True
    for user_agent in get_user_agent_list(current_agent):
        headers["User-Agent"] = user_agent
        while True:
            try:
                response = requests.get(url, headers=headers, params=params, verify=False, timeout=max_sec, stream=stream)

                if response.status_code == 403:
                    #logger("NET", f"Forbidden with User-Agent '{user_agent}' (HTTP 403 - Forbidden), trying next one...")
                    break
                elif response.status_code == 429:
                    #logger("NET", f"Too Many Requests (HTTP 429 - Too Many Requests), retrying with same User-Agent after {sleep_time}s", indent=2)
                    time.sleep(sleep_time)
                    continue

                all_forbidden = False
                if return_tag:
                    try:
                        response.raise_for_status()
                    except requests.exceptions.RequestException as e:
                        tag = get_error_tag_from_exception(e)
                        tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                        if tag == "resource_temporarily_unavailable":
                            return None, user_agent, tag, f"{tag_explanation} ({url}) - [{e}]"
                        else:
                            return None, user_agent, tag, f"{tag_explanation} ({url})"
                    return response, user_agent, None, None

                return response, user_agent

            except requests.exceptions.RequestException as e:
                tag = get_error_tag_from_exception(e)
                tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                if tag == "resource_temporarily_unavailable":
                    return None, None, tag, f"{tag_explanation} ({url}) - [{e}]" if return_tag else (None, None)
                else:
                    return None, None, tag, f"{tag_explanation} ({url})" if return_tag else (None, None)

    if return_tag:
        if all_forbidden:
            return None, None, "forbidden_resource", f"{CRAWLER_CHANGES_INFO.get("forbidden_resource", {}).get("tag_explanation", {}).get("reason", "Unknown reason")} ({url})"
        return None, None, None, None
    else:
        return None, None

def clean_url(u):
    u = url_normalize(u)
    u = url_query_cleaner(u, remove=True, parameterlist=["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"])

    for prefix in ("http://", "https://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]

    return u.split("/")[0]

def get_error_tag_from_exception(e):
    if isinstance(e, tuple) and len(e) == 2 and isinstance(e[1], BaseException):
        e = e[1]

    if isinstance(e, socket.gaierror):
        return "unresolvable_domain"

    if isinstance(e, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ChunkedEncodingError,
        ConnectionResetError,
    )):
        return "resource_temporarily_unavailable"

    if isinstance(e, requests.exceptions.SSLError):
        return "ssl_error"

    status_code = getattr(getattr(e, "response", None), "status_code", None)
    tag = get_https_error_tag(status_code)
    if tag:
        return tag

    raise ValueError(f"Unhandled exception type: {e}")

def get_https_error_tag(status_code):
    if status_code:
        if status_code == 400:
            return "invalid_request"
        elif status_code == 401:
            return "unauthorized_access"
        elif status_code == 404:
            return "missing_resource"
        elif status_code == 405:
            return "method_not_allowed"
        elif status_code == 410:
            return "resource_removed"
        elif status_code in [500, 502, 503, 504]:
            return "resource_temporarily_unavailable"
    return None

def extract_namespaces(xml_data):
    ns = {}
    for event, elem in ET.iterparse(BytesIO(xml_data), events=("start-ns",)):
        prefix, uri = elem
        ns[prefix or "default"] = uri
    return ns

def get_xml_text(element):
    if element is not None and element.text:
        return element.text.strip()
    return None

def get_xml_attr(element, ns, attr_name):
    if element is None:
        return None
    return element.attrib.get(f"{{{ns}}}{attr_name}")

# ==============================
# Resume / recovery functions
# ==============================

def recover_resume(save_path, accepted_types=None):
    packages_status = {}
    total_failed = []
    total_successful = []
    total_unavailable_permanent = []
    failed_packages = set()

    for fname in os.listdir(save_path):
        if not fname.startswith("meta_"):
            continue

        meta_path = os.path.join(save_path, fname)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            identifier = meta.get("identifier")
            failed = []
            success = []
            unavailable_permanent = []

            for file_name, _ in meta.get("resources", {}).items():
                ext = file_name.split(".")[-1].lower() if "." in file_name else None
                if accepted_types and (ext not in accepted_types and ext):
                    continue

                if is_completed(meta, file_name):
                    if is_completed(meta, file_name, complete=False, unavailable_permanent=True):
                        unavailable_permanent.append(file_name)
                    else:
                        success.append(file_name)
                else:
                    failed.append(file_name)

            if failed:
                failed_packages.add(identifier)

            total_failed.extend(failed)
            total_successful.extend(success)
            total_unavailable_permanent.extend(unavailable_permanent)

            packages_status[identifier] = {
                "failed_resources": failed,
                "successful_resources": success,
                "unavailable_permanent": unavailable_permanent
            }
        except Exception as e:
            logger("ERROR", f"Could not read {meta_path}", e)

    return packages_status, total_successful, total_failed, total_unavailable_permanent, failed_packages

def is_completed(package, file_name, complete=True, unavailable=False, unavailable_permanent=False):
    if file_name:
        try:
            info = package["crawlerInfo"]["resourcesInfo"].get(file_name, {})
            file_status = info.get("fileStatus", {})
            file_info = info.get("fileInfo", {})

            if complete and file_status.get("fileCompleted"):
                return True

            if unavailable and "resource_temporarily_unavailable" in file_info:
                return True

            if unavailable_permanent:
                if any(tag in file_info for tag in PERMANENT_UNAVAILABLE_TAGS):
                    return True

            return False
        except Exception as e:
            logger("WARNING", f"Missing or invalid status for '{file_name}': {e}")
            return False
    else:
        try:
            return bool(package["crawlerInfo"]["packageStatus"].get("packageCompleted", False))
        except Exception as e:
            logger("WARNING", f"Missing or invalid status for '{package.get('fileName')}': {e}")
            return False

# ==============================
# Metadata processing functions
# ==============================

# == Filename and hashing functions ==

def generate_short_filename(fname, ext=None):
    fname_hash = hashlib.sha1(fname.encode("utf-8")).hexdigest()
    return f"{fname_hash}.{ext}" if ext else fname_hash

# == Text cleaning and quality assurance functions ==

def clean_control_chars(text):
    return re.sub(r"[\u0080-\u009F]", "", text)

def is_bad_encoding(text):
    if is_bad(text) or "�" in text:
        return True
    return False

def check_encoding_quality(content):
    if is_bad_encoding(content):
        return None

    quality_flags = dict.fromkeys([
        "ñ", "Ñ", "á", "Á", "à", "À", "é", "É", "è", "È",
        "í", "Í", "ì", "Ì", "ó", "Ó", "ò", "Ò", "ú", "Ú",
        "ù", "Ù", "Ç", "ç", "¿", "¡", "?", "!", "º", "ª",
        "€", "ü", "Ü", "ï", "Ï", "'"
    ], False)

    for c in content:
        if c in quality_flags:
            quality_flags[c] = True

    return quality_flags

def fix_lines(decoded_text):
    lines = decoded_text.splitlines()
    fixed_lines = []
    fixed_count = 0
    unrecoverable_count = 0

    for line in lines:
        fixed_line_text, status = fix_line(line)
        fixed_lines.append(fixed_line_text)

        if status is True:
            fixed_count += 1
        elif status is None:
            unrecoverable_count += 1

        del fixed_line_text, status, line

    if fixed_count == 0 and unrecoverable_count == 0:
        del lines, fixed_lines
        gc.collect()
        return None, None, 0, 0

    result = "\n".join(fixed_lines)
    del lines, fixed_lines
    gc.collect()

    if unrecoverable_count > 0 and fixed_count > 0:
        return result, "partial_data_loss", fixed_count, unrecoverable_count
    if unrecoverable_count > 0:
        return result, "irreversible_data_loss", fixed_count, unrecoverable_count

    return result, "fixed_data", fixed_count, unrecoverable_count

def fix_line(line):
    if not is_bad_encoding(line):
        return line, False

    for fix_func in [fix_text, lambda x: " ".join(fix_text(w) for w in x.split()), clean_control_chars]:
        fixed = fix_func(line)
        if not is_bad_encoding(fixed):
            return fixed, True
    del fixed
    gc.collect()
    return line, None

def check_file_empty_or_strip(path, whitespace=b" \t\r\n"):
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            size = mm.size()
            if size == 0:
                return None, "no_data"

            start = 0
            while start < size and mm[start:start+1] in whitespace:
                start += 1

            end = size - 1
            while end >= 0 and mm[end:end+1] in whitespace:
                end -= 1

            if start > end:
                with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp_out:
                    return temp_out.name, "no_data"

            if start == 0 and end == size - 1:
                return None, None

            cleaned_content = mm[start:end+1]
            with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp_out:
                temp_out.write(cleaned_content)
                return temp_out.name, None
    except Exception as e:
        logger("ERROR", f"Error stripping whitespace from file '{path}'", e, indent=4)
        return None, None

def check_single_line(path, encoding):
    try:
        with open(path, "r", encoding=encoding) as f:
            line_count = 0
            for _ in f:
                line_count += 1
                if line_count > 1:
                    return False
            return True
    except Exception as e:
        logger("ERROR", f"Error checking if file '{path}' is one-line", e, indent=4)
        return False

def process_field(field):
    if field.startswith(QUOTE_CHAR) and not field.endswith(QUOTE_CHAR):
        field += QUOTE_CHAR
    elif field.endswith(QUOTE_CHAR) and not field.startswith(QUOTE_CHAR):
        field = QUOTE_CHAR + field

    inner = field[1:-1] if field.startswith(QUOTE_CHAR) and field.endswith(QUOTE_CHAR) and len(field) >= 2 else field
    inner = inner.replace("\r", " ").replace("\n", " ")

    res, i = [], 0
    while i < len(inner):
        ch = inner[i]
        if ch == QUOTE_CHAR:
            res.append(QUOTE_CHAR * 2)
            i += 2 if i + 1 < len(inner) and inner[i + 1] == QUOTE_CHAR else 1
        else:
            res.append(ch)
            i += 1
    return f'{QUOTE_CHAR}{"".join(res)}{QUOTE_CHAR}'

def quotes_balanced(line):
    in_quotes = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == QUOTE_CHAR:
            if in_quotes and i + 1 < n and line[i + 1] == QUOTE_CHAR:
                i += 2
                continue
            in_quotes = not in_quotes
        i += 1
    return not in_quotes

def fix_csv_line(line, delimiter):
    if not delimiter:
        return line

    if quotes_balanced(line):
        fields, field, in_quotes = [], "", False
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if ch == QUOTE_CHAR:
                if in_quotes and i + 1 < n and line[i + 1] == QUOTE_CHAR:
                    field += QUOTE_CHAR * 2
                    i += 2
                    continue
                in_quotes = not in_quotes
                field += ch
            elif ch == delimiter and not in_quotes:
                fields.append(field.strip())
                field = ""
            else:
                field += ch
            i += 1
        fields.append(field.strip())
    else:
        fields = [part.strip() for part in line.split(delimiter)]

    return delimiter.join(process_field(f) for f in fields)

def remove_outer_quotes(text, delimiter):
    cleaned = []
    for raw in text.splitlines():
        if delimiter is not None:
            cleaned.append(raw)
            continue

        s = raw.rstrip("\r")
        lead, trail, unwraps = len(s) - len(s.lstrip(QUOTE_CHAR)), len(s) - len(s.rstrip(QUOTE_CHAR)), 0
        while unwraps < min(lead, trail) and len(s) >= 2:
            s = s[1:-1]
            unwraps += 1
            if delimiter is not None:
                raw = s
                break
        cleaned.append(raw)
    return "\n".join(cleaned)

def count_inner_double_quotes(text):
    in_quotes = False
    count = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == QUOTE_CHAR:
            if in_quotes and i + 1 < n and text[i + 1] == QUOTE_CHAR:
                count += 1
                i += 2
                continue
            in_quotes = not in_quotes
        i += 1
    return count

def fix_csv_line_force_split(line, delimiter):
    if not delimiter:
        return process_field(line)
    fields = [part.strip() for part in line.split(delimiter)]
    return delimiter.join(process_field(f) for f in fields)

def fix_tabular_data(path, encoding):
    reconstructed_lines = 0
    outer_quotes_removed = False
    inner_quotes_fixed = 0
    file_changed = False

    delimiter, _ = detect_delimiter_consistent(path, encoding, max_lines=50)

    with open(path, "r", encoding=encoding) as f, tempfile.NamedTemporaryFile(mode="w", encoding=encoding, delete=False) as temp_out:
        buf = []
        in_quotes = False
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            i = 0
            n = len(line)

            while i < n:
                ch = line[i]
                if ch == QUOTE_CHAR:
                    if in_quotes and i + 1 < n and line[i + 1] == QUOTE_CHAR:
                        buf.append(QUOTE_CHAR * 2)
                        i += 2
                        continue
                    in_quotes = not in_quotes
                    buf.append(ch)
                else:
                    buf.append(ch)
                i += 1

            if in_quotes:
                reconstructed_lines += 1
                buf.append(" ")
                file_changed = True
                continue

            record = "".join(buf)
            buf = []
            in_quotes = False

            cleaned = remove_outer_quotes(record, delimiter)
            if cleaned != record:
                outer_quotes_removed = True
                file_changed = True

            before_inner = count_inner_double_quotes(record)
            before_quotes = record.count(QUOTE_CHAR)

            fixed = fix_csv_line(cleaned, delimiter)

            after_inner = count_inner_double_quotes(fixed)
            after_quotes = fixed.count(QUOTE_CHAR)

            if after_inner > before_inner:
                inner_quotes_fixed += 1
                file_changed = True

            if after_quotes > before_quotes and not (inner_quotes_fixed or outer_quotes_removed or reconstructed_lines):
                file_changed = True

            temp_out.write(fixed + "\n")

        if buf:
            record = "".join(buf)
            cleaned = remove_outer_quotes(record, delimiter)
            fixed = fix_csv_line(cleaned, delimiter)
            temp_out.write(fixed + "\n")

    if reconstructed_lines > 0:
        file_changed = True

    if not file_changed:
        try:
            os.remove(temp_out.name)
        except Exception:
            pass
        return None, 0, False, 0

    return temp_out.name, reconstructed_lines, outer_quotes_removed, inner_quotes_fixed, delimiter

# == Encoding functions ==

def detect_bom(raw):
    if raw.startswith(b'\xff\xfe\x00\x00'):
        return "utf-32-le"
    elif raw.startswith(b'\x00\x00\xfe\xff'):
        return "utf-32-be"
    elif raw.startswith(b'\xef\xbb\xbf'):
        return "utf-8-sig"
    elif raw.startswith(b'\xff\xfe'):
        return "utf-16-le"
    elif raw.startswith(b'\xfe\xff'):
        return "utf-16-be"
    return None

def get_bom_bytes(encoding_bom):
    if encoding_bom == "utf-32-le":
        return b"\xff\xfe\x00\x00"
    if encoding_bom == "utf-32-be":
        return b"\x00\x00\xfe\xff"
    if encoding_bom == "utf-16-le":
        return b"\xff\xfe"
    if encoding_bom == "utf-16-be":
        return b"\xfe\xff"
    return None

def detect_encoding_pattern(raw):
    encoding = chardet.detect(raw)['encoding']
    if encoding == "utf-16be":
        return "utf-16-be"
    if encoding == "utf-16le":
        return "utf-16-le"
    if encoding == "utf-32be":
        return "utf-32-be"
    if encoding == "utf-32le":
        return "utf-32-le"
    return None

def safe_decode(raw, encoding):
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as e:
        if "unexpected end of data" in str(e):
            for i in range(1, 5):
                try:
                    return raw[:-i].decode(encoding)
                except UnicodeDecodeError:
                    continue
        return None
    except Exception:
        return None

def stream_decode_to_tempfile(path, encoding, bom_offset=0, bom_bytes=None):
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmpfile:
                mm.seek(bom_offset)
                reader = codecs.getreader(encoding)(mm)

                if bom_bytes:
                    tmpfile.write(bom_bytes)

                prev_line = None
                for line in reader:
                    if prev_line is None:
                        prev_line = line.lstrip()
                    else:
                        tmpfile.write(prev_line.encode("utf-8"))
                        prev_line = line

                if prev_line is not None:
                    tmpfile.write(prev_line.rstrip().encode("utf-8"))

                return tmpfile.name
    except UnicodeDecodeError:
        return None

def stream_decode_to_tempfile_fixlines(path, encoding, bom_offset=0, bom_bytes=None):
    try:
        fixed_count = 0
        unrecoverable_count = 0
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmpfile:
                mm.seek(bom_offset)
                reader = codecs.getreader(encoding)(mm)

                if bom_bytes:
                    tmpfile.write(bom_bytes)

                prev_line = None
                for line in reader:
                    fixed_line, fixed = fix_line(line)
                    if fixed is True:
                        fixed_count += 1
                    elif fixed is None:
                        unrecoverable_count += 1

                    if prev_line is None:
                        prev_line = fixed_line.lstrip()
                    else:
                        tmpfile.write(prev_line.encode("utf-8"))
                        prev_line = fixed_line

                if prev_line is not None:
                    tmpfile.write(prev_line.rstrip().encode("utf-8"))

                tag = None
                if unrecoverable_count > 0 and fixed_count > 0:
                    tag = "partial_data_loss"
                elif unrecoverable_count > 0:
                    tag = "irreversible_data_loss"
                elif fixed_count > 0:
                    tag = "fixed_data"

                return tmpfile.name, tag, fixed_count, unrecoverable_count
    except UnicodeDecodeError:
        return None, None, 0, 0

def detect_best_encoding(path, encodings=ENCODING_CANDIDATES, num_bytes=64*1024):
    raw_flags = {
        "tags": [],
        "fixed_lines": 0,
        "unrecoverable_lines": 0
    }

    with open(path, "rb") as f:
        raw_sample = f.read(num_bytes)

    if not raw_sample:
        return "utf-8", None, None

    bom_encoding = detect_bom(raw_sample)
    bom_bytes = get_bom_bytes(bom_encoding)
    bom_offset = len(bom_bytes) if bom_bytes else 0

    raw_flags["tags"].append("normalized_utf8")

    # 1. Valid BOM decoding
    if bom_encoding:
        sample_content = safe_decode(raw_sample, bom_encoding)
        if sample_content:
            flags = check_encoding_quality(sample_content) or {}
            temp_path = stream_decode_to_tempfile(path, bom_encoding, bom_offset)
            if temp_path:
                del sample_content, flags
                gc.collect()
                return "utf-8", temp_path, raw_flags

        raw_flags["tags"].append("bom_invalid_or_unreliable")
        raw_sample = raw_sample[bom_offset:]

    # 2. Detect UTF-16/32 patterns
    pattern_encoding = detect_encoding_pattern(raw_sample)
    if pattern_encoding:
        bom_bytes = get_bom_bytes(pattern_encoding)
        sample_content = safe_decode(bom_bytes + raw_sample, pattern_encoding)
        if sample_content:
            flags = check_encoding_quality(sample_content) or {}
            temp_path = stream_decode_to_tempfile(path, pattern_encoding, bom_offset, bom_bytes)
            if temp_path:
                del sample_content, flags
                gc.collect()
                return "utf-8", temp_path, raw_flags

    # 3. Brute-force encodings
    results = {}
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            flags = check_encoding_quality(sample_content) or {}
            temp_path = stream_decode_to_tempfile(path, encoding, bom_offset)
            if temp_path:
                results[encoding] = {"flags": flags, "temp_path": temp_path}

    if results:
        _, best_result = max(results.items(), key=lambda item: sum(item[1]["flags"].values()))

        temp_path = best_result["temp_path"]
        delete_tempfiles([val["temp_path"] for val in results.values()], temp_path)
        del sample_content, results, best_result, flags
        gc.collect()
        return "utf-8", temp_path, raw_flags

    # 4. Brute-force encodings (+ fix)
    results = {}
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            fixed_content, _, _, _ = fix_lines(sample_content)
            if fixed_content:
                flags = check_encoding_quality(fixed_content) or {}
                temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(path, encoding, bom_offset)
                if temp_path:
                    results[encoding] = {"flags": flags, "temp_path": temp_path, "tag": tag, "fixed_lines": fixed_count, "unrecoverable_lines": unrecoverable_count}

    if results:
        _, best_result = max(results.items(), key=lambda item: sum(item[1]["flags"].values()))

        temp_path = best_result["temp_path"]
        delete_tempfiles([val["temp_path"] for val in results.values()], temp_path)

        raw_flags["tags"].append(best_result["tag"])
        raw_flags["fixed_lines"] = best_result["fixed_lines"]
        raw_flags["unrecoverable_lines"] = best_result["unrecoverable_lines"]
        del sample_content, results, best_result, flags
        gc.collect()
        return "utf-8", temp_path, raw_flags

    # 5. Last-resort brute-force (+ fix)
    for encoding in encodings:
        temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(path, encoding, bom_offset)
        if temp_path:
            logger("WARNING", f"Last-resort encoding recovery applied in file '{path}' using {encoding}")

            raw_flags["tags"].append(tag)
            raw_flags["fixed_lines"] = fixed_count
            raw_flags["unrecoverable_lines"] = unrecoverable_count
            del sample_content, results, best_result, flags
            gc.collect()
            return "utf-8", temp_path, raw_flags
        
    del sample_content, temp_path, results, best_result, flags
    gc.collect()
    return None, None, None

# == Delimiter detection functions ==

def detect_delimiter(text, return_count=False):
    counts, in_quotes, i, n = {}, False, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == QUOTE_CHAR:
            if in_quotes and i + 1 < n and text[i + 1] == QUOTE_CHAR:
                i += 2
                continue
            in_quotes = not in_quotes
            i += 1
            continue

        if not in_quotes and (
            not ch.isalnum()
            and (not ch.isspace() or ch == "\t")
            and ch not in NOT_ALLOWED_DELIMITERS
        ):
            counts[ch] = counts.get(ch, 0) + 1
        i += 1

    if not counts:
        return (None, 0) if return_count else None

    best = max(counts, key=counts.get)
    if return_count:
        return best, counts[best]
    else:
        return best

def detect_delimiter_consistent(path, encoding, max_lines=100, max_cv=0.6):
    lines = []
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        line_bytes = b""
        for byte in iter(lambda: mm.read(1), b""):
            line_bytes += byte
            if byte == b"\n":
                line = safe_decode(line_bytes, encoding).strip()
                if line:
                    lines.append(line)
                line_bytes = b""
                if len(lines) >= max_lines:
                    break
        if line_bytes and len(lines) < max_lines:
            line = safe_decode(line_bytes, encoding).strip()
            if line:
                lines.append(line)

    if not lines:
        return None, None

    delimiter_data = []
    for idx, line in enumerate(lines):
        delim, count = detect_delimiter(line, return_count=True)
        if delim:
            delimiter_data.append((idx, delim, count))

    if not delimiter_data:
        return None, None

    delim_groups = {}
    for idx, delim, count in delimiter_data:
        delim_groups.setdefault(delim, []).append((idx, count))

    candidates = []
    for delim, values in delim_groups.items():
        counts = [c for _, c in values]
        nonzero = [c for c in counts if c > 0]
        if not nonzero:
            continue

        freq_counter = Counter(nonzero)
        most_common_val, freq = freq_counter.most_common(1)[0]
        first_idx = next(idx for idx, c in values if c == most_common_val)

        if len(nonzero) > 1:
            mean = statistics.mean(nonzero)
            stdev = statistics.stdev(nonzero)
            cv = stdev / mean if mean != 0 else float("inf")
        else:
            cv = 0

        if cv > max_cv:
            continue

        candidates.append({
            "delim": delim,
            "freq": freq,
            "cv": cv,
            "first_idx": first_idx,
            "most_common_val": most_common_val,
        })

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: (-x["freq"], x["cv"], x["first_idx"]))
    best = candidates[0]
    return best["delim"], best["first_idx"]

# == MIME type functions ==

def resolve_mediatype_conflict(meta_mimetype_og, response, base_name):
    meta_mimetype, _ = get_mime_and_ext(meta_mimetype_og)
    detected_mime, detected_ext = get_resource_ext_info(response)
    meta_ext = get_extension_mime(meta_mimetype) if meta_mimetype else None

    tag_val = final_ext = final_mime = None
    if detected_mime and detected_mime not in GENERIC_MIME_TYPES:
        if meta_ext and detected_ext and meta_ext != detected_ext:
            tag_val = {
                "<mediaType_old>": meta_mimetype, "<fileName_old>": f"{base_name}.{meta_ext}",
                "<mediaType_new>": detected_mime, "<fileName_new>": f"{base_name}.{detected_ext}"
            }
        final_mime = detected_mime
        final_ext = detected_ext
    else:
        if meta_mimetype:
            final_mime = meta_mimetype
            final_ext = meta_ext
        else:
            if detected_mime == "application/force-download":
                final_mime = "text/plain"
                final_ext = "txt"
            else:
                final_mime = detected_mime
                final_ext = detected_ext

    final_file_name = f"{base_name}.{final_ext}" if final_ext else None

    return final_mime, final_file_name, tag_val

def get_mime_and_ext(pos_mime_value):
    if pos_mime_value:
        pos_mime_value = pos_mime_value.strip().lower()
        if "/" in pos_mime_value:
            return pos_mime_value, get_extension_mime(pos_mime_value)
        elif "_" in pos_mime_value:
            guessed_ext = pos_mime_value.split("_")[-1]
            return EXT_TO_MIME.get(guessed_ext), guessed_ext
        else:
            return EXT_TO_MIME.get(pos_mime_value), pos_mime_value

    return None, None

def get_resource_ext_info(response):
    if not response:
        return None, None

    content_type = response.headers.get("Content-Type", "")
    content_disposition = response.headers.get("Content-Disposition", "")

    mime_type = None
    ext = None
    if "filename=" in content_disposition:
        filename = content_disposition.split("filename=")[-1].strip('";').strip()
        if "." in filename:
            ext = filename.split(".")[-1].lower()
            mime_type = EXT_TO_MIME.get(ext)
            if mime_type:
                return mime_type, ext

    if content_type:
        raw_mime = content_type.split(";")[0].split(",")[0].strip('";').strip().lower()
        mime_type, ext = get_mime_and_ext(raw_mime)
        if not ext:
            logger("WARNING", f"No file extension found for MIME type: '{mime_type}'")

    return mime_type, ext

def get_ole_extension(path):
    with olefile.OleFileIO(path) as ole:
        streams = {s[0] for s in ole.listdir()}
        if "Workbook" in streams:
            return "xls"
        elif "WordDocument" in streams:
            return "doc"
        elif "PowerPoint Document" in streams:
            return "ppt"
        else:
            logger("WARNING", f"Unknown OLE type in file: {path}")
            return "ole"

def get_extension_mime(mime_type):
    try:
        ext = MIME_TYPE_MAP.get(mime_type.lower())
        if isinstance(ext, dict):
            ext = ext.get("extensions", [])[0]

        if ext:
            return ext
        elif "+" in mime_type:
            return mime_type.split("+")[-1]
        elif "/" in mime_type:
            suffix = mime_type.split("/", 1)[1]
            for key in MIME_TYPE_MAP.keys():
                if key.endswith("/" + suffix):
                    ext = MIME_TYPE_MAP[key]
                    if isinstance(ext, dict):
                        ext = ext.get("extensions", [])[0]
                    if ext:
                        return ext

        guessed_ext = mimetypes.guess_extension(mime_type.lower())
        if guessed_ext:
            return guessed_ext.lstrip(".")

        return None
    except Exception as e:
        logger("ERROR", f"Failed to get extension for MIME type: '{mime_type}'", e)
        return None

# == Data extraction functions ==

def extract_multilang_field(data, lang_field=None, value_field=None):
    result = {}
    if isinstance(data, list):
        for entry in data:
            lang = (entry.get(lang_field, "unknown") or "").strip().lower()
            value = (entry.get(value_field) or "").strip()
            if value:
                result.setdefault(lang, []).append(value)

    elif isinstance(data, dict):
        for lang, value in data.items():
            if isinstance(value, str) and value.strip():
                result[lang.strip().lower()] = [value.strip()]

    for lang in result:
        result[lang] = list(dict.fromkeys(result[lang]))

    return result

def extract_mapped_field(field_content, mapping):
    if not field_content:
        return []

    def fallback_uri(field):
        return field.strip().split("/")[-1].replace("-", " ").title()

    def apply_fallback_template(template, field):
        if isinstance(template, str):
            return template.replace("FALLBACK_VALUE", fallback_uri(field))
        elif isinstance(template, dict):
            return {
                k: apply_fallback_template(v, field)
                for k, v in template.items()
            }
        else:
            return template

    fields = [field_content] if isinstance(field_content, str) else field_content if isinstance(field_content, list) else []

    fallback_template = mapping.get("MAP_FALLBACK")
    result = []
    for field in fields:
        mapped = mapping.get(field)
        if mapped:
            result.append(mapped)
        elif fallback_template and is_url(field):
            result.append(apply_fallback_template(fallback_template, field))
        elif is_url(field):
            result.append(fallback_uri(field))
        else:
            result.append(field)

    return result

def extract_first_nonempty_value(field):
    if isinstance(field, dict):
        for value in field.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(field, list):
        for item in field:
            if isinstance(item, str) and item.strip():
                return item.strip()
    elif isinstance(field, str):
        return field.strip()
    return ""

def normalize_no_html_text(text):
    if not text or not isinstance(text, str):
        return text

    text = BeautifulSoup(text, "html.parser").get_text()
    text = " ".join(text.split())

    return text.strip()

# == crawlerChangesInfo functions ==

def init_metadata(package=True, crawled=True):
    if package:
        return {
            "crawlerInfo": {
                "packageInfo" : {},
                "packageStatus" : {
                    "packageCrawled": datetime.now().isoformat()
                },
                "resourcesInfo": {}
            }
        }
    else:
        base = {
            "fileMetadataChanges": {},
            "binaryFileChanges": {},
            "fileInfo": {},
            "fileStatus": {}
        }
        if crawled:
            base["fileStatus"]["fileCrawled"] = datetime.now().isoformat()
        return base

def add_tag_explanations(tags, data_source=None):
    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    elif isinstance(tags, str):
        tags = [tags]

    explanations = {}
    for tag in tags:
        tag_content = CRAWLER_CHANGES_INFO.get(tag)
        if not tag_content:
            explanations[tag] = {"reason": "(no explanation available)"}
            continue

        tag_explanation = json.dumps(tag_content.get("tag_explanation", {}))
        tag_placeholders = tag_content.get("tag_placeholders", [])
        for tag_placeholder in tag_placeholders:
            tag_explanation = tag_explanation.replace(
                tag_placeholder, str(data_source.get(tag_placeholder, f"<{tag_placeholder}>"))
            )

        explanations[tag] = json.loads(tag_explanation)

    return explanations

# ==============================
# Resource loading functions
# ==============================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def print_intro():
    with open(os.path.join(BASE_DIR, "resources", "intro.txt"), "r", encoding="utf-8") as f:
        print(f.read())

def load_resource(filename, fallback_value=None):
    with open(os.path.join(BASE_DIR, "resources", filename), "r", encoding="utf-8") as f:
        data = json.load(f)

    if fallback_value:
        data["MAP_FALLBACK"] = fallback_value

    return data

def load_tokens():
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, "config.ini"))

    return {
        section.lower(): config.get(section, "token", fallback=None) or None
        for section in config.sections()
    }

def build_extension_to_mime_map(mime_map):
    ext_to_mime = {}
    for mime, info in mime_map.items():
        if isinstance(info, dict):
            exts = info.get("extensions")
            if exts and len(exts) > 0:
                first_ext = exts[0].lower()
                if first_ext not in ext_to_mime:
                    ext_to_mime[first_ext] = mime
    return ext_to_mime

ZENODO_STATE_FILE = os.path.join(BASE_DIR, "resources", "zenodo_state.json")
AUTH_TOKENS = load_tokens()

USER_AGENTS = load_resource("user_agents.json")
MIME_TYPE_MAP = load_resource("mime_extensions.json")
EXT_TO_MIME = build_extension_to_mime_map(MIME_TYPE_MAP)

CRAWLER_CHANGES_INFO = {
    **load_resource("raw_changes_explanations.json"),
    **load_resource("file_info_explanations.json"),
    **load_resource("metadata_changes_explanations.json")
}

DATOSGOBESCRAWLER_THEME_MAP = load_resource(
    "datosgobes_theme_map.json",
    fallback_value="FALLBACK_VALUE"
)

DATOSGOBESCRAWLER_SPATIAL_MAP = load_resource(
    "datosgobes_spatial_map.json",
    fallback_value={"name": "FALLBACK_VALUE", "type": None}
)
CKANCRAWLER_SPATIAL_MAP = load_resource(
    "ckan_spatial_map.json",
    fallback_value={"name": "FALLBACK_VALUE", "type": None}
)

DATOSGOBESCRAWLER_PUBLISHER_MAP = load_resource(
   "datosgobes_publisher_map.json", 
   fallback_value={
       "identifier": {"value": "FALLBACK_VALUE", "scheme": None}, 
       "name": None
   }
)
