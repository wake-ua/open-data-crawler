<div id="top"></div>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![LinkedIn][linkedin-shield]][linkedin-url]

<br/>

<div align="center">
  <a href="https://github.com/aberenguerpas/opendatacrawler">
    <img src="images/logo.png" alt="OpenDataCrawler Logo" width="200" height="200">
  </a>

  <p align="center">
    A flexible tool to crawl and normalize data from open data portals into your projects.
    <br/>
    <a href="https://github.com/aberenguerpas/opendatacrawler/issues">Report a Bug</a>
    ·
    <a href="https://github.com/aberenguerpas/opendatacrawler/issues">Request a Feature</a>
  </p>
</div>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About the Project</a>
      <ul>
        <li><a href="#key-features">Key Features</a></li>
        <li><a href="#currently-supported-portals-and-sites">Currently Supported Portals and Sites</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#requirements">Requirements</a></li>
        <li><a href="#installation">Installation</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li>
      <a href="#contributing">Contributing</a>
      <ul>
        <li><a href="#how-to-contribute">How to Contribute</a></li>
        <li><a href="#add-support-for-a-new-portal">Add Support for a New Portal</a></li>
        <li><a href="#define-new-mapping-files">Define New Mapping Files</a></li>
      </ul>
    </li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>
---

## About The Project

Open Data Crawler is a tool designed to extract datasets, and optionally their metadata, from open data and statistics portals. The community can contribute by adding support for new portals or implementing additional features.

### Key Features
- Download datasets from open data or statistics portals
- Retrieve metadata from resources
- Filter datasets by data type
- Filter datasets by topic or category

