import codecs
import csv
from email.message import Message
from urllib.parse import urlparse, unquote
import mimetypes
import mmap
import os
import random
import re
import statistics
import tempfile
import shutil
import traceback
from collections import Counter

import chardet
import olefile
from ftfy import fix_text
from ftfy.badness import is_bad
from frictionless import Dialect, describe

from opendatacrawler.setup_logger import log_manager

from .filesystem import ATOMIC_TEMP_PREFIX, TABULAR_TEMP_PREFIX, delete_tempfiles
from .resources import EXT_TO_MIME, MIME_TYPE_MAP
from .metadata import add_tag_explanations

logger = log_manager.log

ENCODING_CANDIDATES = ["utf-8", "iso-8859-1", "windows-1252", "windows-1250", "cp850"]
NOT_ALLOWED_DELIMITERS = [":", " ", '"', "'", "_", "(", ")", "<", ">", "[", "]", "{", "}", "-", ".", "+", "*", "=", "/", "\\", "�", "@", "”", "#", "“", "¿", "?"]
QUOTE_CHAR = '"'
RAW_SIGNATURES = [
    (b"\x37\x7A\xBC\xAF\x27\x1C", "application/x-7z-compressed"),
    (b"\x52\x61\x72\x21\x1A\x07", "application/x-rar-compressed"),
    (b"PK0", "application/zip-empty"),
    (b"<?xml", "application/xml"),
    (b"<!DOCTYPE html>", "text/html"),
    (b"<html", "text/html"),
]
GENERIC_MIME_TYPES = {"application/octet-stream", "text/plain", "application/force-download"}

