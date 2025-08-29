import os
import json
import mimetypes
import hashlib
import chardet
import olefile
import re
import magic
from collections import Counter
from ftfy.badness import is_bad
from ftfy import fix_text
from w3lib.url import url_query_cleaner
from url_normalize import url_normalize
import requests
from opendatacrawler.setup_logger import logger

ENCODING_CANDIDATES = ["utf-8", "iso-8859-1", "windows-1252", "windows-1250", "cp850"]

RAW_SIGNATURES = [
    (b"\x37\x7A\xBC\xAF\x27\x1C", "application/x-7z-compressed"),
    (b"\x52\x61\x72\x21\x1A\x07", "application/x-rar-compressed"),
    (b"PK0", "application/zip-empty"),
    (b"<?xml", "text/xml"),
    (b"<!", "text/html"),
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
        logger("...", f"The directory '{path}' already exists, skipping creation")
        return True

    try:
        os.makedirs(path, exist_ok=False)
        logger("OK", f"Successfully created the directory '{path}'")
        return True
    except OSError as e:
        logger("ERROR", f"Failed to create the directory '{path}'", e)
        return False

# ==============================
# Request functions
# ==============================

def get_user_agent_list(current_agent):
    return [current_agent] + [ua for ua in USER_AGENTS if ua != current_agent] if current_agent else USER_AGENTS

def make_request(url, current_agent, headers=None, params=None):
    headers = headers.copy() if headers else {}

    for user_agent in get_user_agent_list(current_agent):
        headers["User-Agent"] = user_agent

        response = requests.get(url, headers=headers, params=params, verify=False)

        if response.status_code == 403:
            logger("WARNING", f"403 Forbidden with User-Agent: {user_agent}, trying next one")
            continue

        return response, user_agent

    return None, None

def check_url(url):
    return url.startswith("http://") or url.startswith("https://")

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

            if failed:
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
        if not is_bad_encoding(line):
            fixed_lines.append(line)
            continue

        for fix_func in [fix_text, lambda x: " ".join(fix_text(w) for w in x.split()), clean_control_chars]:
            fixed = fix_func(line)
            if not is_bad_encoding(fixed):
                fixed_lines.append(fixed)
                fixed_count += 1
                break
        else:
            fixed_lines.append(line)
            unrecoverable_count += 1

    if fixed_count == 0 and unrecoverable_count == 0:
        return None, None, 0, 0

    result = "\n".join(fixed_lines)
    if unrecoverable_count > 0 and fixed_count > 0:
        return result, "partial_data_loss", fixed_count, unrecoverable_count
    if unrecoverable_count > 0:
        return result, "irreversible_data_loss", fixed_count, unrecoverable_count
    return result, "fixed_data", fixed_count, unrecoverable_count

def needs_strip(raw_full):
    return raw_full != raw_full.strip()

def reconstruct_lines(text, max_lines=None):
    lines = []
    partial_row = []
    reconstructed_lines = 0

    for line in text.splitlines():
        partial_row.append(line)
        joined_lines = "\n".join(partial_row)

        quote_count = 0
        i = 0
        while i < len(joined_lines):
            if joined_lines[i] == '"':
                if i + 1 < len(joined_lines) and joined_lines[i + 1] == '"':
                    i += 1
                else:
                    quote_count += 1
            i += 1

        if quote_count % 2 == 0:
            reconstructed = " ".join(partial_row)
            if len(partial_row) > 1:
                reconstructed_lines += 1
            lines.append(reconstructed)
            partial_row = []

        if max_lines and len(lines) >= max_lines:
            break

    return lines, reconstructed_lines

def check_unbalanced_quotes(text):
    for line in text.splitlines():
        if line.count('"') % 2 != 0:
            lines, reconstructed_lines = reconstruct_lines(text)
            return "\n".join(lines), reconstructed_lines

    return text, 0

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

def remove_bom(raw, encoding_bom):
    if encoding_bom in ["utf-32-le", "utf-32-be"]:
        return raw[4:]
    if encoding_bom == "utf-8-sig":
        return raw[3:]
    if encoding_bom in ["utf-16-le", "utf-16-be"]:
        return raw[2:]
    return raw

def add_bom(raw, encoding_bom):
    if encoding_bom == "utf-32-le":
        return b"\xff\xfe\x00\x00" + raw
    if encoding_bom == "utf-32-be":
        return b"\x00\x00\xfe\xff" + raw
    if encoding_bom == "utf-16-le":
        return b"\xff\xfe" + raw
    if encoding_bom == "utf-16-be":
        return b"\xfe\xff" + raw
    return raw

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

def detect_best_encoding(file_path, encodings=ENCODING_CANDIDATES, num_bytes=64*1024):
    raw_flags = {
        "tags": [],
        "fixed_lines": 0,
        "unrecoverable_lines": 0
    }

    with open(file_path, "rb") as f:
        raw_full = f.read()
    raw_sample = raw_full[:num_bytes]

    if not raw_sample:
        raw_flags["tags"].append("no_data")
        return None, None, None, True

    if not raw_sample.strip():
        raw_flags["tags"].append("no_valid_data")
        return None, "", raw_flags, True

    if needs_strip(raw_full):
        raw_flags["tags"].append("raw_strip")

    bom_encoding = detect_bom(raw_sample)

    # 1. Valid BOM decoding
    if bom_encoding:
        content = safe_decode(raw_sample, bom_encoding)
        if content and check_encoding_quality(content):
            full_content = safe_decode(raw_full, bom_encoding)
            if full_content and full_content.strip():
                raw_flags["tags"].append("normalized_utf8")
                return "utf-8", full_content.encode("utf-8").strip(), raw_flags, False

        raw_flags["tags"].append("bom_invalid_or_unreliable")
        raw_sample = remove_bom(raw_sample, bom_encoding)
        raw_full = remove_bom(raw_full, bom_encoding)

    # 2. Detect UTF-16/32 patterns
    pattern_encoding = detect_encoding_pattern(raw_sample)
    if pattern_encoding:
        content = safe_decode(add_bom(raw_sample, pattern_encoding), pattern_encoding)
        if content and check_encoding_quality(content):
            full_content = safe_decode(add_bom(raw_full, pattern_encoding), pattern_encoding)
            if full_content and full_content.strip():
                raw_flags["tags"].append("normalized_utf8")
                return "utf-8", full_content.encode("utf-8").strip(), raw_flags, False

    # 3. Brute-force encodings
    results = {}
    for encoding in encodings:
        content = safe_decode(raw_sample, encoding)
        if content and content.strip():
            flags = check_encoding_quality(content)
            if flags:
                full_content = safe_decode(raw_full, encoding)
                if full_content and full_content.strip():
                    results[encoding] = flags

    if results:
        best_encoding = max(results.items(), key=lambda item: sum(item[1].values()))[0]
        content = safe_decode(raw_full, best_encoding)
        if content and content.strip():
            if "raw_strip" in raw_flags["tags"]:
                return best_encoding, raw_full.strip(), raw_flags, False
            return best_encoding, None, raw_flags, False

    # 4. Brute-force encodings (+ fix)
    results = {}
    for encoding in encodings:
        content = safe_decode(raw_sample, encoding)
        if content and content.strip():
            fixed_content, _, _, _ = fix_lines(content)
            if fixed_content:
                flags = check_encoding_quality(fixed_content)
                if flags:
                    results[encoding] = flags

    if results:
        best_encoding = max(results.items(), key=lambda item: sum(item[1].values()))[0]
        content = safe_decode(raw_full, best_encoding)
        if content and content.strip():
            fixed_content, tag, fixed_lines, unrecoverable_lines = fix_lines(content)
            if fixed_content:
                raw_flags["tags"].append(tag)
                raw_flags["tags"].append("normalized_utf8")
                raw_flags["fixed_lines"] = fixed_lines
                raw_flags["unrecoverable_lines"] = unrecoverable_lines
                return "utf-8", fixed_content.encode("utf-8").strip(), raw_flags, False

    # 5. Fallback to BOM encoding if all else failed (+ fix)
    if bom_encoding:
        content = safe_decode(raw_full, bom_encoding)
        if content and content.strip():
            fixed_content, tag, fixed_lines, unrecoverable_lines = fix_lines(content)
            if fixed_content:
                raw_flags["tags"].append(tag)
                raw_flags["tags"].append("normalized_utf8")
                raw_flags["fixed_lines"] = fixed_lines
                raw_flags["unrecoverable_lines"] = unrecoverable_lines
                return "utf-8", fixed_content.encode("utf-8").strip(), raw_flags, False

    # 6. Last-resort brute-force (+ fix)
    for encoding in encodings:
        content = safe_decode(raw_full, encoding)
        if content and content.strip():
            fixed_content, tag, fixed_lines, unrecoverable_lines = fix_lines(content)
            if fixed_content:
                raw_flags["tags"].append(tag)
                raw_flags["tags"].append("normalized_utf8")
                raw_flags["fixed_lines"] = fixed_lines
                raw_flags["unrecoverable_lines"] = unrecoverable_lines
                fixed_content = clean_control_chars(fixed_content)
                return "utf-8", fixed_content.encode("utf-8").strip(), raw_flags, False

    return None, None, raw_flags, False

# == Delimiter detection functions ==

def count_unquoted_delimiters(line, delim):
    count = 0
    in_quotes = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == '"':
            if i + 1 < len(line) and line[i + 1] == '"':
                i += 1
            else:
                in_quotes = not in_quotes
        elif char == delim and not in_quotes:
            count += 1
        i += 1
    return count

NOT_ALLOWED_DELIMITERS = ['"', "'", "_", "(", ")", "<", ">", "[", "]", "{", "}", "-", ".", "+", "*", "=", "/", "\\", "&"]

def detect_delimiter(text, max_lines=50, min_consistent_lines=5):
    lines = text.splitlines()[:max_lines]
    if not lines:
        return None, None

    delimiter_scores = []

    all_chars = set(c for line in lines for c in line if not c.isalnum() and c not in NOT_ALLOWED_DELIMITERS)
    for delim in all_chars:
        counts = [count_unquoted_delimiters(line, delim) for line in lines]
        nonzero_counts = [c for c in counts if c > 0]

        if not nonzero_counts:
            continue

        most_common_val, freq = Counter(nonzero_counts).most_common(1)[0]

        min_required = min(min_consistent_lines, len(lines))
        if freq < min_required:
            continue

        try:
            first_idx = counts.index(most_common_val)
        except ValueError:
            continue

        score = freq * (most_common_val ** 0.5)
        delimiter_scores.append((score, delim, first_idx))

    if not delimiter_scores:
        return None, None

    best = max(delimiter_scores, key=lambda x: (x[0], -x[2]))
    return best[1], best[2]

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

def detect_mime_by_signature(raw):
    for sig, mime in RAW_SIGNATURES:
        if raw.startswith(sig):
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

def extract_multilang_field(data_list, lang_field, value_field):
    result = {}
    for entry in data_list:
        lang = (entry.get(lang_field, "unknown") or "").strip().lower()
        value = (entry.get(value_field) or "").strip()

        if value:
            result.setdefault(lang, []).append(value)

    for lang in result:
        result[lang] = list(dict.fromkeys(result[lang]))

    return result

def extract_uris_field(field_content, mapping):
    if not field_content:
        return []

    def fallback(uri):
        return uri.strip().split("/")[-1].replace("-", " ").title()

    def apply_fallback_template(template, uri):
        if isinstance(template, str):
            return template.replace("FALLBACK_VALUE", fallback(uri))
        elif isinstance(template, dict):
            return {
                k: apply_fallback_template(v, uri)
                for k, v in template.items()
            }
        else:
            return template

    uris = [field_content] if isinstance(field_content, str) else field_content if isinstance(field_content, list) else []

    fallback_template = mapping.get("MAP_FALLBACK")
    result = []
    for uri in uris:
        mapped = mapping.get(uri)
        if mapped:
            result.append(mapped)
        elif fallback_template:
            result.append(apply_fallback_template(fallback_template, uri))
        else:
            result.append(fallback(uri))

    return result

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

DATOSGOBESCRAWLER_PUBLISHER_MAP = load_resource(
   "datosgobes_publisher_map.json", 
   fallback_value={
       "identifier": {"value": "FALLBACK_VALUE", "scheme": None}, 
       "name": None
   }
)
