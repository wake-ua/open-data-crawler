import csv
import json
import os
import re
import sys
import sqlite3
from contextlib import closing
from collections.abc import Mapping
import tempfile
import hashlib
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class CatalogRows(Mapping):
    def __init__(self, path):
        self.path = path

    def __getitem__(self, key):
        row_id = self.resolve_id(key)
        if row_id is None:
            raise KeyError(key)
        with closing(sqlite3.connect(self.path)) as database:
            row = database.execute("SELECT payload FROM rows WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def resolve_id(self, key):
        key = str(key)
        with closing(sqlite3.connect(self.path)) as database:
            row = database.execute("SELECT id FROM rows WHERE id=?", (key,)).fetchone()
            if row is not None:
                return row[0]
            try:
                row = database.execute("SELECT id FROM aliases WHERE alias=?", (key,)).fetchone()
            except sqlite3.OperationalError:
                return None
            return row[0] if row is not None else None

    def __iter__(self):
        with closing(sqlite3.connect(self.path)) as database:
            yield from (row[0] for row in database.execute("SELECT id FROM rows ORDER BY id"))

    def __len__(self):
        with closing(sqlite3.connect(self.path)) as database:
            return database.execute("SELECT count(*) FROM rows").fetchone()[0]

    def items(self):
        with closing(sqlite3.connect(self.path)) as database:
            for key, payload in database.execute("SELECT id, payload FROM rows ORDER BY id"):
                yield key, json.loads(payload)

class DatosGobEsCrawler:
    CATALOG_CSV_URL = "https://mycloud.red.es/index.php/s/X8pPWWsiEWYyBJR/download?path=/&files=datosgobes.csv"
    CATALOG_CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "datosgobes_catalog.csv")
    CATALOG_CSV_META_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "datosgobes_catalog_meta.json")
    CATALOG_PACKAGE_IDS = {
        "ea0044367-catalogo-de-datos-de-datos-gob-es",
    }
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
        cache = os.path.join(self.odcrawler.base_domain_path, ".cache")
        os.makedirs(cache, exist_ok=True)
        self.CATALOG_CSV_PATH = os.path.join(cache, "datosgobes_catalog.csv")
        self.CATALOG_CSV_META_PATH = os.path.join(cache, "datosgobes_catalog_meta.json")
        self.catalog_index_path = os.path.join(cache, "datosgobes.sqlite")
        self._catalog_rows_by_id = None
        self.odcrawler.serial_metadata_phase = False

    def _parse_frequency(self, value):
        if not value or not isinstance(value, str):
            return {}

        if utils.is_url(value.strip()):
            return {"type": value.strip(), "value": value.strip().rstrip("/").rsplit("/", 1)[-1]}
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

        date_pattern = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
        intervals = []
        for part in utils.split_multivalue(value):
            match = re.fullmatch(rf"\s*({date_pattern})?\s*(?:/| - |-)\s*({date_pattern})?\s*", part)
            if match and any(match.groups()):
                intervals.append({"startDate": match[1], "endDate": match[2]})
            elif re.fullmatch(date_pattern, part):
                intervals.append({"startDate": part, "endDate": None})
        return intervals[0] if len(intervals) == 1 else intervals

    def _parse_distributions(self, value):
        if not value or not isinstance(value, str):
            return []
        token_pattern = re.compile(r"\[([A-Za-z_]+)\]")
        matches = list(token_pattern.finditer(value))
        if not matches:
            return []

        pending_title: dict[str, list[str]] = {}
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
                    target = current["title"]
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
                    current["format"] = {"value": raw_val}
            elif key == "BYTE_SIZE":
                if current is not None and raw_val.isdigit():
                    current["byteSize"] = int(raw_val)
            elif key == "IDENTIFIER":
                if current is not None and raw_val:
                    current["identifier"] = raw_val
            elif key == "RELATION":
                if current is not None and raw_val:
                    current["relation"] = raw_val

        finalize(current)
        return distributions

    def _extract_catalog_id(self, row):
        for field in ("URL", "URL DE ACCESO"):
            raw_value = row.get(field)
            if not raw_value or not isinstance(raw_value, str):
                continue
            raw_value = raw_value.strip()
            if not raw_value:
                continue
            if "catalogo/" in raw_value:
                return raw_value.rstrip("/").split("/")[-1]
        return None

    def _extract_source_identifier(self, row):
        raw_value = row.get("IDENTIFICADOR")
        if not raw_value or not isinstance(raw_value, str):
            return None
        raw_value = raw_value.strip().rstrip("/")
        return raw_value or None

    def _catalog_access_url(self, row, package_id):
        raw_url = row.get("URL")
        parsed = urlparse(raw_url or "")
        if parsed.netloc == "datos.gob.es" and "/catalogo/" in parsed.path:
            return utils.fix_url(f"https://datos.gob.es/es/catalogo/{package_id}")
        return utils.fix_url(raw_url or f"https://datos.gob.es/es/catalogo/{package_id}")

    def _publisher_identifier(self, package_id):
        prefix = str(package_id).split("-", 1)[0].upper()
        if re.fullmatch(r"[A-Z]\d{8}", prefix):
            return f"http://datos.gob.es/recurso/sector-publico/org/Organismo/{prefix}"
        return None

    def _resource_identifier_from_url(self, value):
        if not value:
            return None
        match = re.search(r"/resource/([^/?#]+)/", value)
        return match.group(1) if match else None

    def _source_identifier_aliases(self, source_identifier):
        if not source_identifier:
            return []
        aliases = [source_identifier.rstrip("/")]
        if "catalogo/" in source_identifier:
            aliases.append(source_identifier.rstrip("/").split("/")[-1])
        return list(dict.fromkeys(alias for alias in aliases if alias))

    def _normalize_catalog_timestamp(self, value):
        if not value:
            return None

        text = str(value).strip()
        if not text:
            return None

        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        elif re.search(r"[+-]\d{4}$", text):
            text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt

    def _get_metadata_path(self, package_id):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.odcrawler.domain}_{package_id}')}.json"
        return metadata_file_name, os.path.join(self.odcrawler.save_path, metadata_file_name)

    def _load_local_package_modified(self, package_id):
        metadata_file_name, metadata_path = self._get_metadata_path(package_id)
        if not os.path.exists(metadata_path):
            return None, False, False

        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            return metadata.get("modified"), True, False
        except Exception as e:
            logger("WARNING", f"Could not read local metadata file '{metadata_file_name}' while checking datos.gob.es updates; the package will be refreshed", e)
            return None, True, True

    def _detect_packages_requiring_refresh(self, rows_by_id):
        refresh_packages = []
        for package_id, row in (rows_by_id or {}).items():
            remote_modified = self._normalize_catalog_timestamp(row.get("FECHA DE ÚLTIMA MODIFICACIÓN"))
            if remote_modified is None:
                continue

            local_modified, local_exists, local_read_error = self._load_local_package_modified(package_id)
            if not local_exists:
                continue
            if local_read_error:
                refresh_packages.append(package_id)
                continue

            local_modified = self._normalize_catalog_timestamp(local_modified)
            if local_modified is None or remote_modified > local_modified:
                refresh_packages.append(package_id)

        if refresh_packages:
            logger("INFO", f"Detected {len(refresh_packages)} existing datos.gob.es package(s) with catalog updates; they will be refreshed in this run", level="print")

        return refresh_packages

    def _index_csv(self, csv_path, index_path):
        csv.field_size_limit(16 * 1024 * 1024)
        count = 0
        with closing(sqlite3.connect(index_path)) as database, database, open(csv_path, encoding="utf-8-sig", newline="") as source:
            database.execute("CREATE TABLE rows (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            database.execute("CREATE TABLE aliases (alias TEXT PRIMARY KEY, id TEXT NOT NULL)")
            reader = csv.DictReader(source, strict=True)
            required = {"URL", "TÍTULO", "DISTRIBUCIONES", "IDENTIFICADOR"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise utils.CatalogError("datos.gob.es CSV does not contain required columns")
            ambiguous_aliases = set()
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise utils.CatalogError("Malformed or truncated datos.gob.es CSV record")
                key = self._extract_catalog_id(row)
                if not key:
                    raise utils.CatalogError("datos.gob.es row has no catalog identifier")
                if key in self.CATALOG_PACKAGE_IDS:
                    continue
                try:
                    database.execute("INSERT INTO rows VALUES (?, ?)", (key, json.dumps(row, ensure_ascii=False)))
                except sqlite3.IntegrityError as error:
                    raise utils.CatalogError(f"Duplicate datos.gob.es catalog ID: {key}") from error
                for alias in self._source_identifier_aliases(self._extract_source_identifier(row)):
                    if alias == key or alias in ambiguous_aliases:
                        continue
                    try:
                        database.execute("INSERT INTO aliases VALUES (?, ?)", (alias, key))
                    except sqlite3.IntegrityError:
                        database.execute("DELETE FROM aliases WHERE alias=?", (alias,))
                        ambiguous_aliases.add(alias)
                count += 1
        if not count:
            raise utils.CatalogError("datos.gob.es CSV catalog is empty")
        return count

    def _load_catalog_rows(self):
        if self._catalog_rows_by_id is not None:
            return self._catalog_rows_by_id
        with utils.file_lock(self.CATALOG_CSV_PATH + ".lock"):
            local = {}
            try:
                with open(self.CATALOG_CSV_META_PATH) as source:
                    local = json.load(source)
            except (OSError, ValueError):
                pass
            ttl = utils.get_config_option("datosgobes", "catalog_ttl_seconds", int, 86400)
            ready = os.path.exists(self.catalog_index_path) and os.path.exists(self.CATALOG_CSV_PATH) and not local.get("refreshing")
            fresh = ready and local.get("source_url") == self.CATALOG_CSV_URL and time.time() - local.get("checked_at", 0) < ttl
            if not fresh:
                headers = {"Accept": "text/csv"}
                if ready and local.get("etag"):
                    headers["If-None-Match"] = local["etag"]
                elif ready and local.get("last_modified"):
                    headers["If-Modified-Since"] = local["last_modified"]
                response, self.odcrawler.user_agent = self.odcrawler.make_request(self.CATALOG_CSV_URL, self.odcrawler.user_agent, headers=headers, stream=True)
                if response is None:
                    raise utils.CatalogError("datos.gob.es catalog refresh failed; previous cache retained")
                temp_csv = temp_index = None
                try:
                    response.raise_for_status()
                    if response.status_code == 304 and ready:
                        local["checked_at"] = time.time()
                    else:
                        digest = hashlib.sha256()
                        total = 0
                        maximum = utils.get_config_option("datosgobes", "catalog_max_mb", int, 1024) * 1024 * 1024
                        with tempfile.NamedTemporaryFile(dir=os.path.dirname(self.CATALOG_CSV_PATH), prefix=utils.ATOMIC_TEMP_PREFIX, delete=False) as output:
                            temp_csv = output.name
                            for chunk in response.iter_content(1024 * 1024):
                                total += len(chunk)
                                if total > maximum:
                                    raise utils.CatalogError("datos.gob.es catalog exceeds configured size limit")
                                self.odcrawler.ensure_disk_headroom(temp_csv, required_bytes=len(chunk), context="caching catalog")
                                digest.update(chunk); output.write(chunk)
                            output.flush(); os.fsync(output.fileno())
                        temp_index = temp_csv + ".sqlite"
                        count = self._index_csv(temp_csv, temp_index)
                        if not utils.atomic_dump_json(self.CATALOG_CSV_META_PATH, {**local, "refreshing": True}):
                            raise utils.CatalogError("Could not record catalog replacement intent")
                        os.replace(temp_csv, self.CATALOG_CSV_PATH)
                        os.replace(temp_index, self.catalog_index_path)
                        local = {"source_url": self.CATALOG_CSV_URL, "checked_at": time.time(), "sha256": digest.hexdigest(),
                                 "sizeBytes": total, "count": count, "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified")}
                    if not utils.atomic_dump_json(self.CATALOG_CSV_META_PATH, local, indent=2):
                        raise utils.CatalogError("Could not persist catalog cache metadata")
                finally:
                    response.close()
                    for path in (temp_csv, temp_index):
                        if path and os.path.exists(path):
                            os.remove(path)
            self._catalog_rows_by_id = CatalogRows(self.catalog_index_path)
        return self._catalog_rows_by_id

    def get_package_list(self):
        rows_by_id = self._load_catalog_rows()
        if self.odcrawler.check_remote_updates:
            self.odcrawler.set_packages_requiring_refresh(self._detect_packages_requiring_refresh(rows_by_id))
        else:
            self.odcrawler.set_packages_requiring_refresh([])
        ids = list(rows_by_id.keys())
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

        resource["accessURL"] = utils.fix_url(resource_meta.get("accessURL"))
        resource["downloadURL"] = utils.fix_url(resource_meta.get("downloadURL") or resource["accessURL"])
        resource["identifier"] = resource_meta.get("identifier") or self._resource_identifier_from_url(resource["accessURL"])
        resource["modified"] = resource_meta.get("modified")
        resource["remoteSize"] = resource_meta.get("byteSize")
        resource["relation"] = resource_meta.get("relation")

        meta_media_type = resource_meta.get("format", {}).get("value")

        return resource, meta_media_type

    def _build_resource_match_signature(self, resource):
        if not isinstance(resource, dict):
            return resource
        name = resource.get("name")
        if isinstance(name, dict):
            name = json.dumps(utils.sanitize_json_keys(name), ensure_ascii=False, sort_keys=True)
        return (
            resource.get("downloadURL"),
            resource.get("mediaType"),
            resource.get("modified"),
            name,
        )

    def merge_existing_package_state(self, existing_package, package):
        return self.odcrawler.merge_existing_package_state(existing_package, package)

    def get_package(self, package_id, metadata_file_name):
        rows = self._load_catalog_rows()
        row = rows.get(package_id)
        if not row:
            logger("WARNING", f"No catalog CSV row found for package '{package_id}'")
            return None
        package_id = rows.resolve_id(package_id) or package_id

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["accessURL"] = self._catalog_access_url(row, package_id)
        metadata["requestURL"] = self.CATALOG_CSV_URL
        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://datos.gob.es/sites/default/files/favicon.png"
        source_identifier = self._extract_source_identifier(row)
        if source_identifier and source_identifier != metadata["accessURL"].rstrip("/"):
            metadata["sourceIdentifier"] = source_identifier

        metadata["title"] = utils.extract_bracketed_lang_text(row.get("TÍTULO"))
        metadata["description"] = utils.extract_bracketed_lang_text(row.get("DESCRIPCIÓN"))
        publisher_name = (row.get("ÓRGANO PUBLICADOR") or "").strip()
        metadata["publisher"] = {"identifier": self._publisher_identifier(package_id), "title": publisher_name, "name": publisher_name} if publisher_name else {}
        metadata["language"] = utils.normalize_language_values(utils.split_multivalue(row.get("IDIOMAS")))
        metadata["languageURI"] = utils.split_multivalue(row.get("IDIOMAS"))
        metadata["keyword"] = utils.extract_bracketed_lang_text(row.get("ETIQUETAS"))
        metadata["theme"] = utils.split_multivalue(row.get("TEMÁTICAS"))
        metadata["accrualPeriodicity"] = self._parse_frequency(row.get("FRECUENCIA DE ACTUALIZACIÓN"))
        metadata["modified"] = row.get("FECHA DE ÚLTIMA MODIFICACIÓN")
        metadata["issued"] = row.get("FECHA DE CREACIÓN")
        metadata["license"] = row.get("CONDICIONES DE USO")
        metadata["source"] = self.odcrawler.domain
        metadata["temporal"] = self._parse_temporal_range(row.get("COBERTURA TEMPORAL"))
        metadata["geo"] = utils.split_multivalue(row.get("COBERTURA GEOGRÁFICA"))
        metadata["relatedResources"] = row.get("RECURSOS RELACIONADOS")
        metadata["conformsTo"] = row.get("NORMATIVA")
        metadata["valid"] = row.get("VIGENCIA DEL RECURSO")

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
