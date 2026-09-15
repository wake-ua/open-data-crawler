import configparser
import json
import os

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES_DIR = os.path.join(PACKAGE_DIR, "resources")
CONFIG_PATH = os.environ.get("ODC_CONFIG", os.path.join(PACKAGE_DIR, "config.ini"))

def print_intro():
    with open(os.path.join(RESOURCES_DIR, "intro.txt"), "r", encoding="utf-8") as f:
        print(f.read())

def load_resource(filename, fallback_value=None):
    with open(os.path.join(RESOURCES_DIR, filename), "r", encoding="utf-8") as f:
        data = json.load(f)

    if fallback_value:
        data["MAP_FALLBACK"] = fallback_value

    return data

def load_tokens():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)

    return {
        section.lower(): config.get(section, "token", fallback=None) or None
        for section in config.sections()
    }

def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config

def _section_aliases(section):
    normalized = str(section).strip().lower()
    compact = normalized.replace("_", "").replace("-", "")
    aliases = [normalized]
    if compact != normalized:
        aliases.append(compact)
    return aliases

def get_config_option(section, option, cast=str, fallback=None):
    config = load_config()
    option = str(option).strip().lower()

    target_aliases = set(_section_aliases(section))
    for section_name in config.sections():
        if section_name.strip().lower() not in target_aliases and section_name.strip().lower().replace("_", "").replace("-", "") not in target_aliases:
            continue
        if not config.has_option(section_name, option):
            continue

        if cast is bool:
            return config.getboolean(section_name, option, fallback=fallback)
        if cast is int:
            return config.getint(section_name, option, fallback=fallback)
        if cast is float:
            return config.getfloat(section_name, option, fallback=fallback)
        return config.get(section_name, option, fallback=fallback)

    return fallback

def build_extension_to_mime_map(mime_map):
    ext_to_mime = {}
    for mime, info in mime_map.items():
        if isinstance(info, dict):
            exts = info.get("extensions")
            if exts and len(exts) > 0:
                for ext in exts:
                    ext_to_mime.setdefault(ext.lower(), mime)
    ext_to_mime.update({
        "json": "application/json", "xml": "application/xml",
        "csv": "text/csv", "tsv": "text/tab-separated-values",
        "zip": "application/zip", "px": "text/x-pcaxis",
        "geojson": "application/geo+json",
    })
    return ext_to_mime

ZENODO_STATE_FILE = os.path.join(RESOURCES_DIR, "zenodo_state.json")
AUTH_TOKENS = load_tokens()

USER_AGENTS = load_resource("user_agents.json")
MIME_TYPE_MAP = load_resource("mime_extensions.json")
EXT_TO_MIME = build_extension_to_mime_map(MIME_TYPE_MAP)

CRAWLER_CHANGES_INFO = {
    **load_resource("raw_changes_explanations.json"),
    **load_resource("file_info_explanations.json"),
    **load_resource("metadata_changes_explanations.json"),
}

DATOSGOBESCRAWLER_THEME_MAP = load_resource(
    "datosgobes_theme_map.json",
    fallback_value="FALLBACK_VALUE",
)

DATOSGOBESCRAWLER_SPATIAL_MAP = load_resource(
    "datosgobes_spatial_map.json",
    fallback_value={"name": "FALLBACK_VALUE", "type": None},
)

CKANCRAWLER_SPATIAL_MAP = load_resource(
    "ckan_spatial_map.json",
    fallback_value={"name": "FALLBACK_VALUE", "type": None},
)

DATOSGOBESCRAWLER_PUBLISHER_MAP = load_resource(
    "datosgobes_publisher_map.json",
    fallback_value={
        "identifier": {"value": "FALLBACK_VALUE", "scheme": None},
        "name": None,
    },
)
