import requests
import os
import json
import re
from tqdm import tqdm
from datetime import datetime, date, timedelta
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class ZenodoCrawler():
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

        reqs_per_sec = utils.get_config_option("zenodo", "reqs_per_sec", cast=float, fallback=5000/3600)
        self.odcrawler.init_rate_limit(reqs_per_sec=reqs_per_sec)

        self.token = utils.AUTH_TOKENS.get("zenodo")
        #self.ns = {
        #    "oai": "http://www.openarchives.org/OAI/2.0/",
        #    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        #    "dct": "http://purl.org/dc/terms/",
        #    "dcat": "http://www.w3.org/ns/dcat#",
        #    "foaf": "http://xmlns.com/foaf/0.1/",
        #}

    def get_state_file(self, url, path=utils.ZENODO_STATE_FILE):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            ids = set(state.get("ids", []))
            last_date = date.fromisoformat(state.get("lastDate"))
            last_hour = state.get("lastHour", 0)
        else:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, params={"size": 1, "sort": "oldest"}, max_sec=self.odcrawler.max_sec)
            if not response:
                return set(), date.today(), 0

            hits = response.json().get("hits", {}).get("hits", [])
            ids = set()
            if not hits:
                return ids, date.today(), 0

            last_date = datetime.fromisoformat(hits[0]["created"].replace("Z", "+00:00")).date()
            last_hour = 0

        return ids, last_date, last_hour

    def get_package_list(self):
        url = f"{self.odcrawler.domain}/api/records?type=dataset"
        ids, last_date, last_hour = self.get_state_file(url)

        page_size = 25
        last_max_date = None
        try:
            while True:
                response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, params={"size": 1, "sort": "newest"}, max_sec=self.odcrawler.max_sec)
                if not response:
                    break

                hits = response.json().get("hits", {}).get("hits", [])
                if not hits:
                    break

                max_date = datetime.fromisoformat(hits[0]["created"].replace("Z", "+00:00")).date()

                if last_max_date == max_date:
                    break

                last_max_date = max_date
                total_hours = ((max_date - last_date).days * 24) + (24 - last_hour)
                with tqdm(total=total_hours, desc="Fetching packages IDs...", colour="blue") as pbar:
                    while last_date <= max_date:
                        last_successful_hour = None
                        for hour in range(last_hour, 24):
                            hour_start = f"{last_date}T{hour:02d}:00:00Z"
                            hour_end = f"{last_date}T{hour:02d}:59:59Z"

                            page = 1
                            
                            while True:
                                params = {
                                    "page": page,
                                    "size": page_size,
                                    "sort": "oldest",
                                    "q": f"created:[{hour_start} TO {hour_end}]"
                                }

                                response, self.odcrawler.user_agent, tag, e = self.odcrawler.make_request(url, self.odcrawler.user_agent, params=params, max_sec=self.odcrawler.max_sec, return_tag=True)
                                if not response:
                                    if tag == "resource_temporarily_unavailable":
                                        page_size = max(page_size // 2, 10)
                                    continue

                                hits = response.json().get("hits", {}).get("hits", [])
                                if not hits:
                                    break
                                
                                last_successful_hour = hour
                                for record in hits:
                                    if record["id"] not in ids:
                                        ids.add(record["id"])

                                if len(hits) < page_size:
                                    break

                                page += 1

                            if last_date == max_date and last_successful_hour is not None:
                                last_hour = last_successful_hour - 1 if last_successful_hour > 0 else 0
                            else:
                                last_hour = 0

                            state = {
                                "lastDate": last_date.isoformat(),
                                "lastHour" : last_hour, 
                                "totalIds": len(ids),
                                "ids": list(ids)
                            }

                            if last_date == max_date and last_successful_hour is not None:
                                state["lastHour"] = last_successful_hour
                            else:
                                state["lastHour"] = hour + 1 if hour < 23 else 0

                            utils.atomic_dump_json(utils.ZENODO_STATE_FILE, state, indent=2)

                            pbar.update(1)

                        last_date += timedelta(days=1)
                        last_hour = 0

            logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.get_print_domain()}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.odcrawler.get_print_domain()}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.odcrawler.get_print_domain()}'", e)

        return list(ids)

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["identifier"] = resource_meta.get("id", None)
        resource["name"] = resource_meta.get("key", None)

        resource["downloadURL"] = utils.fix_url(resource_meta.get("links", {}).get("self", None))

        meta_media_type = resource_meta.get("key", None)
        if meta_media_type:
            ext = meta_media_type.split(".")[-1].lower() if "." in meta_media_type else None
            meta_media_type = utils.EXT_TO_MIME.get(ext)

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.odcrawler.domain}/api/records/{package_id}")

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Authorization": f"Bearer {self.token}"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://zenodo.org/static/images/invenio-rdm.svg"

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

        data = response.json()

        metadata["title"] = data.get("metadata", {}).get("title", {})
        metadata["description"] = data.get("metadata", {}).get("description", {})
        metadata["doi"] = data.get("metadata", {}).get("doi", {})

        distributions = data.get("files", {})
        if not isinstance(distributions, list):
            distributions = [distributions]

        metadata["publisher"] = data.get("metadata", {}).get("creators", {})

        metadata["language"] = data.get("metadata", {}).get("language", {})

        keywords = data.get("metadata", {}).get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords_list = []
        if isinstance(keywords, list):
            for item in keywords:
                if isinstance(item, str):
                    for keyword in re.split(r"[;,]", item):
                        kw = keyword.strip()
                        if kw:
                            keywords_list.append(kw)
        metadata["keyword"] = keywords_list

        #metadata["theme"] = []
        #metadata["accrualPeriodicity"] = None

        metadata["modified"] = data.get("modified", None)
        #metadata["issued"] = None

        metadata["license"] = data.get("metadata", {}).get("license", {}).get("id", None)

        metadata["source"] = self.odcrawler.domain

        #metadata["temporal"] = None
        #metadata["spatial"] = None

        if distributions:
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["dataRaw"] = data

        return metadata
