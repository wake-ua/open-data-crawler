import csv
import json
import os
import re
import sys
from typing import Any, cast
from urllib.parse import urlparse

import requests

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class DatosGobEsCrawler:
    CATALOG_CSV_URL = "https://mycloud.red.es/index.php/s/X8pPWWsiEWYyBJR/download?path=/&files=datosgobes.csv"
    CATALOG_CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "datosgobes_catalog.csv")
    CATALOG_CSV_META_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "datosgobes_catalog_meta.json")
    CATALOG_PACKAGE_IDS = {
        "ea0044367-catalogo-de-datos-de-datos-gob-es",
    }

    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
        self._catalog_rows_by_id = None
        self.odcrawler.serial_metadata_phase = False

    def _parse_frequency(self, value):
        if not value or not isinstance(value, str):
            return {}

        type_match = re.search(r"\[TYPE\](.*?)(?=\[VALUE\]|$)", value)
        value_match = re.search(r"\[VALUE\](.*)$", value)
        parsed = {}
        if type_match:
            parsed["type"] = type_match.group(1).strip()
        if value_match:
            parsed["value"] = value_match.group(1).strip()
        return parsed

    def _parse_temporal_range(self, value):
        if not value or not isinstance(value, str):
            return {}

        parts = [part.strip() for part in value.split("-") if part.strip()]
        if len(parts) >= 2:
            return {"startDate": parts[0], "endDate": parts[1]}
        if parts:
            return {"startDate": parts[0], "endDate": None}
        return {}

    def _parse_distributions(self, value):
        if not value or not isinstance(value, str):
            return []
        token_pattern = re.compile(r"\[([A-Za-z_]+)\]")
        matches = list(token_pattern.finditer(value))
        if not matches:
            return []

        pending_title = {}
        current: dict[str, Any] | None = None
        distributions = []

        def add_title(target, lang, text):
            clean_text = " ".join(text.split()).strip().rstrip("/")
            if clean_text:
                target.setdefault((lang or "es").lower(), []).append(clean_text)

        def clean_token_value(text):
            if not isinstance(text, str):
                return text
            return " ".join(text.split()).strip().rstrip("/")

        def finalize(dist):
            if not dist:
                return
            access_url = utils.fix_url(dist.get("accessURL"))
            if not access_url:
                return
            dist["accessURL"] = access_url
            dist["downloadURL"] = access_url
            dist["title"] = {lang: list(dict.fromkeys(vals)) for lang, vals in dist.get("title", {}).items() if vals}
            media_value = (dist.get("format") or {}).get("value")
            dist["format"] = {"value": media_value} if media_value else {}
            relation = dist.get("relation")
            if relation:
                dist["relation"] = utils.fix_url(relation)
            else:
                dist.pop("relation", None)
            distributions.append(dist)

        for idx, match in enumerate(matches):
            key = match.group(1)
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(value)
            raw_val = value[start:end].strip()
            raw_val = clean_token_value(raw_val)

            if key.startswith("TITLE"):
                lang = key.split("_", 1)[1].lower() if "_" in key else "es"
                if current is not None and not current.get("accessURL"):
                    current_dist = cast(Any, current)
                    target = current_dist["title"]
                else:
                    target = pending_title
                add_title(target, lang, raw_val)
            elif key == "ACCESS_URL":
                if current and current.get("accessURL"):
                    finalize(current)
                    current = None
                current = {
                    "title": {lang: vals[:] for lang, vals in pending_title.items()},
                    "accessURL": raw_val,
                    "downloadURL": raw_val,
                    "format": {},
                }
                pending_title = {}
            elif key == "MEDIA_TYPE":
                if current is not None and raw_val:
                    current_dist = cast(Any, current)
                    current_dist["format"] = {"value": raw_val}
            elif key == "RELATION":
                if current is not None and raw_val:
                    current_dist = cast(Any, current)
                    current_dist["relation"] = raw_val

        finalize(current)
        return distributions

    def _extract_catalog_id(self, row):
        for field in ("IDENTIFICADOR", "URL", "URL DE ACCESO"):
            raw_value = row.get(field)
            if not raw_value or not isinstance(raw_value, str):
                continue
            raw_value = raw_value.strip()
            if not raw_value:
                continue
            if "catalogo/" in raw_value:
                return raw_value.rstrip("/").split("/")[-1]
        return None

    def _load_catalog_rows(self):
        if self._catalog_rows_by_id is not None:
            return self._catalog_rows_by_id

        try:
            csv.field_size_limit(sys.maxsize)
        except OverflowError:
            csv.field_size_limit(2**31 - 1)

        headers = {
            "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
            "User-Agent": self.odcrawler.user_agent,
            "Connection": "keep-alive",
        }

        refresh_catalog = True
        local_meta = {}
        if os.path.exists(self.CATALOG_CSV_META_PATH):
            try:
                with open(self.CATALOG_CSV_META_PATH, "r", encoding="utf-8") as meta_file:
                    local_meta = json.load(meta_file)
            except Exception:
                local_meta = {}

        if os.path.exists(self.CATALOG_CSV_PATH):
            try:
                head_response = requests.head(
                    self.CATALOG_CSV_URL,
                    headers=headers,
                    verify=False,
                    timeout=self.odcrawler.max_sec,
                    allow_redirects=True,
                )
                if head_response.ok:
                    remote_size = head_response.headers.get("Content-Length")
                    if remote_size and str(remote_size) == str(local_meta.get("content_length")):
                        refresh_catalog = False
                        logger("INFO", f"Using existing local datos.gob.es catalog CSV at '{self.CATALOG_CSV_PATH}' (same Content-Length)")
                head_response.close()
            except requests.RequestException:
                refresh_catalog = False
                logger("WARNING", f"Using existing local datos.gob.es catalog CSV at '{self.CATALOG_CSV_PATH}' because remote header check failed")

        if refresh_catalog:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(
                self.CATALOG_CSV_URL,
                self.odcrawler.user_agent,
                headers=headers,
            )
            if response:
                if utils.atomic_write_bytes(self.CATALOG_CSV_PATH, response.content):
                    meta_payload = {
                        "source_url": self.CATALOG_CSV_URL,
                        "content_length": response.headers.get("Content-Length"),
                    }
                    utils.atomic_dump_json(self.CATALOG_CSV_META_PATH, meta_payload, indent=2)
                    logger("INFO", f"datos.gob.es catalog CSV refreshed at '{self.CATALOG_CSV_PATH}'")
                response.close()
            elif not os.path.exists(self.CATALOG_CSV_PATH):
                logger("ERROR", f"Error fetching datos.gob.es catalog CSV from '{self.CATALOG_CSV_URL}' and no local catalog file found")
                self._catalog_rows_by_id = {}
                return self._catalog_rows_by_id
            else:
                logger("WARNING", f"Using existing local datos.gob.es catalog CSV at '{self.CATALOG_CSV_PATH}' because remote refresh failed")

        rows_by_id = {}
        total_rows = 0
        extracted_ids = 0
        excluded_catalog_rows = 0
        duplicate_ids = 0
        try:
            with open(self.CATALOG_CSV_PATH, encoding="utf-8-sig", errors="replace", newline="") as catalog_file:
                reader = csv.DictReader(catalog_file)
                for row in reader:
                    row = {key: value for key, value in row.items() if key is not None}
                    extra_fields = row.pop(None, None)
                    if extra_fields:
                        existing = row.get("DISTRIBUCIONES", "") or ""
                        extras = [field for field in extra_fields if isinstance(field, str) and field.strip()]
                        if extras:
                            row["DISTRIBUCIONES"] = f"{existing},{','.join(extras)}" if existing else ",".join(extras)
                    total_rows += 1
                    package_id = self._extract_catalog_id(row)
                    if package_id:
                        extracted_ids += 1
                    if package_id in self.CATALOG_PACKAGE_IDS:
                        excluded_catalog_rows += 1
                        continue
                    if package_id:
                        if package_id in rows_by_id:
                            duplicate_ids += 1
                        rows_by_id[package_id] = row
        except Exception as e:
            logger("ERROR", "Unexpected error parsing datos.gob.es fallback catalog CSV", e)
            rows_by_id = {}

        self._catalog_rows_by_id = rows_by_id
        return self._catalog_rows_by_id

    def get_package_list(self):
        ids = list(self._load_catalog_rows().keys())
        logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.get_print_domain()}'", level="print")
        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        title = resource_meta.get("title", {})
        if isinstance(title, dict) and any(isinstance(v, list) for v in title.values()):
            resource["name"] = title
        else:
            resource["name"] = utils.extract_multilang_field(title, "_lang", "_value")

        resource["downloadURL"] = utils.fix_url(resource_meta.get("accessURL") or resource_meta.get("downloadURL"))

        meta_media_type = resource_meta.get("format", {}).get("value")

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        row = self._load_catalog_rows().get(package_id)
        if not row:
            logger("WARNING", f"No catalog CSV row found for package '{package_id}'")
            return None

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["accessURL"] = utils.fix_url(f"https://datos.gob.es/es/catalogo/{package_id}")
        metadata["requestURL"] = self.CATALOG_CSV_URL
        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://datos.gob.es/sites/default/files/favicon.png"

        metadata["title"] = utils.extract_bracketed_lang_text(row.get("TÍTULO"))
        metadata["description"] = utils.extract_bracketed_lang_text(row.get("DESCRIPCIÓN"))
        publisher_name = (row.get("ÓRGANO PUBLICADOR") or "").strip()
        metadata["publisher"] = {"title": publisher_name, "name": publisher_name} if publisher_name else {}
        metadata["language"] = utils.normalize_language_values(utils.split_multivalue(row.get("IDIOMAS")))
        metadata["keyword"] = utils.extract_bracketed_lang_text(row.get("ETIQUETAS"))
        metadata["theme"] = utils.split_multivalue(row.get("TEMÁTICAS"))
        metadata["accrualPeriodicity"] = self._parse_frequency(row.get("FRECUENCIA DE ACTUALIZACIÓN"))
        metadata["modified"] = row.get("FECHA DE ÚLTIMA MODIFICACIÓN")
        metadata["issued"] = row.get("FECHA DE CREACIÓN")
        metadata["license"] = row.get("CONDICIONES DE USO")
        metadata["source"] = self.odcrawler.domain
        metadata["temporal"] = self._parse_temporal_range(row.get("COBERTURA TEMPORAL"))
        metadata["geo"] = utils.split_multivalue(row.get("COBERTURA GEOGRÁFICA"))

        distributions = self._parse_distributions(row.get("DISTRIBUCIONES"))
        fallback_url = utils.fix_url(row.get("URL DE ACCESO"))
        if not distributions and fallback_url:
            distributions = [{
                "title": {"es": [utils.extract_first_lang_text(row.get("TÍTULO")) or package_id]},
                "accessURL": fallback_url,
                "downloadURL": fallback_url,
                "format": {"value": "HTML"},
            }]

        if distributions:
            download_url = distributions[0].get("accessURL") or distributions[0].get("downloadURL")
            if download_url and metadata["publisher"] is not None:
                metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = row

        return metadata