### Currently Supported Portals and Sites
- [x] [datos.gob.es](https://datos.gob.es)
- [x] CKAN
- [ ] Socrata *
- [ ] [Eurostat](https://ec.europa.eu/eurostat) *
- [ ] [World Bank Data Catalog](https://datacatalogapi.worldbank.org/) *
- [ ] OpenDataSoft *

\* Works with restrictions or download limitations  

See the [open issues](https://github.com/aberenguerpas/opendatacrawler/issues) for a full list of proposed features and known issues.

<p align="right">(<a href="#top">back to top</a>)</p>

---

## Getting Started

To set up the project locally, follow these steps:

### Requirements
* Python 3.9+ installed  
* Socrata portals require an App Token to avoid throttling limits. You can obtain an API key [here](https://support.socrata.com/hc/en-us/articles/210138558-Generating-an-App-Token) and set it in `config.ini`

### Installation

1. Clone the repo
  ```sh
  git clone https://github.com/aberenguerpas/opendatacrawler.git
  ```

2. Install the package from the project root
  ```sh
  pip install .
  ```

<p align="right">(<a href="#top">back to top</a>)</p>

---

## Usage

Using this tool is very simple: you only need to specify the URL of the data portal, and the tool will automatically detect the portal type and start downloading the data.  
For a full list of available commands, run:
```sh
python opendatacrawler -h
```

### Examples
#### Download all data from a portal:
```
python opendatacrawler -d https://datos.gob.es
```
#### Download specific format data (e.g., 'xls' and 'csv'):
```
python opendatacrawler -d https://datos.gob.es -t xls csv
```
#### Download specific categories (e.g., 'tourism' and 'transport'):
```
python opendatacrawler -d https://datos.gob.es -c tourism transport
```

<!-- _For more examples, see the [Documentation](https://example.com)_ -->

<p align="right">(<a href="#top">back to top</a>)</p>

## Contributing

Contributions are what make the open source community such an amazing place to learn, share, and build together. Any contribution is **greatly appreciated**.  

If you have a suggestion, improvement, or want to add support for a new site or portal, feel free to fork the repository and open a pull request. You can also open an issue using the `enhancement` label.

And don't forget to give the project a star! Thanks for your support! 🌟

### How to Contribute
1. Fork the repository
2. Create a feature branch 
  ```sh
    git checkout -b feature/AmazingFeature
  ```
3. Commit your changes
  ```sh
    git commit -m 'Add some AmazingFeature'
  ```
4. Push to the branch
  ```sh
    git push origin feature/AmazingFeature
  ```
5. Open a Pull Request

### Add Support for a New Portal
1. Create a file named `<PortalName>crawler.py` inside `opendatacrawler/` folder (e.g. `examplecrawler.py`).
2. Create a class `<PortalName>Crawler` with this constructor:
  ```python
  class ExampleCrawler:
      def __init__(self, domain, data_types, user_agent):
          self.domain = domain.rstrip("/")
          self.data_types = data_types
          self.user_agent = user_agent
  ```
3. Implement the required methods:
  - **`get_package_list(self)`**:  
    Returns the list of dataset or package identifiers from the portal's API. Each identifier represents a dataset entry (i.e., a collection of one or more downloadable resources), which will be fetched individually in the next step.

  - **`parse_resource(self, data, base_name)`**:  
    Normalizes a single resource object (distribution, file, link...) from the dataset into the project's internal resource schema.  
    This should always include extracting the `downloadURL`, determining MIME type, and generating a normalized filename using:  
    ```python
    utils.generate_short_filename(base_name, ext=utils.get_extension_mime(resource["mediaType"]))
    ```

  - **`get_package(self, dataset_id, metadata_file_name)`**:  
    Retrieves and normalizes the full metadata of a package of datasets given its identifier (including its associated resources using `parse_resource()`).  
    To keep consistency across crawlers, it's recommended to structure the resource list as follows:  
    ```python
    resource_list = []
    for idx, res in enumerate(resources):
        resource_list.append(self.parse_resource(res, f"{metadata['fileName']}_{idx}"))
    metadata["resources"] = resource_list
    ```

4. Use helper functions (`utils.py`) and built-in logging:
- **Network calls**: `utils.make_request(url, self.user_agent, headers=...)`  
  > Note: the function may rotate the `user_agent`. Always capture and reuse the returned value.

- **File helpers**: `utils.generate_short_filename()`, `utils.get_mime_extension()`, `utils.get_extension_mime()`...

- **Field extraction helpers**: `utils.extract_multilang_field()`, `utils.extract_mapped_field()`  
  > For more advanced usage of `extract_mapped_field()` with fallback logic and JSON mappings, see [Define new mapping files](#define-new-mapping-files).

5. Return metadata using the normalized keys defined by the project, based on the [DCAT standard](https://www.w3.org/TR/vocab-dcat/).
  Each package of datasets must return a dictionary containing at least:
  - `identifier`: the package's ID from the portal.
  - `fileName`: the normalized name for saving the metadata file (provided to `get_package()`).
  - `resources`: a list of resources, each one generated using `parse_resource()`.

  It is also strongly recommended to include the following for debugging and traceability:
  - `requestURL`: the exact URL used to fetch the metadata.
  - `accessURL`: the public-facing URL where a user would normally access the package.

  Each resource in the resources list must contain:
  - `downloadURL`: the direct link to download the file.
  - `fileName`: the filename, including its extension, to use when saving the resource locally.
  - `mediaType`: the MIME type of the resource, which should be used to guess the proper file extension via `utils.get_extension_mime()`.

  > Note: You don't need to include all possible DCAT fields, just the ones available in the portal. However, the structure of the returned metadata must remain consistent.

6. Register portal detection:  
  Update the function `detect_dms()` in `odcrawler.py` to detect your portal and return the correct crawler instance.

### Define New Mapping Files

You can also contribute to the project by defining your own JSON mapping files inside the `resources/` folder, following the naming pattern `<portal>_<field>_map.json` (lowercase) and then loading these mappings using `load_resource()` from `utils.py`, and assign them to a constant using the format `<PORTALNAME>CRAWLER_<FIELDNAME>_MAP` (uppercase).

These are used by `utils.extract_mapped_field()` to normalize raw values (e.g. URIs) into human-readable text, and to avoid unnecessary requests for known or static entities (such as publishers, themes, spatial areas, etc.).

#### Example of using a mapping file

In the case of needing a new field map for the field `theme` in the crawler `ExampleCrawler`, where the field contains URIs that should be normalized to an object with a human-readable name and an identifier:

- 1. Create a file named `resources/example_theme_map.json` with the desired content. Example:
  ```json
  {
    "https://example.org/theme/health-care": {
      "name": "Health Care",
      "identifier": "HC12345"
    }
  }
  ```
- 2. Load the map using `load_resource()` in `utils.py`, providing a fallback value for unmapped entries:
  ```python
  EXAMPLECRAWLER_THEME_MAP = load_resource(
      "example_theme_map.json",
      fallback_value={"name": "FALLBACK_VALUE", "identifier": None}
  )
  ```
- 3. Use `utils.extract_mapped_field()` in your crawler code to normalize a value:
  ```python
  utils.extract_mapped_field("https://example.org/theme/energy-efficiency", EXAMPLECRAWLER_THEME_MAP)
  ```
- 4. If the input exists in the map, the mapped value will be returned.

  Otherwise, the fallback logic is applied:
  - For URLs (as in the example below), the last segment is extracted, hyphens are replaced with spaces, and the result is converted to title case (e.g., `"energy-efficiency"` becomes `"Energy Efficiency"`).  
  - For non-URLs, the raw value is used as-is.

  The fallback result is then inserted into the `"FALLBACK_VALUE"` placeholder defined in the fallback template.

  Final result:
  ```python
  [{ "name": "Energy Efficiency", "identifier": None }]
  ```

<p align="right">(<a href="#top">back to top</a>)</p>

---

## License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="right">(<a href="#top">back to top</a>)</p>

---

## Contact

* Paula Margarita García-Tapia Mateo \
✉️ paula.garciatapia@ua.es

*  Alberto Berenguer Pastor \
✉️ alberto.berenguer@ua.es

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/aberenguerpas/opendatacrawler?style=for-the-badge
[contributors-url]: https://github.com/aberenguerpas/opendatacrawler/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/aberenguerpas/opendatacrawler.svg?style=for-the-badge
[forks-url]: https://github.com/aberenguerpas/opendatacrawler/network/members
[stars-shield]: https://img.shields.io/github/stars/aberenguerpas/opendatacrawler.svg?style=for-the-badge
[stars-url]: https://github.com/aberenguerpas/opendatacrawler/stargazers
[issues-shield]: https://img.shields.io/github/issues/aberenguerpas/opendatacrawler.svg?style=for-the-badge
[issues-url]: https://github.com/aberenguerpas/opendatacrawler/issues
[license-shield]: https://img.shields.io/github/license/aberenguerpas/opendatacrawler?style=for-the-badge
[license-url]: https://github.com/aberenguerpas/opendatacrawler/blob/main/LICENSE
[linkedin-shield]: https://img.shields.io/badge/-LinkedIn-black.svg?style=for-the-badge&logo=linkedin&colorB=555
[linkedin-url]: https://www.linkedin.com/in/alberto-berenguer-pastor-220274154/
[product-screenshot]: images/screenshot.png