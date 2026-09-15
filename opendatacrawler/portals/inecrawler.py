import json
import os
import time
import threading
from datetime import datetime, timezone

import requests

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class INECrawler:
    DATA_TYPE_ALIASES = {"xls": "xlsx"}
    DEFAULT_DATA_TYPES = ["json"]
    SUPPORTS_CATEGORY_FILTER = False
    API_PATH = "/wstempus/js/ES"
    OPERATIONS_ENDPOINT = "OPERACIONES_DISPONIBLES"
    TABLES_ENDPOINT = "TABLAS_OPERACION/{operation_code}"
    TABLE_DATA_ENDPOINT = "DATOS_TABLA/{table_id}"
    PERIODICITIES_ENDPOINT = "PERIODICIDADES"
    TABLE_PAGE_URL = "https://www.ine.es/jaxiT3/Tabla.htm?t={table_id}"
    INE_LOGO_URL = "https://www.ine.es/favicon.ico"
    EXPORT_FORMATS = {
        "json": {
            "extension": "json",
            "media_type": "application/json",
            "url": "https://servicios.ine.es/wstempus/jsCache/es/DATOS_TABLA/{table_id}?tip=AM&",
        },
        "csv": {
            "extension": "csv",
            "media_type": "text/csv",
            "url": "https://www.ine.es/jaxiT3/files/t/es/csv_bd/{table_id}.csv",
        },
        "xlsx": {
            "extension": "xlsx",
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "url": "https://www.ine.es/jaxiT3/files/t/es/xlsx/{table_id}.xlsx",
        },
        "px": {
            "extension": "px",
            "media_type": "text/x-pcaxis",
            "url": "https://www.ine.es/jaxiT3/files/t/es/px/{table_id}.px",
        },
    }
    SUPPORTED_DATA_TYPES = set(EXPORT_FORMATS)

    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
        self._tables_by_id = None
        self._periodicities_by_id = None
        self._tables_lock = threading.Lock()
        self.cache_path = os.path.join(self.odcrawler.base_domain_path, ".cache", "ine_tables.json")
        self.odcrawler.serial_metadata_phase = False

    def _api_url(self, endpoint, **values):
        path = endpoint.format(**values)
        return utils.fix_url(f"https://servicios.ine.es{self.API_PATH}/{path}")

    def _request_json(self, url):
        response, self.odcrawler.user_agent = self.odcrawler.make_request(
            url,
            self.odcrawler.user_agent,
            headers={"Accept": "application/json", "Connection": "keep-alive"},
            max_sec=self.odcrawler.max_sec,
        )
        return utils.read_json(response, f"reading INE endpoint {url}")

    @staticmethod
    def _timestamp_to_iso(value):
        if value in (None, "", "null"):
            return None
        try:
            timestamp = float(value) / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return str(value)

    def _load_tables(self, limit=None):
        with self._tables_lock:
            return self._load_tables_locked(limit=limit)

    def _load_tables_locked(self, limit=None):
        if self._tables_by_id is not None:
            return self._tables_by_id

        ttl = utils.get_config_option("ine", "catalog_ttl_seconds", int, 3600)
        try:
            with open(self.cache_path) as source:
                cached = json.load(source)
            if limit is None and cached.get("origin") == "https://servicios.ine.es" and time.time() - cached.get("checked_at", 0) < ttl and isinstance(cached.get("tables"), dict):
                self._tables_by_id = cached["tables"]
                return self._tables_by_id
        except (OSError, ValueError):
            pass
        tables_by_id = {}
        operations_url = self._api_url(self.OPERATIONS_ENDPOINT)
        operations = self._request_json(operations_url)
        if not isinstance(operations, list) or not operations:
            raise utils.CatalogError("INE operations response is invalid or empty")

        for operation in operations:
            if not isinstance(operation, dict) or not operation.get("Codigo"):
                continue

            operation_code = str(operation["Codigo"]).strip()
            tables_url = self._api_url(self.TABLES_ENDPOINT, operation_code=operation_code)
            tables = self._request_json(tables_url)
            if not isinstance(tables, list):
                raise utils.CatalogError(f"Invalid tables response for INE operation {operation_code}")

            for table in tables:
                if not isinstance(table, dict) or table.get("Id") is None:
                    continue
                table_id = str(table["Id"])
                tables_by_id[table_id] = {
                    **table,
                    "operation": operation,
                    "operation_code": operation_code,
                }
                if limit and len(tables_by_id) >= limit:
                    self._tables_by_id = tables_by_id
                    return self._tables_by_id

        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        if not utils.atomic_dump_json(self.cache_path, {"origin": "https://servicios.ine.es", "checked_at": time.time(), "tables": tables_by_id}):
            raise utils.CatalogError("Could not persist INE table index")
        self._tables_by_id = tables_by_id
        return self._tables_by_id

    def _load_periodicities(self):
        if self._periodicities_by_id is not None:
            return self._periodicities_by_id
        periodicities = self._request_json(self._api_url(self.PERIODICITIES_ENDPOINT))
        if isinstance(periodicities, list):
            self._periodicities_by_id = {item.get("Id"): item for item in periodicities if isinstance(item, dict) and item.get("Id") is not None}
        else:
            self._periodicities_by_id = {}
        return self._periodicities_by_id

    def _temporal_from_table(self, table):
        start = table.get("Anyo_Periodo_ini")
        end = table.get("FechaRef_fin")
        return {"startDate": str(start) if start not in (None, "", "null") else None, "endDate": str(end) if end not in (None, "", "null") else None}

    def _detect_packages_requiring_refresh(self, tables_by_id):
        refresh_packages = []
        for table_id, table in (tables_by_id or {}).items():
            remote_modified = self._timestamp_to_iso(table.get("Ultima_Modificacion"))
            if not remote_modified:
                continue

            metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.odcrawler.domain}_{table_id}')}.json"
            metadata_path = f"{self.odcrawler.save_path}/{metadata_file_name}"
            try:
                with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                    local_modified = json.load(metadata_file).get("modified")
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError):
                refresh_packages.append(table_id)
                continue

            if local_modified != remote_modified:
                refresh_packages.append(table_id)

        if refresh_packages:
            logger("INFO", f"Detected {len(refresh_packages)} existing INE table(s) with remote updates; they will be refreshed in this run", level="print")
        return refresh_packages

    def get_package_list(self):
        tables_by_id = self._load_tables(limit=self.odcrawler.max_packages)
        if self.odcrawler.check_remote_updates:
            self.odcrawler.set_packages_requiring_refresh(self._detect_packages_requiring_refresh(tables_by_id))
        else:
            self.odcrawler.set_packages_requiring_refresh([])

        ids = list(tables_by_id.keys())
        logger("OK", f"Retrieved {len(ids)} tables from '{self.odcrawler.get_print_domain()}'", level="print")
        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {
            "fileName": base_name,
            "id": resource_meta.get("id"),
            "name": resource_meta.get("name"),
            "description": resource_meta.get("description"),
            "issued": resource_meta.get("issued"),
            "modified": resource_meta.get("modified"),
            "accessURL": utils.fix_url(resource_meta.get("accessURL")),
            "downloadURL": utils.fix_url(resource_meta.get("downloadURL") or resource_meta.get("accessURL")),
        }
        return resource, resource_meta.get("mediaType") or "application/json"

    def _requested_export_formats(self):
        requested_types = [
            str(data_type).strip().lower()
            for data_type in (self.odcrawler.data_types or [])
            if str(data_type).strip()
        ]
        if not requested_types:
            return ["json"]

        formats = []
        for data_type in requested_types:
            if data_type == "xls":
                data_type = "xlsx"
            if data_type in self.EXPORT_FORMATS and data_type not in formats:
                formats.append(data_type)
        return formats

    def _build_distributions(self, package_id, table_name, modified):
        distributions = []
        for format_name in self._requested_export_formats():
            format_info = self.EXPORT_FORMATS[format_name]
            if format_name == "json":
                download_url = format_info["url"].format(table_id=package_id)
            else:
                download_url = format_info["url"].format(domain=self.odcrawler.domain, table_id=package_id)

            distributions.append({
                "id": f"{package_id}:{format_name}",
                "name": f"{table_name} ({format_name})",
                "description": f"Datos de la tabla {table_name} en formato {format_name.upper()}.",
                "issued": None,
                "modified": modified,
                "mediaType": format_info["media_type"],
                "accessURL": download_url,
                "downloadURL": download_url,
            })
        return distributions

    def get_package(self, package_id, metadata_file_name):
        table = self._load_tables().get(str(package_id))
        if not table:
            logger("WARNING", f"No INE table found for package '{package_id}'")
            return None

        operation = table.get("operation") or {}
        operation_name = operation.get("Nombre") or table.get("operation_code") or "INE"
        table_name = table.get("Nombre") or f"Tabla {package_id}"
        data_url = self._api_url(self.TABLE_DATA_ENDPOINT, table_id=package_id)
        access_url = self.TABLE_PAGE_URL.format(table_id=package_id)
        modified = self._timestamp_to_iso(table.get("Ultima_Modificacion"))
        periodicity = self._load_periodicities().get(table.get("FK_Periodicidad"), {})

        metadata = utils.init_metadata()
        metadata["identifier"] = str(package_id)
        metadata["accessURL"] = access_url
        metadata["requestURL"] = data_url
        metadata["fileName"] = metadata_file_name
        metadata["img"] = self.INE_LOGO_URL
        metadata["title"] = {"es": [table_name]}
        metadata["description"] = {}
        metadata["publisher"] = {
            "identifier": "INE",
            "title": "Instituto Nacional de Estadística",
            "homepage": "https://www.ine.es",
        }
        metadata["language"] = ["es"]
        metadata["keyword"] = [value for value in (operation_name, table.get("operation_code"), operation.get("Cod_IOE")) if value]
        metadata["theme"] = []
        metadata["modified"] = modified
        metadata["issued"] = None
        metadata["license"] = "https://creativecommons.org/licenses/by/4.0/"
        metadata["source"] = self.odcrawler.domain
        metadata["sourceIdentifier"] = table.get("Codigo")
        metadata["temporal"] = self._temporal_from_table(table)
        metadata["accrualPeriodicity"] = {
            "id": table.get("FK_Periodicidad"),
            "label": periodicity.get("Nombre"),
            "code": periodicity.get("Codigo"),
            "source": "INE",
        }
        if operation.get("Url"):
            operation = {**operation, "accessURL": utils.fix_url(f"https://www.ine.es{operation['Url']}")}
        metadata["operation"] = operation

        distributions = self._build_distributions(package_id, table_name, modified)
        self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = table

        return metadata
