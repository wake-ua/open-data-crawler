import os
import json
import mimetypes
import hashlib
import chardet
import olefile
import re
import tempfile
import mmap
import codecs
import magic
import statistics
from collections import Counter
from ftfy.badness import is_bad
from ftfy import fix_text
from w3lib.url import url_query_cleaner
from url_normalize import url_normalize
import requests
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

ENCODING_CANDIDATES = ["utf-8", "iso-8859-1", "windows-1252", "windows-1250", "cp850"]

NOT_ALLOWED_DELIMITERS = [":", " ",'"', "'", "_", "(", ")", "<", ">", "[", "]", "{", "}", "-", ".", "+", "*", "=", "/", "\\", "�"]

RAW_SIGNATURES = [
    (b"\x37\x7A\xBC\xAF\x27\x1C", "application/x-7z-compressed"),
    (b"\x52\x61\x72\x21\x1A\x07", "application/x-rar-compressed"),
    (b"PK0", "application/zip-empty"),
    (b"<?xml", "text/xml"),
    (b"<!", "text/html"),
    (b"<!DOCTYPE html>", "text/html"),
    (b"<html", "text/html"),
]

MIME_EXTENSION_EQUIVALENTS = {
    "csv": {"text/csv", "text/plain", "text/tsv"},
    "tsv": {"text/csv", "text/plain", "text/tsv"},
}

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
            try:
                os.remove(path)
            except Exception:
                pass

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

def get_user_agent_list(current_agent):
    return [current_agent] + [ua for ua in USER_AGENTS if ua != current_agent] if current_agent else USER_AGENTS

def make_request(url, current_agent, headers=None, params=None, max_sec=None):
    headers = headers.copy() if headers else {}

    for user_agent in get_user_agent_list(current_agent):
        headers["User-Agent"] = user_agent

        response = requests.get(url, headers=headers, params=params, verify=False, timeout=max_sec)
        if response.status_code == 403:
            logger("NET", f"Forbidden with User-Agent '{user_agent}' (HTTP 403 - Forbidden), trying next one...")
            continue

        return response, user_agent

    return None, None

