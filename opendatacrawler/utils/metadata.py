import hashlib
import copy
import json
import re
from datetime import datetime
from html import unescape

import pycountry
from bs4 import BeautifulSoup

from .network import is_url
from .resources import CRAWLER_CHANGES_INFO


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


def generate_short_filename(fname, ext=None):
    fname_hash = hashlib.sha1(fname.encode("utf-8")).hexdigest()
    return f"{fname_hash}.{ext}" if ext else fname_hash


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
        if isinstance(template, dict):
            return {k: apply_fallback_template(v, field) for k, v in template.items()}
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


def split_multivalue(value, separator="//"):
    if not value or not isinstance(value, str):
        return []
    if separator == "//":
        parts = re.split(r"(?<!:)//", value)
    else:
        parts = value.split(separator)
    return [part.strip() for part in parts if part and part.strip()]


def extract_bracketed_lang_text(value, default_lang="es"):
    if not value or not isinstance(value, str):
        return {}

    matches = re.findall(r"\[([a-z]{2})\](.*?)(?=\[[a-z]{2}\]|$)", value, flags=re.IGNORECASE | re.DOTALL)
    if matches:
        result = {}
        for lang, text in matches:
            clean_text = " ".join(unescape(text).split()).strip()
            if clean_text:
                result.setdefault(lang.lower(), []).append(clean_text)
        return result

    clean_value = " ".join(unescape(value).split()).strip()
    return {default_lang: [clean_value]} if clean_value else {}


def extract_first_lang_text(value, preferred_langs=("es", "en", "ca", "gl", "eu"), default_lang="es"):
    multi = extract_bracketed_lang_text(value, default_lang=default_lang)
    if not multi:
        return ""

    for lang in preferred_langs:
        if multi.get(lang):
            return multi[lang][0]

    first_lang = next(iter(multi), None)
    return multi[first_lang][0] if first_lang else ""


def normalize_no_html_text(text):
    if not text or not isinstance(text, str):
        return text

    text = BeautifulSoup(text, "html.parser").get_text()
    text = " ".join(text.split())

    return text.strip()


def get_country_label(code):
    if code:
        code = str(code).strip()
        if not code:
            return None

        country = pycountry.countries.get(alpha_2=code.upper())
        return country.name if country else None
    return None


def get_language_label(code):
    if code:
        code = str(code).strip()
        if not code:
            return None

        language = pycountry.languages.get(alpha_2=code.lower())
        if not language:
            language = pycountry.languages.get(alpha_3=code.lower())

        return getattr(language, "name", None) if language else None
    return None


def normalize_language_values(values):
    values = [values] if isinstance(values, str) else values if isinstance(values, list) else []
    normalized = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue

        if "publications.europa.eu/resource/authority/language/" in value:
            code = value.rstrip("/").split("/")[-1]
            label = get_language_label(code)
            normalized.append(label or code)
        else:
            normalized.append(value)

    return list(dict.fromkeys(normalized))


def sanitize_json_keys(obj):
    if isinstance(obj, dict):
        sanitized = {}
        for key, value in obj.items():
            if key is None:
                sanitized_key = "_none"
            elif isinstance(key, str):
                sanitized_key = key
            else:
                sanitized_key = str(key)
            sanitized[sanitized_key] = sanitize_json_keys(value)
        return sanitized
    if isinstance(obj, list):
        return [sanitize_json_keys(item) for item in obj]
    return obj


def snake_to_camel_key(key):
    if not isinstance(key, str) or "_" not in key:
        return key
    head, *tail = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail if part)


def metadata_tag_key(tag):
    return snake_to_camel_key(tag)


def prepare_metadata_for_save(package):
    metadata_copy = copy.deepcopy(package)
    crawler_info = metadata_copy.get("crawlerInfo")
    if not isinstance(crawler_info, dict):
        return sanitize_json_keys(metadata_copy)

    package_status = crawler_info.get("packageStatus")
    if isinstance(package_status, dict):
        package_status.pop("changed_metadata", None)
        package_status.pop("changedMetadata", None)

    return sanitize_json_keys(metadata_copy)


def init_metadata(package=True, crawled=True):
    if package:
        return {
            "crawlerInfo": {
                "packageInfo": {},
                "packageMetadataChanges": {},
                "packageStatus": {
                    "packageCrawled": datetime.now().isoformat(),
                },
                "resourcesInfo": {},
            }
        }

    base = {
        "fileMetadataChanges": {},
        "binaryFileChanges": {},
        "fileInfo": {},
        "fileStatus": {},
    }
    if crawled:
        base["fileStatus"]["fileCrawled"] = datetime.now().isoformat()
    return base


def add_tag_explanations(tags, data_source=None):
    data_source = data_source or {}

    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    elif isinstance(tags, str):
        tags = [tags]

    explanations = {}
    for tag in tags:
        metadata_key = metadata_tag_key(tag)
        tag_content = CRAWLER_CHANGES_INFO.get(tag)
        if not tag_content:
            explanations[metadata_key] = {"reason": "(no explanation available)"}
            continue

        tag_explanation = json.dumps(tag_content.get("tag_explanation", {}))
        tag_placeholders = tag_content.get("tag_placeholders", [])
        for tag_placeholder in tag_placeholders:
            tag_explanation = tag_explanation.replace(
                tag_placeholder, str(data_source.get(tag_placeholder, f"<{tag_placeholder}>"))
            )

        explanations[metadata_key] = json.loads(tag_explanation)

    return explanations
