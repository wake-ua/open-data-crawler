import requests
from opendatacrawler import utils
from datetime import datetime
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class GbifCrawler():
    SUPPORTED_DATA_TYPES = {"zip"}
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

    def get_package_list(self):
        ids, seen = [], set()
        offset = 0
        expected = None
        while True:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(
                f"{self.odcrawler.domain}/v1/dataset/search", self.odcrawler.user_agent,
                params={"limit": 100, "offset": offset})
            data = utils.read_json(response, "enumerating GBIF")
            if not isinstance(data, dict) or not isinstance(data.get("count"), int) or not isinstance(data.get("results"), list):
                raise utils.CatalogError("Invalid GBIF search envelope")
            if expected is None:
                expected = data["count"]
            if expected != data["count"]:
                raise utils.CatalogError("GBIF catalog count changed during enumeration; retry")
            batch = [str(item["key"]) for item in data["results"] if item.get("key")]
            if len(batch) != len(data["results"]) or any(key in seen for key in batch) or len(set(batch)) != len(batch):
                raise utils.CatalogError("GBIF duplicate/missing keys during pagination")
            ids.extend(batch); seen.update(batch)
            if data.get("endOfRecords") or len(ids) >= expected:
                break
            if not batch:
                raise utils.CatalogError("GBIF returned an empty page before completion")
            offset += len(batch)
        if len(ids) != expected:
            raise utils.CatalogError(f"GBIF catalog incomplete: {len(ids)}/{expected}")
        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("key")
        resource["endpointType"] = resource_meta.get("type")
        resource["name"] = resource_meta.get("name") or resource_meta.get("key")
        resource["description"] = resource_meta.get("description")

        resource["downloadURL"] = utils.fix_url(resource_meta.get("url"))

        meta_media_type = "application/zip" if resource_meta.get("type") == "DWC_ARCHIVE" else resource_meta.get("format", "")

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.odcrawler.domain}/v1/dataset/{package_id}")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://images.ctfassets.net/uo17ejk9rkwj/4rmEF9F4ZGiCwQk2WSMcce/b47146eacaf7b0dfc166656678c7ffe6/GBIF-2015.png"

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

        data = utils.read_json(response, f"reading GBIF dataset {package_id}")
        if not isinstance(data, dict) or not data.get("key"):
            raise utils.CatalogError("GBIF dataset response lacks key")

        metadata["accessURL"] = utils.fix_url(f"https://www.gbif.org/dataset/{package_id}")

        metadata["title"] = data.get("title", {})
        metadata["description"] = data.get("description", {})

        language = data.get("language", [])
        language = [language] if isinstance(language, str) else language
        data_language = data.get("dataLanguage", [])
        data_language = [data_language] if isinstance(data_language, str) else data_language
        metadata["language"] = list(set(language + data_language))

        metadata["doi"] = data.get("doi", "")

        publisher = next((contact for contact in data.get("contacts", []) if contact.get("type") == "ORIGINATOR"),{})
        metadata["publisher"] = {
            "identifier": publisher.get("key", ""),
            "title": utils.extract_first_nonempty_value(publisher.get("organization", "")),
            "homepage": data.get("homepage", ""),
        }

        distributions = []
        endpoints_res = data.get("endpoints", [])
        for endpoint in endpoints_res:
            if endpoint.get("type") == "DWC_ARCHIVE":
                distributions.append(endpoint)

        data_descriptions_res = data.get("dataDescriptions", [])
        for dd_res in data_descriptions_res:
            if dd_res.get("url"):
                distributions.append(dd_res)

        metadata["theme"] = data.get("tags", [])

        metadata["keyword"] = []
        keywords = data.get("keywords", [])
        if isinstance(keywords, list):
            metadata["keyword"].extend(keywords)
        for collection in data.get("keywordCollections", []):
            keywords = collection.get("keywords", [])
            if isinstance(keywords, list):
                metadata["keyword"].extend(keywords)
        metadata["keyword"] = list(dict.fromkeys(metadata["keyword"]))

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("modified", "")
        metadata["issued"] = data.get("created", "")
        metadata["license"] = data.get("license", "")

        metadata["source"] = self.odcrawler.domain

        metadata["temporal"] = utils.temporal_intervals(data.get("temporalCoverages"))
        metadata["endpoints"] = data.get("endpoints", [])

        spatials = data.get("geographicCoverages", [])
        metadata["geo"] = []
        metadata["spatial"] = []
        for spatial in spatials:
            if isinstance(spatial, dict):
                description = spatial.get("description")
                bbox = spatial.get("boundingBox")
                if description:
                    metadata["geo"].append(description)
                if bbox and not bbox.get("globalCoverage"):
                    metadata["spatial"].append(bbox)

        if distributions:
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        metadata["distributions"] = distributions
        if self.odcrawler.save_raw_data:
            metadata["rawData"] = data

        return metadata