def clean_url(u):
    u = url_normalize(u)
    u = url_query_cleaner(u, remove=True, parameterlist=["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"])

    for prefix in ("http://", "https://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]

    return u.split("/")[0]

# ==============================
# Resume / recovery functions
# ==============================

def recover_resume(save_path, accepted_types=None):
    packages_status = {}
    total_failed = []
    total_successful = []
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
            for r in meta.get("resources", []):
                file_name = r.get("fileName", "")
                if accepted_types and file_name.split(".")[-1].lower() not in accepted_types:
                    continue

                if r.get("crawlerChangesInfo", {}).get("complete") is True:
                    success.append(file_name)
                else:
                    failed.append(file_name)

            if failed or meta.get("complete") is not True:
                failed_packages.add(identifier)

            total_failed.extend(failed)
            total_successful.extend(success)
            packages_status[identifier] = {
                "failed_resources": failed,
                "successful_resources": success
            }
        except Exception as e:
            logger("ERROR", f"Could not read {meta_path}", e)

    return packages_status, total_successful, total_failed, failed_packages

# ==============================
# Metadata processing functions
# ==============================

# == Filename and hashing functions ==

def generate_short_filename(fname, ext=None):
    fname_hash = hashlib.sha1(fname.encode("utf-8")).hexdigest()
    return f"{fname_hash}.{ext}" if ext else fname_hash

# Text cleaning and quality assurance functions

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

    if fixed_count == 0 and unrecoverable_count == 0:
        return None, None, 0, 0

    result = "\n".join(fixed_lines)
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
    return line, None

def are_quotes_balanced(line):
    i = 0
    in_quotes = False
    while i < len(line):
        if line[i] == '"':
            if i + 1 < len(line) and line[i + 1] == '"':
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
        else:
            i += 1

    return not in_quotes

def check_unbalanced_quotes(text):
    if '"' not in text:
        return text, 0

    for line in text.splitlines():
        if not are_quotes_balanced(line):
            lines, reconstructed_lines = reconstruct_lines(text)
            return "\n".join(lines), reconstructed_lines

    return text, 0

def reconstruct_lines(text, max_lines=None):
    lines = []
    partial_row = []
    reconstructed_lines = 0

    for line in text.splitlines():
        partial_row.append(line)
        joined_lines = "\n".join(partial_row)
        
        if are_quotes_balanced(joined_lines):
            reconstructed = "".join(partial_row)
            if len(partial_row) > 1:
                reconstructed_lines += 1
            lines.append(reconstructed)
            partial_row = []

        if max_lines and len(lines) >= max_lines:
            break

    if partial_row: 
        lines.append("".join(partial_row))

    return lines, reconstructed_lines

def strip_outer_quotes(text):
    lines = text.splitlines()
    cleaned_lines = []
    changed = False

    for line in lines:
        original = line.strip()

        start_quotes = len(re.match(r'^"+', original).group(0)) if re.match(r'^"+', original) else 0
        end_quotes = len(re.search(r'"+$', original).group(0)) if re.search(r'"+$', original) else 0

        if start_quotes >= 2 and end_quotes >= 2:
            inner = original[start_quotes:-end_quotes]
            cleaned_lines.append(inner.strip())
            changed = True
        else:
            cleaned_lines.append(original)

    return "\n".join(cleaned_lines), changed

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

def stream_decode_to_tempfile(raw_mm, encoding, bom_offset=0, bom_bytes=None):
    try:
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as tmpfile:
            raw_mm.seek(bom_offset)
            reader = codecs.getreader(encoding)(raw_mm)

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
    except Exception:
        return None

def stream_decode_to_tempfile_fixlines(raw_mm, encoding, bom_offset=0, bom_bytes=None):
    fixed_count = 0
    unrecoverable_count = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as tmpfile:
            raw_mm.seek(bom_offset)
            reader = codecs.getreader(encoding)(raw_mm)

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
    except Exception:
        return None, None, 0, 0

def detect_best_encoding(file_path, encodings=ENCODING_CANDIDATES, num_bytes=64*1024):
    raw_flags = {
        "tags": [],
        "fixed_lines": 0,
        "unrecoverable_lines": 0
    }

    with open(file_path, "rb") as f:
        raw_full_mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        raw_sample = raw_full_mm[:num_bytes]

    if not raw_sample:
        raw_flags["tags"].append("no_data")
        return None, None, None, True

    if not raw_sample.strip():
        raw_flags["tags"].append("no_valid_data")
        return None, "", raw_flags, True

    bom_encoding = detect_bom(raw_sample)
    bom_bytes = get_bom_bytes(bom_encoding)
    bom_offset = len(bom_bytes) if bom_bytes else 0

    raw_flags["tags"].append("normalized_utf8")

    # 1. Valid BOM decoding
    if bom_encoding:
        sample_content = safe_decode(raw_sample, bom_encoding)
        if sample_content and check_encoding_quality(sample_content):
            temp_path = stream_decode_to_tempfile(raw_full_mm, bom_encoding)
            if temp_path:
                return "utf-8", temp_path, raw_flags, False

        raw_flags["tags"].append("bom_invalid_or_unreliable")
        raw_sample = raw_sample[bom_offset:]

    # 2. Detect UTF-16/32 patterns
    pattern_encoding = detect_encoding_pattern(raw_sample)
    if pattern_encoding:
        bom_bytes = get_bom_bytes(pattern_encoding)
        sample_content = safe_decode(bom_bytes + raw_sample, pattern_encoding)
        if sample_content and check_encoding_quality(sample_content):
            temp_path = stream_decode_to_tempfile(raw_full_mm, pattern_encoding, bom_offset, bom_bytes)
            if temp_path:
                return "utf-8", temp_path, raw_flags, False

    # 3. Brute-force encodings
    results = {}
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            flags = check_encoding_quality(sample_content)
            if flags:
                temp_path = stream_decode_to_tempfile(raw_full_mm, encoding, bom_offset)
                if temp_path:
                    results[encoding] = {"flags": flags, "temp_path": temp_path}

    if results:
        _, best_result = max(results.items(), key=lambda item: sum(item[1]["flags"].values()))

        temp_path = best_result["temp_path"]
        delete_tempfiles([val["temp_path"] for val in results.values()], temp_path)

        return "utf-8", temp_path, raw_flags, False

    # 4. Brute-force encodings (+ fix)
    results = {}
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            fixed_content, _, _, _ = fix_lines(sample_content)
            if fixed_content:
                flags = check_encoding_quality(fixed_content)
                if flags:
                    temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(raw_full_mm, encoding, bom_offset)
                    if temp_path:
                        results[encoding] = {"flags": flags, "temp_path": temp_path, "tag": tag, "fixed_lines": fixed_count, "unrecoverable_lines": unrecoverable_count}

    if results:
        _, best_result = max(results.items(), key=lambda item: sum(item[1]["flags"].values()))

        temp_path = best_result["temp_path"]
        delete_tempfiles([val["temp_path"] for val in results.values()], temp_path)

        raw_flags["tags"].append(best_result["tag"])
        raw_flags["fixed_lines"] = best_result["fixed_lines"]
        raw_flags["unrecoverable_lines"] = best_result["unrecoverable_lines"]

        return "utf-8", best_result["temp_path"], raw_flags, False

    # 5. Last-resort brute-force (+ fix)
    for encoding in encodings:
        temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(raw_full_mm, encoding, bom_offset)
        if temp_path:
            logger("WARNING", f"Last-resort encoding recovery applied in file '{file_path}' using {encoding}")

            raw_flags["tags"].append(tag)
            raw_flags["fixed_lines"] = fixed_count
            raw_flags["unrecoverable_lines"] = unrecoverable_count

            return "utf-8", temp_path, raw_flags, False

    return None, None, None, False

# == Delimiter detection functions ==

def count_unquoted_delimiters(line, delim):
    if not are_quotes_balanced(line):
        return 0

    count = 0
    in_quotes = False
    i = 0
    while i < len(line):
        if line[i] == '"':
            if i + 1 < len(line) and line[i + 1] == '"':
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
        elif line[i] == delim and not in_quotes:
            count += 1
            i += 1
        else:
            i += 1
    return count

def detect_delimiter(text, max_lines=50, max_cv=0.6):
    lines = text.splitlines()[:max_lines]
    if not lines:
        return None, None

    delimiter_candidates = []

    all_chars = set(c for line in lines for c in line if not c.isalnum() and c not in NOT_ALLOWED_DELIMITERS)
    for delim in all_chars:
        counts = [count_unquoted_delimiters(line, delim) for line in lines]

        nonzero_counts = [c for c in counts if c > 0]
        if not nonzero_counts:
            continue

        freq_counter = Counter(nonzero_counts)
        most_common_val, freq = freq_counter.most_common(1)[0]

        try:
            first_idx = counts.index(most_common_val)
        except ValueError:
            continue

        if len(nonzero_counts) > 1:
            mean = statistics.mean(nonzero_counts)
            stdev = statistics.stdev(nonzero_counts)
            cv = stdev / mean if mean != 0 else float("inf")
        else:
            cv = 0

        if cv > max_cv:
            continue

        delimiter_candidates.append({
            "delim": delim,
            "freq": freq,
            "cv": cv,
            "first_idx": first_idx,
            "most_common_val": most_common_val,
        })

    if not delimiter_candidates:
        return None, None

    delimiter_candidates.sort(key=lambda x: (-x["freq"], x["cv"], x["first_idx"]))
    best = delimiter_candidates[0]

    return best["delim"], best["first_idx"]

# == MIME type functions ==

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
        if ext:
            return ext

        guessed_ext = mimetypes.guess_extension(mime_type.lower())
        if guessed_ext:
            return guessed_ext.lstrip(".")

        logger("WARNING", f"No file extension found for MIME type: '{mime_type}'")
        return None

    except Exception as e:
        logger("ERROR", f"Failed to get extension for MIME type: '{mime_type}'", e)
        return None

def get_mime_extension(ext):
    try:
        ext = ext.lower().lstrip(".")
        mime = EXTENSION_TYPE_MAP.get(ext)
        if mime:
            return mime

        guessed_mime = mimetypes.types_map.get(f".{ext}")
        if guessed_mime:
            return guessed_mime

        logger("WARNING", f"No MIME type found for extension: '{ext}'")
        return None

    except Exception as e:
        logger("ERROR", f"Failed to get MIME type for extension: '{ext}'", e)
        return None

def detect_mime_by_signature(raw):
    for sig, mime in RAW_SIGNATURES:
        if raw.strip().startswith(sig):
            return mime
    return None

def check_mimetype_mismatch(file_path, num_bytes=2048):
    file_extension = os.path.splitext(file_path)[1].lower().lstrip(".")
    with open(file_path, "rb") as f:
        raw_content = f.read(num_bytes)

    detected_mime = detect_mime_by_signature(raw_content)
    if not detected_mime:
        detected_mime_magic = magic.from_buffer(raw_content, mime=True)
        if detected_mime_magic in ["application/octet-stream", "text/html"]:
            detected_mime = "text/plain"
        else:
            detected_mime = detected_mime_magic

    no_data = False
    if detected_mime == "application/zip-empty":
        logger("WARNING", f"File '{file_path}' is an empty 'application/zip'")
        detected_mime = "application/zip"
        no_data = True
    elif detected_mime == "application/x-empty":
        logger("WARNING", f"File '{file_path}' is an empty file")
        detected_mime = "text/plain"
        no_data = True

    guessed_extension = None
    mismatch = False
    if detected_mime:
        if detected_mime == "application/x-ole-storage":
            guessed_extension = get_ole_extension(file_path)
        else:
            guessed_extension = get_extension_mime(detected_mime)

        if not guessed_extension:
            logger("WARNING", f"No extension guessed for MIME '{detected_mime}', defaulting to text/plain")
            detected_mime = "text/plain"
            guessed_extension = get_extension_mime(detected_mime)

        allowed_mimes = MIME_EXTENSION_EQUIVALENTS.get(file_extension)
        if allowed_mimes:
            mismatch = detected_mime.lower() not in allowed_mimes
        else:
            mismatch = guessed_extension != file_extension

    return mismatch, guessed_extension, detected_mime, no_data

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

# == crawlerChangesInfo functions ==

def fix_mime_mismatch(resource, guessed_extension, detected_mime):
    old_filename = resource.get("fileName")
    old_path = resource.get("path", "")
    old_mime = resource.get("mediaType")

    new_filename = old_filename.rsplit(".", 1)[0] + "." + guessed_extension
    new_path = old_path.replace(old_filename, new_filename)

    resource["crawlerChangesInfo"]["resourceMetadataChanges"].append({
        "reason": "MIME type mismatch",
        "fields": ["mediaType", "fileName", "path"],
        "oldValue": {
            "mediaType": old_mime,
            "fileName": old_filename,
            "path": old_path
        },
        "newValue": {
            "mediaType": detected_mime,
            "fileName": new_filename,
            "path": new_path
        }
    })

    resource["fileName"] = new_filename
    resource["path"] = new_path
    resource["mediaType"] = detected_mime

    dataset_dir = os.path.dirname(old_path)
    old_file_path = os.path.join(dataset_dir, old_filename)
    new_file_path = os.path.join(dataset_dir, new_filename)

    try:
        os.rename(old_file_path, new_file_path)
        logger("OK", f"Successfully renamed file '{old_filename}' to '{new_filename}'")
    except Exception as e:
        logger("ERROR", f"Failed to rename file '{old_filename}' to '{new_filename}': {e}")

def add_tag_explanations(resource, destination, tags, data_source=None):
    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    elif isinstance(tags, str):
        tags = [tags]

    explanations_dict = None
    if destination == "fileInfo":
        explanations_dict = FILE_INFO_EXPLANATIONS
    elif destination == "binaryFileChanges":
        explanations_dict = RAW_CHANGES_EXPLANATIONS

    explanations = []
    for tag in tags:
        spec = explanations_dict.get(tag)
        if not spec:
            explanations.append({tag: {"reason": "(no explanation available)"}})
            continue

        reason = spec["reason"]
        entry = {tag: {"reason": reason}}

        if "value" in spec:
            placeholder = spec["value"]
            key = placeholder.strip("<>").lower()
            val = data_source.get(key, 0) if isinstance(data_source, dict) else 0
            entry[tag]["reason"] = reason.replace(placeholder, str(val))
            entry[tag]["value"] = val

        elif "values" in spec:
            entry[tag]["values"] = {}
            for k, placeholder in spec["values"].items():
                val = data_source.get(k, 0) if isinstance(data_source, dict) else 0
                entry[tag]["reason"] = entry[tag]["reason"].replace(placeholder, str(val))
                entry[tag]["values"][k] = val

        explanations.append(entry)

    resource["crawlerChangesInfo"][destination].extend(explanations)

# ==============================
# Resource loading functions
# ==============================

def print_intro():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "intro.txt"), "r", encoding="utf-8") as f:
        print(f.read())

def load_resource(filename, fallback_value=None):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", filename), "r", encoding="utf-8") as f:
        data = json.load(f)

    if fallback_value:
        data["MAP_FALLBACK"] = fallback_value

    return data

USER_AGENTS = load_resource("user_agents.json")
MIME_TYPE_MAP = load_resource("mime_type_map.json")
EXTENSION_TYPE_MAP ={v: k for k, v in MIME_TYPE_MAP.items()}

RAW_CHANGES_EXPLANATIONS = load_resource("raw_changes_explanations.json")
FILE_INFO_EXPLANATIONS = load_resource("file_info_explanations.json")

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
