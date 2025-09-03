import requests
from urllib.parse import urlparse
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class DatosGobEsCrawler():
    def __init__(self, domain, data_types, user_agent):
        self.domain = domain.rstrip("/")
        self.data_types = data_types
        self.user_agent = user_agent

    def get_package_list(self):
        ids = []
        url = "http://datos.gob.es/virtuoso/sparql"

        params = {
            "query": "SELECT DISTINCT ?dataset WHERE {?dataset a <http://www.w3.org/ns/dcat#Dataset>}"
        }

        headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": self.user_agent,
            "Connection": "keep-alive"
        }

        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers, params=params)
            if not response:
                logger("ERROR", f"Error fetching package list from '{self.domain}': no working User-Agent found")
                return ids

            response.raise_for_status()

            for result in response.json().get("results", {}).get("bindings", []):
                ids.append(result.get("dataset", {}).get("value").split("/")[-1])

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}'", e)

        return ids

    def parse_resource(self, data, base_name):
        resource = {}

        resource["name"] = utils.extract_multilang_field(data.get("title", []), "_lang", "_value")

        resource["downloadURL"] = data.get("accessURL") or data.get("downloadURL")

        resource["mediaType"] = data.get("format", {}).get("value")
        resource["fileName"] = utils.generate_short_filename(base_name, ext=utils.get_extension_mime(resource["mediaType"]))

        return resource

    def get_package(self, dataset_id, metadata_file_name):
        url = f"https://datos.gob.es/apidata/catalog/dataset/{dataset_id}"
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
        if not response:
            logger("ERROR", f"No working User-Agent for URL '{url}'")
            return None

        response.raise_for_status()

        items = response.json()["result"].get("items", [])
        if not items:
            logger("WARNING", f"No data returned for package '{dataset_id}'")
            return None

        data = items[0]
        metadata = {}

        metadata["identifier"] = dataset_id
        metadata["accessURL"] = f"https://datos.gob.es/es/catalogo/{dataset_id}"
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name

        metadata["img"] = "https://datos.gob.es/sites/default/files/favicon.png"

        metadata["title"] = utils.extract_multilang_field(data.get("title", []), "_lang", "_value")
        metadata["description"] = utils.extract_multilang_field(data.get("description", []), "_lang", "_value")

        distributions = data.get("distribution", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        metadata["publisher"] = utils.extract_mapped_field(data.get("publisher"), utils.DATOSGOBESCRAWLER_PUBLISHER_MAP)[0]

        download_url = distributions[0].get("accessURL") or distributions[0].get("downloadURL")
        if download_url:
            metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language")

        metadata["keyword"] = utils.extract_multilang_field(data.get("keyword", []), "_lang", "_value")

        metadata["theme"] = utils.extract_mapped_field(data.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)

        metadata["accrualPeriodicity"] = {k: v for k, v in data.get("accrualPeriodicity", {}).get("value", {}).items() if k != "_about"}
        metadata["modified"] = data.get("modified")
        metadata["issued"] = data.get("issued")
        metadata["license"] = data.get("license")
        metadata["source"] = self.domain

        temporal = data.get("temporal", {})
        if isinstance(temporal, dict):
            metadata["temporal"] = {
                "startDate": temporal.get("startDate") or temporal.get("start_date"),
                "endDate": temporal.get("endDate") or temporal.get("end_date"),
            }

        metadata["geo"] = utils.extract_mapped_field(data.get("spatial"), utils.DATOSGOBESCRAWLER_SPATIAL_MAP)

        resource_list = []
        for idx, res in enumerate(distributions):
            resource_list.append(self.parse_resource(res, f"{metadata["fileName"]}_{idx}"))

        metadata["resources"] = resource_list

        return metadata
