import chardet
import mimetypes
import hashlib
import os
from w3lib.url import url_query_cleaner
from url_normalize import url_normalize
import json
from opendatacrawler.setup_logger import logger

def print_intro():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "intro.txt"), "r", encoding="utf-8") as f:
        print(f.read())

def check_url(url):
    return url.startswith("http://") or url.startswith("https://")

def create_folder(path):
    try:
        os.makedirs(path, exist_ok=True)
        logger("OK", f"Successfully created the dir '{path}'")
        return True
    except OSError as e:
        logger("ERROR", f"Creation of the dir '{path}' failed", e)
        return False

def clean_url(u):
    u = url_normalize(u)
    u = url_query_cleaner(u, remove=True, parameterlist=["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"])
    
    for prefix in ("http://", "https://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]

    return u.split("/")[0]

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
            
            failed_resources = []
            successful_resources = []
            for r in meta.get("resources", []):
                if accepted_types and r.get("fileName", "").split(".")[-1].lower() not in accepted_types:
                    continue

                if r.get("path"):
                    successful_resources.append(r.get("fileName"))
                else:
                    failed_resources.append(r.get("fileName"))

            packages_status[meta.get("identifier")] = {
                "failed_resources": failed_resources,
                "successful_resources": successful_resources
            }

            total_failed.extend(failed_resources)
            total_successful.extend(successful_resources)

            if failed_resources:
                failed_packages.add(meta.get("identifier"))

        except Exception as e:
            logger("ERROR", f"Could not read {meta_path}", e)

    return packages_status, total_successful, total_failed, failed_packages

def detect_encoding(path):
    with open(path, 'rb') as f:
        return chardet.detect(f.read())['encoding']

def get_extension_mime(mime_type):
    mime_map = {
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
        "application/pdf": "pdf",
        "text/pc-axis": "px",
        "application/json": "json",
        "application/xml": "xml",
        "application/x-tmx+xml": "tmx",
        "application/api": "api",
        "application/x-zip-compressed": "zip",
        "application/x-zipped-shp": "zip",
        "application/rss+xml": "rss",
        "application/netcdf": "nc",
        "text/ascii": "txt",
        "application/elp": "elp",
        "application/scorm": "zip",
        "application/ecw": "ecw",
        "application/vnd.geo+json": "geojson",
        "application/vnd.ogc.wms_xml": "xml",
        "text/wms": "wms",
        "text/rdf+n3": "n3",
        "application/x-turtle": "ttl",
        "text/wfs": "xml",
        "application/las": "las",
        "application/javascript": "js",
        "application/sparql-results+json": "json",
        "application/geo+pdf": "pdf",
    }

    try:
        ext = mime_map.get(mime_type.lower())
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

def generate_short_filename(fname, ext=None, maxlen=10):
    fname_hash = hashlib.sha1(fname.encode("utf-8")).hexdigest()[:maxlen]
    
    return f"{fname_hash}.{ext}" if ext else fname_hash