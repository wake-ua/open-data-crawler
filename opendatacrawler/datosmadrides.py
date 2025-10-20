import requests
from urllib.parse import urlparse
from opendatacrawler import utils
import xml.etree.ElementTree as ET
from io import BytesIO
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class DatosMadridEsCrawler():
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

        self.catalog_data, self.ns = self.get_catalog_data_ns()

    def get_catalog_data_ns(self):
        url = f"{self.odcrawler.domain}/egob/catalogo.rdf"

        response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, max_sec=self.odcrawler.max_sec)
        if not response:
            return None, {}
        
        catalog_data = response.content
        if not catalog_data:
            return None, {}

        root = ET.parse(BytesIO(catalog_data)).getroot()
        ns = utils.extract_namespaces(catalog_data)
        return root, ns

    def get_package_list(self):
        try:
            ids = []
            for dataset in self.catalog_data.findall(".//dcat:Dataset", self.ns):
                id_el = dataset.find("dct:identifier", self.ns)
                if id_el is not None and id_el.text:
                    ids.append(id_el.text.strip().removeprefix(f"{self.odcrawler.domain}/egob/catalogo/").lstrip("/"))

            logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.odcrawler.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.odcrawler.domain}'", e)

        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}

        resource["fileName"] = base_name

        resource["name"] = utils.normalize_no_html_text(utils.get_xml_text(resource_meta.find("dct:title", self.ns)))

        download_el = resource_meta.find("dcat:accessURL", self.ns)
        download_url = None
        if download_el is not None:
            download_url = download_el.text or utils.get_xml_attr(download_el, self.ns["rdf"], "resource")
        resource["downloadURL"] = utils.fix_url(download_url)

        media_el = resource_meta.find("dcat:mediaType", self.ns) or resource_meta.find("dct:format", self.ns)
        meta_media_type = utils.get_xml_text(media_el)

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        if self.catalog_data is None:
            return None

        data = None
        for dataset in self.catalog_data.findall(".//dcat:Dataset", self.ns):
            id_el = dataset.find("dct:identifier", self.ns)
            if id_el is not None and id_el.text and id_el.text.strip().removeprefix(f"{self.odcrawler.domain}/egob/catalogo/").lstrip("/") == package_id:
                data = dataset
                break

        if data is None:
            logger("WARNING", f"Dataset '{package_id}' not found in RDF", level="print")
            return None

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["accessURL"] = utils.fix_url(package_id)
        metadata["requestURL"] = None

        metadata["fileName"] = metadata_file_name

        metadata["img"] = "https://datos.madrid.es/FwFront/portal_egob/img/portal_logo_f.png"

        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/egob/catalogo/{package_id}")

        metadata["title"] =  utils.normalize_no_html_text(utils.get_xml_text(data.find("dct:title", self.ns)))
        metadata["description"] =  utils.normalize_no_html_text(utils.get_xml_text(data.find("dct:description", self.ns)))

        distributions = data.findall(".//dcat:distribution/dcat:Distribution", self.ns)
        if not isinstance(distributions, list):
            distributions = [distributions]

        publisher_el = data.find("dct:publisher", self.ns)
        metadata["publisher"] = {"identifier": utils.get_xml_attr(publisher_el, self.ns["rdf"], "resource") if publisher_el is not None else None}

        access_el = distributions[0].find("dcat:accessURL", self.ns)
        if access_el is not None:
            access_url = access_el.text or utils.get_xml_attr(access_el, self.ns["rdf"], "resource")
            if access_url:
                domain = urlparse(access_url).netloc
                metadata["publisher"]["homepage"] = f"https://{domain}"

        metadata["language"] = None

        metadata["accrualPeriodicity"] = utils.get_xml_text(data.find(".//dct:accrualPeriodicity//rdfs:label", self.ns))
        metadata["modified"] = utils.get_xml_text(data.find("dct:modified", self.ns))
        metadata["issued"] = utils.get_xml_text(data.find("dct:issued", self.ns))
        metadata["license"] = utils.get_xml_attr(data.find("dct:license", self.ns), self.ns["rdf"], "resource")
    
        metadata["source"] = self.odcrawler.domain

        temporal_el = data.find("dct:temporal", self.ns)
        if temporal_el is not None:
            start_el = temporal_el.find(".//dct:startDate", self.ns)
            end_el = temporal_el.find(".//dct:endDate", self.ns)
            metadata["temporal"] = {
                "startDate": utils.get_xml_text(start_el),
                "endDate": utils.get_xml_text(end_el),
            }
        else:
            metadata["temporal"] = None

        spatial_el = data.find("dct:spatial", self.ns)
        if spatial_el is not None:
            spatial_uri = utils.get_xml_attr(spatial_el, self.ns["rdf"], "resource")
            spatial_label = utils.get_xml_text(spatial_el.find(".//rdfs:label", self.ns))
            metadata["geo"] = spatial_label or spatial_uri
        else:
            metadata["geo"] = None

        if distributions:
            logger("WORK", f"Processing {len(distributions)} resources from package '{package_id}' ('{metadata_file_name}')...", indent=2)
            self.odcrawler.init_and_parse_resources(metadata, distributions)
            
        return metadata