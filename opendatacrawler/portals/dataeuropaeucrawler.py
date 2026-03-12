import requests
import json
from datetime import datetime

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class DataEuropaEuCrawler():
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
        
    def get_package_list(self, limit=1000):
        ids = []
        url = f"{self.odcrawler.domain}/api/hub/search"

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        params = {
            "q": "",
            "filters": "dataset,resource",
            "resource": "editorial-content",
            "facets": json.dumps({
                "country": [self.odcrawler.current_country] if self.odcrawler.current_country else [],
                "catalog": [],
                "format": [],
                "scoring": [],
                "license": [],
                "categories": [],
                "publisher": [],
                "subject": [],
                "keywords": [],
                "is_hvd": [],
                "hvdCategory": [],
                "superCatalog": [],
                "mostLiked": []
            }),
            "limit": limit,
            "scroll": "true"
        }

        try:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(f"{url}/search", self.odcrawler.user_agent, headers=headers, params=params)
            if not response:
                logger("ERROR", f"Error fetching package list from '{self.odcrawler.get_print_domain()}': no working User-Agent found")
                return ids

            response.raise_for_status()
            payload = response.json()
            data = payload.get("result") or {}

            ids.extend(item["id"] for item in data.get("results", []))

            scroll_id = data.get("scrollId")
            if not scroll_id:
                logger("ERROR", f"Error fetching package list from '{self.odcrawler.get_print_domain()}': no 'scroll_id'")
                return ids

            while True:
                response, self.odcrawler.user_agent = self.odcrawler.make_request(f"{url}/scroll", self.odcrawler.user_agent, params={"scrollId": scroll_id})
                if not response:
                    break

                response.raise_for_status()
                payload = response.json()
                data = payload.get("result") or {}

                batch = data.get("results", [])
                if not batch:
                    break

                ids.extend(item["id"] for item in batch)
                scroll_id = data.get("scrollId")

            logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.get_print_domain()}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.odcrawler.get_print_domain()}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.odcrawler.get_print_domain()}'", e)

        return ids
    
    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("id")

        resource["name"] = utils.extract_multilang_field(resource_meta.get("title", {}))
    
        resource["downloadURL"] = utils.fix_url((resource_meta.get("access_url") or [None])[0])

        meta_media_type = resource_meta.get("format")
        if isinstance(meta_media_type, dict):
            meta_media_type = meta_media_type.get("label")

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
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    logger("WARNING", f"Non-retryable error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
                    metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(error_tag))
                    metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                    return metadata
                else:
                    logger("WARNING", f"Retryable error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
            else:
                logger("ERROR", f"Error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
            return None

        payload = response.json()
        data = payload.get("result") or {}

        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/data/datasets/{package_id}")

        metadata["title"] = utils.extract_multilang_field(data.get("title", {}))
        metadata["description"] = utils.extract_multilang_field(data.get("description", {}))

        distributions = data.get("distributions", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        catalog = data.get("catalog", {})

        publisher = catalog.get("publisher", {})
        metadata["publisher"] = {
            "identifier": publisher.get("id", ""),
            "title": publisher.get("name", ""),
            "homepage": publisher.get("homepage", ""),
        }

        metadata["language"] = [lang.get("id") for lang in catalog.get("language", []) if isinstance(lang, dict)]

        metadata["keyword"] = [kw.get("label") for kw in data.get("keywords", []) if isinstance(kw, dict)]

        metadata["theme"] = [cat.get("label") for cat in data.get("categories", []) if isinstance(cat, dict)]

        temporal = data.get("temporal")
        if isinstance(temporal, dict):
            metadata["temporal"] = {
                "startDate": temporal.get("startDate"),
                "endDate": temporal.get("endDate"),
            }

        metadata["issued"] = (data.get("issued") or data.get("catalog_record", {}).get("issued", ""))
        metadata["modified"] = (data.get("modified") or data.get("catalog_record", {}).get("modified", ""))

        metadata["license"] = None

        if data.get("spatial_resource"):
            metadata["spatial"] = data["spatial"]

        if distributions:
            metadata["license"] = distributions[0].get("license", "")
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = data

        metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = (datetime.now().isoformat())

        return metadata