def process_tabular(crawler, path, resource, info, log_indent=4):
    temp_path, tag = check_file_empty_or_strip(path)
    if temp_path:
        shutil.move(temp_path, path)
        logger("FIX", f"File '{path}' content stripped and overwritten", indent=log_indent)
        info["binaryFileChanges"].update(add_tag_explanations("stripped_data"))

    if tag:
        logger("WARNING", f"File '{path}' has no data or no valid content", indent=log_indent)
        info["fileInfo"].update(add_tag_explanations(tag))
        return False

    try:
        encoding, temp_path, raw_flags = detect_best_encoding(path)
        if encoding:
            resource["encoding"] = encoding
        else:
            logger("ERROR", f"No matching encoding found for: {path}", indent=log_indent)
            return False

        if temp_path:
            shutil.move(temp_path, path)
            logger("FIX", f"Overwrote cleaned content into '{path}'", indent=log_indent)
        if raw_flags:
            info["binaryFileChanges"].update(add_tag_explanations(raw_flags, raw_flags))
    except Exception as error:
        logger("ERROR", f"Error while detecting encoding for: {path}", [error, traceback.format_exc()], indent=log_indent)
        return False

    raw_signature = detect_raw_signature(path)
    if raw_signature == "text/html":
        logger("WARNING", f"Normalized file '{path}' is actually an HTML page, not the expected downloadable dataset", indent=log_indent)
        info["fileInfo"].update(add_tag_explanations("html_page_downloaded"))
        return "completed_without_file"

    try:
        max_fix_attempts = 2
        delimiter_fix = None
        for attempt in range(max_fix_attempts):
            temp_path, reconstructed_rows, outer_quotes_removed, inner_quotes_fixed, delimiter_fix = fix_tabular_data(path, resource["encoding"])

            if not temp_path:
                break

            shutil.move(temp_path, path)
            if attempt == 0:
                logger("FIX", f"Cleaned and standardized the tabular file '{path}'", indent=log_indent)
                info["binaryFileChanges"].update(add_tag_explanations("standardized_field_quotes"))
            else:
                logger("FIX", f"Reprocessing tabular file '{path}' after previous structural fixes", indent=log_indent)

            if reconstructed_rows:
                logger("FIX", f"Reconstructed {reconstructed_rows} multiline rows in tabular file '{path}' by merging quoted fields split across rows", indent=log_indent)
                info["binaryFileChanges"].update(add_tag_explanations("reconstructed_rows", {"<reconstructed_rows>": reconstructed_rows}))
            if outer_quotes_removed:
                logger("FIX", f"Removed unnecessary outer quotes wrapping entire rows in tabular file '{path}'", indent=log_indent)
                info["binaryFileChanges"].update(add_tag_explanations("stripped_outer_quotes"))
            if inner_quotes_fixed:
                logger("FIX", f"Fixed {inner_quotes_fixed} rows with malformed inner quotes in tabular file '{path}'", indent=log_indent + 2)
                info["binaryFileChanges"].update(add_tag_explanations("fixed_inner_quotes", {"<fixed_inner_quotes>": inner_quotes_fixed}))

            if delimiter_fix is None and (reconstructed_rows or outer_quotes_removed or inner_quotes_fixed):
                logger("FIX", f"Retrying delimiter detection on normalized file '{path}'", indent=log_indent + 2)
                continue
            break
    except Exception as error:
        logger("ERROR", f"Failed to clean tabular file '{path}'", [error, traceback.format_exc()], indent=log_indent)
        return False

    try:
        delimiter, start_row, delimiter_stats = detect_delimiter_consistent(
            path,
            resource["encoding"],
            return_stats=True,
        )
        if delimiter_stats.get("nonempty_lines", 0) <= 1:
            logger("WARNING", f"Tabular file '{path}' appears to contain only one line", indent=log_indent)
            info["binaryFileChanges"].update(add_tag_explanations("one_line"))

        if not delimiter:
            info["fileInfo"].update(add_tag_explanations("no_delimiter_detected"))
            logger("WARNING", f"File '{path}' appears to not contain a delimiter, likely not a structured/tabular file", indent=log_indent)
            return True

        if delimiter_fix and delimiter_fix != delimiter:
            logger("ERROR", f"Delimiter mismatch in '{path}', expected '{delimiter_fix}', detected '{delimiter}'", indent=log_indent)

        validation = validate_tabular_rows(path, resource["encoding"], delimiter, start_row=start_row or 0)
        if validation["inconsistent_rows"]:
            details = ", ".join(f"row {item['row']} has {item['columns']} columns" for item in validation["inconsistent_rows"][:3])
            logger("WARNING", f"Tabular file '{path}' still has inconsistent row widths after processing: expected {validation['expected_columns']} columns, {details}", indent=log_indent)
            info["fileInfo"].update(add_tag_explanations("inconsistent_tabular_rows", {
                "<expected_columns>": validation["expected_columns"],
                "<checked_rows>": validation["checked_rows"],
                "<inconsistent_rows>": len(validation["inconsistent_rows"]),
            }))
            return False

        resource["delimiter"] = delimiter
        dialect = Dialect.from_descriptor({"delimiter": delimiter, **({"comment_rows": [start_row]} if start_row else {})})

        if crawler.extract_schema:
            try:
                resource_metadata = describe(path, encoding=resource["encoding"], dialect=dialect).to_dict()
                resource["schema"] = resource_metadata.get("schema")
                logger("OK", f"Schema extracted from '{path}' (encoding: '{resource['encoding']}', delimiter: '{delimiter}', start_row: {start_row})", indent=log_indent)
            except Exception as error:
                logger("ERROR", f"Failed to extract schema from file '{path}'", [error, traceback.format_exc()], indent=log_indent)
                return False
        else:
            resource.pop("schema", None)

        if crawler.partial_dataset_rows is not None:
            try:
                sample_mode = str(crawler.partial_dataset_sample_mode or "first").strip().lower()
                if sample_mode not in {"first", "random"}:
                    sample_mode = "first"
                sample_seed = crawler.partial_dataset_random_seed if sample_mode == "random" else None
                temp_path, kept_rows, total_rows = trim_tabular_data_rows(
                    path,
                    resource["encoding"],
                    crawler.partial_dataset_rows,
                    start_row=start_row or 0,
                    sample_mode=sample_mode,
                    sample_seed=sample_seed,
                )
                if temp_path:
                    shutil.move(temp_path, path)
                    removed_rows = total_rows - kept_rows
                    sampling_details = f"a reproducible random sample (seed: {sample_seed})" if sample_mode == "random" and sample_seed is not None else ("a random sample" if sample_mode == "random" else "the first rows")
                    logger("FIX", f"Stored partial tabular dataset '{path}' after full download and post-processing: kept {kept_rows} data rows out of {total_rows} total data rows using {sampling_details}", indent=log_indent)
                    info["binaryFileChanges"].update(add_tag_explanations("partial_dataset_trimmed", {
                        "<kept_rows>": kept_rows,
                        "<total_rows>": total_rows,
                        "<removed_rows>": removed_rows,
                        "<sampling_details>": sampling_details,
                    }))
            except Exception as error:
                logger("ERROR", f"Failed to trim tabular file '{path}' to {crawler.partial_dataset_rows} data rows", [error, traceback.format_exc()], indent=log_indent)
                return False
    except Exception as error:
        logger("ERROR", f"Failed to detect delimiter for file '{path}'", [error, traceback.format_exc()], indent=log_indent)
        return False

    return True

