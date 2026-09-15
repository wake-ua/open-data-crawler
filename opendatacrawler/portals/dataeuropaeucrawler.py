import requests
import json
from datetime import datetime

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class DataEuropaEuCrawler():
    def _clean_empty_text(self, value):
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return ""
        if isinstance(value, dict):
            return {k: self._clean_empty_text(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean_empty_text(v) for v in value]
        return value

    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
    def get_package_list(self, limit=1000):
        max_packages = self.odcrawler.max_packages
        page_limit = min(limit, max_packages) if max_packages else limit
        base = f"{self.odcrawler.domain}/api/hub/search"
        params = {"q": "", "filters": "dataset", "limit": page_limit, "scroll": "true",
                  "facets": json.dumps({"country": [self.odcrawler.current_country] if self.odcrawler.current_country else []})}
        ids, seen = [], set()
        endpoint = "search"
        expected = None
        while True:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(f"{base}/{endpoint}", self.odcrawler.user_agent, params=params)
            payload = utils.read_json(response, "enumerating data.europa.eu")
            data = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise utils.CatalogError("EU search response lacks result.results")
            if expected is None:
                count = data.get("count")
                expected = count if isinstance(count, int) else None
            batch = data["results"]
            if not batch:
                break
            batch_ids = [str(item["id"]) for item in batch if isinstance(item, dict) and item.get("id")]
            if len(batch_ids) != len(batch) or any(key in seen for key in batch_ids) or len(set(batch_ids)) != len(batch_ids):
                raise utils.CatalogError("EU scroll repeated or malformed dataset IDs")
            ids.extend(batch_ids); seen.update(batch_ids)
            if max_packages and len(ids) >= max_packages:
                return ids[:max_packages]
            scroll_id = data.get("scrollId")
            if not scroll_id:
                if expected == len(ids):
                    break
                raise utils.CatalogError("EU scroll missing cursor before completion")
            endpoint = "scroll"
            params = {"scrollId": scroll_id}
        if expected is not None and len(ids) != expected:
            raise utils.CatalogError(f"EU catalog incomplete: expected {expected}, received {len(ids)}")
        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("id")
        resource["name"] = utils.extract_multilang_field(resource_meta.get("title", {}))
        resource["description"] = utils.extract_multilang_field(self._clean_empty_text(resource_meta.get("description", {})))
        resource["accessURL"] = utils.fix_url(next(iter(utils.flatten_labels(resource_meta.get("access_url"))), None))
        resource["downloadURL"] = utils.fix_url(next(iter(utils.flatten_labels(resource_meta.get("download_url"))), None) or resource["accessURL"])
        resource["issued"] = resource_meta.get("issued")
        resource["modified"] = resource_meta.get("modified")
        resource["remoteSize"] = resource_meta.get("byte_size")
        resource["license"] = resource_meta.get("license")
        if resource_meta.get("checksum"):
            resource["checksum"] = resource_meta.get("checksum")

        meta_media_type = resource_meta.get("media_type")
        format_data = resource_meta.get("format")
        if isinstance(format_data, dict):
            resource["format"] = {
                "identifier": format_data.get("id"),
                "title": format_data.get("label"),
                "resource": format_data.get("resource"),
                "types": format_data.get("format_types"),
            }
            if not meta_media_type:
                meta_media_type = format_data.get("label")
        elif not meta_media_type:
            meta_media_type = format_data

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        url = f"{self.odcrawler.domain}/api/hub/search/datasets/{package_id}"
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://european-union.europa.eu/themes/contrib/oe_theme/dist/eu/images/logo/standard-version/positive/logo-eu--en.svg"
        
        response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=headers, return_tag=True)
        if not response:
            error_message = utils.extract_error_message(e)
            tag_values = utils.extract_error_tag_values(e)
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    logger("WARNING", f"Non-retryable error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
                    metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(error_tag, tag_values))
                    metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                    return metadata
                else:
                    logger("WARNING", f"Retryable error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
            else:
                logger("ERROR", f"Error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
            return None

        payload = utils.read_json(response, f"reading EU dataset {package_id}")
        data = payload.get("result")
        if not isinstance(data, dict) or not data.get("id"):
            raise utils.CatalogError("EU dataset response lacks result.id")

        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/data/datasets/{package_id}")
        metadata["sourceIdentifier"] = data.get("resource")
        metadata["alternateIdentifiers"] = data.get("identifier", [])
        metadata["landingPage"] = next(iter(utils.flatten_labels(data.get("landing_page"))), None)

        metadata["title"] = utils.extract_multilang_field(data.get("title", {}))
        metadata["description"] = utils.extract_multilang_field(self._clean_empty_text(data.get("description", {})))

        distributions = data.get("distributions", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        catalog = data.get("catalog", {})

        metadata["catalog"] = catalog
        publisher = data.get("publisher") or {}
        metadata["publisher"] = {
            "identifier": publisher.get("id") or publisher.get("resource", ""),
            "title": publisher.get("name", ""),
            "homepage": publisher.get("homepage", ""),
            "type": publisher.get("type"),
        }

        metadata["language"] = [lang.get("id") for lang in data.get("language", []) if isinstance(lang, dict)]

        metadata["keyword"] = [kw.get("label") for kw in data.get("keywords", []) if isinstance(kw, dict)]

        metadata["theme"] = [cat.get("label") for cat in data.get("categories", []) if isinstance(cat, dict)]
        metadata["accessRights"] = data.get("access_right")
        metadata["accrualPeriodicity"] = data.get("accrual_periodicity")
        metadata["isHvd"] = data.get("is_hvd")
        metadata["quality"] = data.get("quality_meas")
        metadata["country"] = data.get("country")
        metadata["corporateBodyClassification"] = data.get("corporate-body-classification")

        metadata["temporal"] = utils.temporal_intervals(data.get("temporal"))

        metadata["issued"] = (data.get("issued") or data.get("catalog_record", {}).get("issued", ""))
        metadata["modified"] = (data.get("modified") or data.get("catalog_record", {}).get("modified", ""))

        metadata["license"] = None

        if data.get("spatial_resource") or data.get("spatial") or data.get("country"):
            metadata["spatial"] = data.get("spatial") or data.get("spatial_resource") or data.get("country")

        if distributions:
            metadata["license"] = distributions[0].get("license", "")
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = data

        metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = (datetime.now().isoformat())

        return metadata
