import requests
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

class CkanCrawler:
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler
        self.token = utils.AUTH_TOKENS.get("ckan")

    def _normalize_ckan_timestamp(self, value):
        if not value:
            return None

        text = str(value).strip()
        if not text:
            return None

        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)

    def _get_metadata_path(self, package_name):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.odcrawler.domain}_{package_name}')}.json"
        return metadata_file_name, os.path.join(self.odcrawler.save_path, metadata_file_name)

    def _load_local_package_modified(self, package_name):
        metadata_file_name, metadata_path = self._get_metadata_path(package_name)
        if not os.path.exists(metadata_path):
            return None, False, False

        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            return metadata.get("modified"), True, False
        except Exception as e:
            logger("WARNING", f"Could not read local metadata file '{metadata_file_name}' while checking for CKAN updates; the package will be refreshed", e)
            return None, True, True

    def _fetch_package_list_names(self, headers):
        try:
            resp, self.odcrawler.user_agent, err_tag, err = self.odcrawler.make_action_request(
                "package_list",
                params={},
                headers=headers,
                return_tag=True,
                current_agent=self.odcrawler.user_agent,
                max_sec=self.odcrawler.max_sec,
            )
            if not resp:
                logger("ERROR", f"Error fetching package_list from '{self.odcrawler.get_print_domain()}'", err)
                return []

            payload = resp.json()
            if not payload.get("success", False):
                logger("ERROR", f"CKAN package_list returned success=false: {payload.get('error')}")
                return []

            ids = payload.get("result") or []
            return ids if isinstance(ids, list) else []
        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package_list from '{self.odcrawler.get_print_domain()}'", e)
            return []
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing package_list from '{self.odcrawler.get_print_domain()}'", e)
            return []

    def _fetch_package_search_results(self, headers, include_private=False, fields=None):
        headers = {
            **headers,
        }
        results = []
        rows = 100
        start = 0

        while True:
            params = {
                "q": "*:*",
                "rows": rows,
                "start": start,
            }
            if include_private:
                params["include_private"] = True
            if fields:
                params["fl"] = ",".join(fields)

            try:
                resp, self.odcrawler.user_agent, err_tag, err = self.odcrawler.make_action_request(
                    "package_search",
                    params=params,
                    headers=headers,
                    return_tag=True,
                    current_agent=self.odcrawler.user_agent,
                    max_sec=self.odcrawler.max_sec,
                )

                if not resp:
                    logger("ERROR", f"Error fetching package_search from '{self.odcrawler.get_print_domain()}'", err)
                    break

                payload = resp.json()
                if not payload.get("success", False):
                    logger("ERROR", f"CKAN package_search returned success=false: {payload.get('error')}")
                    break

                result = payload.get("result") or {}
                count = int(result.get("count") or 0)
                batch = [ds for ds in (result.get("results") or []) if isinstance(ds, dict) and ds.get("name")]
                results.extend(batch)

                start += rows
                if start >= count or not batch:
                    break

            except requests.RequestException as e:
                logger("ERROR", f"Error fetching package_search from '{self.odcrawler.get_print_domain()}'", e)
                break
            except Exception as e:
                logger("ERROR", f"Unexpected error parsing package_search from '{self.odcrawler.get_print_domain()}'", e)
                break

        return results

    def _detect_packages_requiring_refresh(self, package_summaries):
        refresh_packages = []

        for package_summary in package_summaries or []:
            package_name = package_summary.get("name")
            remote_modified = self._normalize_ckan_timestamp(package_summary.get("metadata_modified"))
            if not package_name or remote_modified is None:
                continue

            local_modified, local_exists, local_read_error = self._load_local_package_modified(package_name)
            if not local_exists:
                continue
            if local_read_error:
                refresh_packages.append(package_name)
                continue

            local_modified = self._normalize_ckan_timestamp(local_modified)
            if local_modified is None or remote_modified > local_modified:
                refresh_packages.append(package_name)

        if refresh_packages:
            logger("INFO", f"Detected {len(refresh_packages)} existing CKAN package(s) with remote metadata updates; they will be refreshed in this run", level="print")

        return refresh_packages

    def _should_skip_missing_package_list_entry(self, package_name, headers):
        try:
            resp, self.odcrawler.user_agent, err_tag, err = self.odcrawler.make_action_request(
                "package_show",
                params={"id": package_name},
                headers=headers,
                return_tag=True,
                current_agent=self.odcrawler.user_agent,
                max_sec=self.odcrawler.max_sec,
            )
            if not resp:
                logger("WARNING", f"Could not inspect package '{package_name}' after it was missing from CKAN package_search; keeping it for safety", err)
                return False

            payload = resp.json()
            if not payload.get("success", False):
                logger("WARNING", f"CKAN package_show returned success=false while inspecting '{package_name}' after it was missing from package_search; keeping it for safety")
                return False

            data = payload.get("result") or {}
            package_type = str(data.get("type") or "").strip().lower()
            resources = data.get("resources") or []
            if package_type == "harvest" and not resources:
                logger("INFO", f"Skipping package '{package_name}' because it is a CKAN harvest entry without downloadable resources, not a normal dataset", level="print")
                return True
            return False
        except Exception as e:
            logger("WARNING", f"Unexpected error while inspecting package '{package_name}' after it was missing from CKAN package_search; keeping it for safety", e)
            return False

    def get_package_list(self):
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
        }

        ids = []
        package_summaries = []
        if self.odcrawler.check_remote_updates or self.token:
            package_summaries = self._fetch_package_search_results(
                headers=headers,
                include_private=bool(self.token),
                fields=["id", "name", "metadata_modified"] if self.odcrawler.check_remote_updates else None,
            )

        if self.odcrawler.check_remote_updates:
            self.odcrawler.set_packages_requiring_refresh(self._detect_packages_requiring_refresh(package_summaries))
        else:
            self.odcrawler.set_packages_requiring_refresh([])

        if self.token:
            ids = [item.get("name") for item in package_summaries if item.get("name")]
        else:
            ids = self._fetch_package_list_names(headers)
            if not ids:
                ids = [item.get("name") for item in package_summaries if item.get("name")]
            elif package_summaries:
                summary_names = {item.get("name") for item in package_summaries if item.get("name")}
                missing_from_search = [pkg_id for pkg_id in ids if pkg_id not in summary_names]
                if missing_from_search:
                    skipped_missing = [pkg_id for pkg_id in missing_from_search if self._should_skip_missing_package_list_entry(pkg_id, headers)]
                    if skipped_missing:
                        ids = [pkg_id for pkg_id in ids if pkg_id not in skipped_missing]

                    kept_missing = [pkg_id for pkg_id in missing_from_search if pkg_id not in skipped_missing]
                    if kept_missing:
                        logger("INFO", f"CKAN package_search omitted {len(kept_missing)} package(s); keeping them from package_list for compatibility", level="print")

        logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.get_print_domain()}'", level="print")
        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("id")
        resource["name"] = resource_meta.get("name")

        resource["description"] = resource_meta.get("description")
        resource["state"] = resource_meta.get("state")
        resource["modified"] = resource_meta.get("last_modified") or resource_meta.get("metadata_modified") or resource_meta.get("created")
        resource["created"] = resource_meta.get("created")
        if resource_meta.get("hash"):
            resource["remoteHash"] = resource_meta.get("hash")
        if resource_meta.get("size") is not None:
            resource["remoteSize"] = resource_meta.get("size")

        resource["downloadURL"] = utils.fix_url(resource_meta.get("download_url") or resource_meta.get("url") or resource_meta.get("original_url"))

        if resource["id"] and resource_meta.get("datastore_active"):
            response, self.odcrawler.user_agent, *_ = self.odcrawler.make_action_request(
                "datastore_search",
                params={"resource_id": resource["id"], "limit": 0},
                return_tag=False,
            )
                        
            if response:
                payload = response.json()
                schema_data = payload.get("result") if payload.get("success", False) else None
                if schema_data:
                    resource["schema_og"] = {"fields": []}
                    for field in schema_data.get("fields", []):
                        resource["schema_og"]["fields"].append({
                            "name": field.get("id"),
                            "description": field.get("info", {}).get("notes", ""),
                            "type": field.get("type")
                        })

                    resource["rawDataSchema"] = {k: v for k, v in schema_data.items() if k != "records"}

        meta_media_type = resource_meta.get("mimetype") or resource_meta.get("format")

        return resource, meta_media_type

    def _get_existing_resource_entry(self, by_id, by_name, new_file_name, new_resource):
        resource_id = new_resource.get("id")
        if resource_id and resource_id in by_id:
            return by_id[resource_id]
        return by_name.get(new_file_name)

    def _build_resource_refresh_signature(self, resource):
        if not isinstance(resource, dict):
            return resource
        return {
            "id": resource.get("id"),
            "fileName": resource.get("fileName"),
            "downloadURL": resource.get("downloadURL"),
            "mediaType": resource.get("mediaType"),
            "state": resource.get("state"),
            "modified": self._normalize_ckan_timestamp(resource.get("modified")),
            "remoteHash": resource.get("remoteHash"),
            "remoteSize": resource.get("remoteSize"),
        }

    def merge_existing_package_state(self, existing_package, package):
        if not isinstance(existing_package, dict) or not isinstance(package, dict):
            return package

        existing_resources = existing_package.get("resources", {})
        existing_resources_info = existing_package.get("crawlerInfo", {}).get("resourcesInfo", {})
        new_resources = package.get("resources", {})
        new_resources_info = package.get("crawlerInfo", {}).get("resourcesInfo", {})

        if not isinstance(existing_resources, dict) or not isinstance(existing_resources_info, dict):
            return package
        if not isinstance(new_resources, dict) or not isinstance(new_resources_info, dict):
            return package

        existing_by_id = {}
        existing_by_name = {}
        for old_file_name, old_resource in existing_resources.items():
            if not isinstance(old_resource, dict):
                continue

            old_info = existing_resources_info.get(old_file_name, utils.init_metadata(package=False, crawled=False))
            entry = (old_file_name, old_resource, old_info)
            resource_id = old_resource.get("id")
            if resource_id and resource_id not in existing_by_id:
                existing_by_id[resource_id] = entry
            existing_by_name[old_file_name] = entry

        merged_resources = {}
        merged_resources_info = {}
        preserved_resources = 0
        changed_resources = 0

        for new_file_name, new_resource in new_resources.items():
            new_info = new_resources_info.get(new_file_name, utils.init_metadata(package=False, crawled=False))
            entry = self._get_existing_resource_entry(existing_by_id, existing_by_name, new_file_name, new_resource)
            if not entry:
                merged_resources[new_file_name] = new_resource
                merged_resources_info[new_file_name] = new_info
                continue

            _, old_resource, old_info = entry
            if self._build_resource_refresh_signature(old_resource) != self._build_resource_refresh_signature(new_resource):
                changed_resources += 1
                merged_resources[new_file_name] = new_resource
                merged_resources_info[new_file_name] = new_info
                continue

            merged_resource = self.odcrawler.preserve_local_resource_fields(old_resource, new_resource)

            merged_resources[new_file_name] = merged_resource
            merged_resources_info[new_file_name] = old_info
            preserved_resources += 1

        package["resources"] = merged_resources
        package["crawlerInfo"]["resourcesInfo"] = merged_resources_info

        if preserved_resources or changed_resources:
            logger(
                "INFO",
                f"CKAN refresh preserved local state for {preserved_resources} unchanged resource(s) and marked {changed_resources} resource(s) for reprocessing in package '{package.get('identifier')}'",
                indent=2,
            )

        return package

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.odcrawler.domain}/api/3/action/package_show?id={package_id}")
        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://www.ckan.org/img/ckan-logo-256.png"

        response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_action_request(
            "package_show",
            params={"id": package_id},
            return_tag=True,
        )

        #response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=headers, return_tag=True)
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

        payload = response.json()
        if not payload.get("success", False):
            logger("WARNING", f"CKAN package_show returned success=false for package '{package_id}'", indent=2)
            metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations("invalid_request"))
            metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
            return metadata

        data = payload.get("result") or {}
        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/dataset/{data.get('name')}")

        metadata["title"] = data.get("title", {})
        metadata["description"] = data.get("notes", {})

        distributions = data.get("resources", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        publisher = data.get("organization") or {}
        metadata["publisher"] = {
            "identifier": publisher.get("id", ""),
            "title": utils.extract_first_nonempty_value(publisher.get("title", "")),
        }

        if distributions:
            download_url = distributions[0].get("url") or distributions[0].get("original_url")
            if download_url:
                metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language", [])

        metadata["keyword"] = data.get("keywords", [])
        if not metadata["keyword"]:
            keywords = data.get("original_tags") or data.get("tags") or []
            if isinstance(keywords, list):
                for keyword in keywords:
                    if isinstance(keyword, dict):
                        state = keyword.get("state")
                        if not state or state == "active":
                            display_name = keyword.get("display_name") or keyword.get("name")
                            if display_name:
                                metadata["keyword"].append(display_name)
                    else:
                        metadata["keyword"].append(keyword)

        metadata["theme"] = []
        themes = data.get("theme") or data.get("groups")
        if isinstance(themes, list):
            for theme in themes:
                if isinstance(theme, dict):
                    display_name = theme.get("display_name") or theme.get("title")
                    if display_name:
                            metadata["theme"].append(display_name)
                else:
                    metadata["theme"].append(theme)

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("metadata_modified", "")
        metadata["issued"] = data.get("metadata_created", "")
        metadata["license"] = data.get("license_url", "") or data.get("license_id", "")
        if not metadata["license"] and distributions:
            metadata["license"] = distributions[0].get("license", "")

        metadata["source"] = self.odcrawler.domain

        temporals = data.get("temporal", {}) or data.get("temporals", {})
        if not isinstance(temporals, list):
            temporals = [temporals]
        
        for temporal in temporals:
            if isinstance(temporal, dict):
                metadata["temporal"] = {
                    "startDate": temporal.get("startDate") or temporal.get("start_date"),
                    "endDate": temporal.get("endDate") or temporal.get("end_date"),
                }

        location = data.get("location", "")
        spatial = data.get("spatial", "")
        geo = utils.extract_mapped_field(location, utils.CKANCRAWLER_SPATIAL_MAP)
        if utils.is_geojson(spatial):
            metadata["spatial"] = json.loads(spatial) if isinstance(spatial, str) else spatial
        else:
            if not geo:
                geo = utils.extract_mapped_field(spatial, utils.CKANCRAWLER_SPATIAL_MAP)
            if geo:
                metadata["geo"] = geo

        if distributions:
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = data

        return metadata