def detect_raw_signature(path, max_len=None):
    try:
        signature_len = max_len or max(len(sig) for sig, _ in RAW_SIGNATURES)
        with open(path, "rb") as f:
            head = f.read(max(512, signature_len))

        head_lower = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        for signature, mime_type in RAW_SIGNATURES:
            sig = signature.lower()
            if head_lower.startswith(sig):
                return mime_type
    except Exception:
        return None
    return None

def clean_control_chars(text):
    return re.sub(r"[\u0080-\u009F]", "", text)

def is_bad_encoding(text):
    return bool(is_bad(text) or "�" in text)

def check_encoding_quality(content):
    if is_bad_encoding(content):
        return None

    quality_flags = dict.fromkeys([
        "ñ", "Ñ", "á", "Á", "à", "À", "é", "É", "è", "È",
        "í", "Í", "ì", "Ì", "ó", "Ó", "ò", "Ò", "ú", "Ú",
        "ù", "Ù", "Ç", "ç", "¿", "¡", "?", "!", "º", "ª",
        "€", "ü", "Ü", "ï", "Ï", "'",
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

def check_file_empty_or_strip(path, whitespace=b" \t\r\n"):
    temp_path = None
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
                return None, "no_data"

            if start == 0 and end == size - 1:
                return None, None

            cleaned_content = mm[start:end+1]
            with tempfile.NamedTemporaryFile(mode="wb", delete=False, prefix=TABULAR_TEMP_PREFIX) as temp_out:
                temp_out.write(cleaned_content)
                temp_path = temp_out.name
                return temp_path, None
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        logger("ERROR", f"Error stripping whitespace from file '{path}'", e, indent=4)
        return None, None

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

def needs_field_normalization(field, delimiter):
    if field.startswith(QUOTE_CHAR) != field.endswith(QUOTE_CHAR):
        return True
    if "\r" in field or "\n" in field:
        return True
    if delimiter and delimiter in field and not (field.startswith(QUOTE_CHAR) and field.endswith(QUOTE_CHAR)):
        return True
    inner = field[1:-1] if field.startswith(QUOTE_CHAR) and field.endswith(QUOTE_CHAR) and len(field) >= 2 else field
    i = 0
    while i < len(inner):
        if inner[i] == QUOTE_CHAR:
            if i + 1 < len(inner) and inner[i + 1] == QUOTE_CHAR:
                i += 2
                continue
            return True
        i += 1
    return False

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
        fields.append(field)
    else:
        fields = line.split(delimiter)

    normalized_fields = []
    for field in fields:
        normalized_fields.append(process_field(field) if needs_field_normalization(field, delimiter) else field)

    return delimiter.join(normalized_fields)

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
        cleaned.append(s)
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
    fields = line.split(delimiter)
    return delimiter.join(process_field(f) for f in fields)

def validate_tabular_rows(path, encoding, delimiter, start_row=0, max_rows=10000):
    expected_columns = None
    checked_rows = 0
    inconsistent_rows = []
    with open(path, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter, strict=True)
        for row_number, row in enumerate(reader, start=1):
            if row_number <= start_row:
                continue
            if not row or not any(str(value).strip() for value in row):
                continue
            checked_rows += 1
            columns = len(row)
            if expected_columns is None:
                expected_columns = columns
            elif columns != expected_columns:
                inconsistent_rows.append({"row": row_number, "columns": columns})
                if len(inconsistent_rows) >= 10:
                    break
            if checked_rows >= max_rows:
                break
    return {
        "checked_rows": checked_rows,
        "expected_columns": expected_columns,
        "inconsistent_rows": inconsistent_rows,
    }

def fix_tabular_data(path, encoding):
    reconstructed_lines = 0
    outer_quotes_removed = False
    inner_quotes_fixed = 0
    file_changed = False
    temp_path = None

    delimiter, _ = detect_delimiter_consistent(path, encoding)

    try:
        with open(path, "r", encoding=encoding) as f, tempfile.NamedTemporaryFile(mode="w", encoding=encoding, delete=False, prefix=TABULAR_TEMP_PREFIX) as temp_out:
            temp_path = temp_out.name
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
    except Exception:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise

    if reconstructed_lines > 0:
        file_changed = True

    if not file_changed:
        try:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return None, 0, False, 0, delimiter

    return temp_path, reconstructed_lines, outer_quotes_removed, inner_quotes_fixed, delimiter

def trim_tabular_data_rows(path, encoding, max_data_rows, start_row=0, sample_mode="first", sample_seed=None):
    if max_data_rows is None or max_data_rows < 0:
        return None, 0, 0

    temp_path = None
    try:
        sample_mode = str(sample_mode or "first").strip().lower()
        header_lines = []
        kept_lines = []
        reservoir = []
        rng = random.Random(sample_seed) if sample_mode == "random" else None
        total_data_rows = 0
        nonempty_before_header = 0
        header_found = False

        with open(path, "r", encoding=encoding, newline="") as f:
            for line in f:
                if not header_found:
                    header_lines.append(line)
                    if not line.strip():
                        continue

                    if nonempty_before_header == start_row:
                        header_found = True
                        continue

                    nonempty_before_header += 1
                    continue

                if not line.strip():
                    continue

                row_index = total_data_rows
                total_data_rows += 1

                if sample_mode == "random":
                    if len(reservoir) < max_data_rows:
                        reservoir.append((row_index, line))
                    else:
                        replace_at = rng.randint(0, row_index)
                        if replace_at < max_data_rows:
                            reservoir[replace_at] = (row_index, line)
                elif len(kept_lines) < max_data_rows:
                    kept_lines.append(line)

        if not header_found:
            return None, 0, 0

        if sample_mode == "random":
            if max_data_rows >= total_data_rows:
                return None, total_data_rows, total_data_rows
            selected_lines = [line for _, line in sorted(reservoir, key=lambda item: item[0])]
        else:
            if max_data_rows >= total_data_rows:
                return None, total_data_rows, total_data_rows
            selected_lines = kept_lines

        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", delete=False, prefix=TABULAR_TEMP_PREFIX) as temp_out:
            temp_path = temp_out.name
            temp_out.writelines(header_lines)
            temp_out.writelines(selected_lines)

        return temp_path, len(selected_lines), total_data_rows
    except Exception:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise

def detect_bom(raw):
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    elif raw.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    elif raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    elif raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    elif raw.startswith(b"\xfe\xff"):
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
    encoding = chardet.detect(raw)["encoding"]
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
    temp_path = None
    try:
        with open(path, "rb") as f, tempfile.NamedTemporaryFile(mode="wb", delete=False, prefix=TABULAR_TEMP_PREFIX) as tmpfile:
            temp_path = tmpfile.name
            if bom_offset:
                f.seek(bom_offset)

            reader = codecs.getreader(encoding)(f)
            for line in reader:
                tmpfile.write(line.encode("utf-8"))
            return temp_path
    except UnicodeDecodeError:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return None
    except Exception:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise

def stream_decode_to_tempfile_fixlines(path, encoding, bom_offset=0, bom_bytes=None):
    temp_path = None
    try:
        fixed_count = 0
        unrecoverable_count = 0
        with open(path, "rb") as f, tempfile.NamedTemporaryFile(mode="wb", delete=False, prefix=TABULAR_TEMP_PREFIX) as tmpfile:
            temp_path = tmpfile.name
            if bom_offset:
                f.seek(bom_offset)

            reader = codecs.getreader(encoding)(f)
            for line in reader:
                fixed_line, fixed = fix_line(line)
                if fixed is True:
                    fixed_count += 1
                elif fixed is None:
                    unrecoverable_count += 1

                tmpfile.write(fixed_line.encode("utf-8"))

            tag = None
            if unrecoverable_count > 0 and fixed_count > 0:
                tag = "partial_data_loss"
            elif unrecoverable_count > 0:
                tag = "irreversible_data_loss"
            elif fixed_count > 0:
                tag = "fixed_data"

            return temp_path, tag, fixed_count, unrecoverable_count
    except UnicodeDecodeError:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return None, None, 0, 0
    except Exception:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise

def detect_best_encoding(path, encodings=ENCODING_CANDIDATES, num_bytes=64 * 1024):
    raw_flags = {"tags": [], "fixed_lines": 0, "unrecoverable_lines": 0}

    with open(path, "rb") as f:
        raw_sample = f.read(num_bytes)

    if not raw_sample:
        return "utf-8", None, None

    bom_encoding = detect_bom(raw_sample)
    bom_bytes = get_bom_bytes(bom_encoding)
    bom_offset = len(bom_bytes) if bom_bytes else 0
    raw_flags["tags"].append("normalized_utf8")

    if bom_encoding:
        sample_content = safe_decode(raw_sample, bom_encoding)
        if sample_content:
            temp_path = stream_decode_to_tempfile(path, bom_encoding, bom_offset)
            if temp_path:
                return "utf-8", temp_path, raw_flags
        raw_flags["tags"].append("bom_invalid_or_unreliable")
        raw_sample = raw_sample[bom_offset:]

    pattern_encoding = detect_encoding_pattern(raw_sample)
    if pattern_encoding:
        bom_bytes = get_bom_bytes(pattern_encoding)
        sample_content = safe_decode(bom_bytes + raw_sample, pattern_encoding)
        if sample_content:
            temp_path = stream_decode_to_tempfile(path, pattern_encoding, 0)
            if temp_path:
                return "utf-8", temp_path, raw_flags

    scored_encodings = []
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            flags = check_encoding_quality(sample_content) or {}
            scored_encodings.append((encoding, sum(flags.values())))

    if scored_encodings:
        for encoding, _ in sorted(scored_encodings, key=lambda item: item[1], reverse=True):
            temp_path = stream_decode_to_tempfile(path, encoding, bom_offset)
            if temp_path:
                return "utf-8", temp_path, raw_flags

    scored_fixed_encodings = []
    for encoding in encodings:
        sample_content = safe_decode(raw_sample, encoding)
        if sample_content and sample_content.strip():
            fixed_content, _, _, _ = fix_lines(sample_content)
            if fixed_content:
                flags = check_encoding_quality(fixed_content) or {}
                scored_fixed_encodings.append((encoding, sum(flags.values())))

    if scored_fixed_encodings:
        for encoding, _ in sorted(scored_fixed_encodings, key=lambda item: item[1], reverse=True):
            temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(path, encoding, bom_offset)
            if temp_path:
                if tag:
                    raw_flags["tags"].append(tag)
                raw_flags["fixed_lines"] = fixed_count
                raw_flags["unrecoverable_lines"] = unrecoverable_count
                return "utf-8", temp_path, raw_flags

    for encoding in encodings:
        temp_path, tag, fixed_count, unrecoverable_count = stream_decode_to_tempfile_fixlines(path, encoding, bom_offset)
        if temp_path:
            logger("WARNING", f"Last-resort encoding recovery applied in file '{path}' using {encoding}")
            if tag:
                raw_flags["tags"].append(tag)
            raw_flags["fixed_lines"] = fixed_count
            raw_flags["unrecoverable_lines"] = unrecoverable_count
            return "utf-8", temp_path, raw_flags

    return None, None, None

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
    return (best, counts[best]) if return_count else best

def detect_delimiter_consistent(path, encoding, max_lines=100, max_cv=0.6, return_stats=False):
    lines = []
    with open(path, "rb") as f:
        for _ in range(max_lines):
            line_bytes = f.readline()
            if not line_bytes:
                break

            line = safe_decode(line_bytes, encoding)
            if not line:
                continue

            line = line.strip()
            if line:
                lines.append(line)

    if not lines:
        return (None, None, {"nonempty_lines": 0}) if return_stats else (None, None)

    delimiter_data = []
    for idx, line in enumerate(lines):
        delim, count = detect_delimiter(line, return_count=True)
        if delim:
            delimiter_data.append((idx, delim, count))

    if not delimiter_data:
        return (None, None, {"nonempty_lines": len(lines)}) if return_stats else (None, None)

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
        return (None, None, {"nonempty_lines": len(lines)}) if return_stats else (None, None)

    candidates.sort(key=lambda x: (-x["freq"], -x["most_common_val"], x["cv"], x["first_idx"]))
    best = candidates[0]
    if return_stats:
        return best["delim"], best["first_idx"], {"nonempty_lines": len(lines)}
    return best["delim"], best["first_idx"]

def resolve_mediatype_conflict(meta_mimetype_og, response, download_url, base_name):
    meta_mimetype, _ = get_mime_and_ext(meta_mimetype_og)
    detected_mime, detected_ext = get_resource_ext_info(response, download_url)
    meta_ext = get_extension_mime(meta_mimetype) if meta_mimetype else None
    clean_base_name = base_name.rsplit(".", 1)[0] if base_name and "." in base_name else base_name

    tag_val = final_ext = final_mime = None
    if detected_mime and detected_mime not in GENERIC_MIME_TYPES:
        if meta_ext and detected_ext and meta_ext != detected_ext:
            tag_val = {
                "<mediaType_old>": meta_mimetype, "<fileName_old>": f"{clean_base_name}.{meta_ext}",
                "<mediaType_new>": detected_mime, "<fileName_new>": f"{clean_base_name}.{detected_ext}",
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

    final_file_name = f"{clean_base_name}.{final_ext}" if final_ext else clean_base_name
    return final_mime, final_file_name, tag_val

def get_mime_and_ext(pos_mime_value):
    if isinstance(pos_mime_value, dict):
        pos_mime_value = pos_mime_value.get("label") or pos_mime_value.get("value") or pos_mime_value.get("id")
        if isinstance(pos_mime_value, dict):
            pos_mime_value = next(iter(pos_mime_value.values()), None)
    if isinstance(pos_mime_value, str) and pos_mime_value:
        pos_mime_value = pos_mime_value.strip().lower().split(";")[0]
        if pos_mime_value.startswith(("https://", "http://")):
            pos_mime_value = pos_mime_value.rstrip("/").rsplit("/", 1)[-1]
        if "/" in pos_mime_value:
            return pos_mime_value, get_extension_mime(pos_mime_value)
        elif "_" in pos_mime_value:
            guessed_ext = pos_mime_value.split("_")[-1]
            return EXT_TO_MIME.get(guessed_ext), guessed_ext
        return EXT_TO_MIME.get(pos_mime_value), pos_mime_value
    return None, None

def get_resource_ext_info(response, download_url):
    headers = response.headers if response is not None else {}
    content_type = headers.get("Content-Type", "").split(";")[0].strip().lower()
    message = Message()
    message["Content-Disposition"] = headers.get("Content-Disposition", "")
    filename = message.get_filename()
    scripts = {"php", "asp", "aspx", "jsp", "htm", "html"}
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
        if ext in EXT_TO_MIME and ext not in scripts:
            return EXT_TO_MIME[ext], ext
    mime, ext = get_mime_and_ext(content_type)
    if mime and mime not in GENERIC_MIME_TYPES:
        return mime, ext
    if download_url:
        filename = unquote(urlparse(download_url).path).rsplit("/", 1)[-1]
        candidate = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
        if candidate in EXT_TO_MIME and candidate not in scripts:
            return EXT_TO_MIME[candidate], candidate
    return mime, ext

def get_ole_extension(path):
    with olefile.OleFileIO(path) as ole:
        streams = {s[0] for s in ole.listdir()}
        if "Workbook" in streams:
            return "xls"
        elif "WordDocument" in streams:
            return "doc"
        elif "PowerPoint Document" in streams:
            return "ppt"
        logger("WARNING", f"Unknown OLE type in file: {path}")
        return "ole"

def get_extension_mime(mime_type):
    try:
        ext = MIME_TYPE_MAP.get(mime_type.lower())
        if isinstance(ext, dict):
            extensions = ext.get("extensions") or []
            ext = extensions[0] if extensions else None

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
                        extensions = ext.get("extensions") or []
                        ext = extensions[0] if extensions else None
                    if ext:
                        return ext

        guessed_ext = mimetypes.guess_extension(mime_type.lower())
        if guessed_ext:
            return guessed_ext.lstrip(".")

        return None
    except Exception as e:
        logger("ERROR", f"Failed to get extension for MIME type: '{mime_type}'", e)
        return None
