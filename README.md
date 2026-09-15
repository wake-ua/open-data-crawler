<div id="top"></div>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![LinkedIn][linkedin-shield]][linkedin-url]

<br/>

<div align="center">
  <!--
  <a href="https://github.com/aberenguerpas/opendatacrawler">
    <img src="images/logo.png" alt="OpenDataCrawler Logo" width="200" height="200">
  </a>
  -->
<pre>
****************************************************************************************************
   ___                         ___         _             ___                         _             
  /___\ _ __    ___  _ __     /   \  __ _ | |_   __ _   / __\ _ __   __ _ __      __| |  ___  _ __ 
 //  //| '_ \  / _ \| '_ \   / /\ / / _` || __| / _` | / /   | '__| / _` |\ \ /\ / /| | / _ \| '__|
/ \_// | |_) ||  __/| | | | / /_// | (_| || |_ | (_| |/ /___ | |   | (_| | \ V  V / | ||  __/| |   
\___/  | .__/  \___||_| |_|/___,'   \__,_| \__| \__,_|\____/ |_|    \__,_|  \_/\_/  |_| \___||_|   
       |_|                                                                              - v2.3.0   
****************************************************************************************************
</pre>
  
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
    <li>
      <a href="#usage">Usage</a>
      <ul>
        <li><a href="#examples">Examples</a></li>
      </ul>
    </li>
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
- [x] [Zenodo](https://zenodo.org/) * (request limit ≈ 1.39 req/sec ~ 5000 req/hour)
- [x] [GBIF](https://www.gbif.org/es/)
- [x] [datos.madrid.es](https://datos.madrid.es)
- [x] [data.europa.eu](https://data.europa.eu)
- [x] [INE (Instituto Nacional de Estadística)](https://www.ine.es/) *
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
* Python 3.12+ on Linux
* Optional CKAN and Zenodo tokens can be configured in `[CKAN]` and `[zenodo]` sections. Public endpoints do not receive an empty bearer token.
* Copy `opendatacrawler/config.example.ini` to a private configuration file and set `ODC_CONFIG=/absolute/path/config.ini`. A local `opendatacrawler/config.ini` is also read when `ODC_CONFIG` is unset; credentials are excluded from built distributions.
* Runtime defaults can also be configured in `opendatacrawler/config.ini` using sections such as `[defaults]`, `[datosgobes]`, `[zenodo]` or `[ine]`.
  * Example for partial tabular downloads:
    ```ini
    [defaults]
    partial_dataset_rows = 100
    partial_dataset_sample_mode = first
    ; partial_dataset_random_seed = 1
    ```

> [!IMPORTANT]
> Command-line arguments take priority over `config.ini`, and `config.ini` takes priority over the built-in defaults.

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
opendatacrawler -h
```

### Examples
#### Download all data from a portal:
```
opendatacrawler -d https://datos.gob.es
```
> [!NOTE]
> In `datos.gob.es`, this first refreshes or reuses the local catalog index under `<output>/datos.gob.es/.cache/`, and then processes packages from that local index.
#### Download specific format data (e.g., 'xls' and 'csv'):
```
opendatacrawler -d https://datos.gob.es -t xls csv
```
> [!NOTE]
> The INE crawler uses JSON by default and also supports `-t csv`, `-t xlsx`/`-t xls`, and `-t px`, treating `xls` as `xlsx`. Any other extension is rejected for INE. Each selected format is downloaded as a separate distribution.

#### Download specific categories (e.g., 'turismo' and 'transporte'):
```
opendatacrawler -d https://datos.gob.es -c turismo transporte
```
> [!NOTE]
> `-c` is not supported by INE as it does not expose categories through this crawler.

#### Download a specific dataset by ID:
```
opendatacrawler -d https://datos.gob.es -id a01002820-academias-andaluzas
```
#### Save the downloaded data in a custom folder:
```
opendatacrawler -d https://datos.gob.es -p /my/example/path/
```
#### Limit how many packages are processed:
```
opendatacrawler -d https://datos.gob.es -n 10
```
#### Limit how many resources are downloaded per package:
```
opendatacrawler -d https://datos.gob.es -nr 1
```
#### Save only metadata without downloading dataset files:
```
opendatacrawler -d https://datos.gob.es -nd
```
#### Download data from a specific country in data.europa.eu:
```
opendatacrawler -d https://data.europa.eu --country es
```
> [!NOTE]
> `--country` is only supported by data.europa.eu.

#### Force re-download of specific datasets:
```
opendatacrawler -d https://datos.gob.es -id a01002820-academias-andaluzas -replace
```
#### Resume without checking whether already-downloaded packages changed remotely:
```
opendatacrawler -d https://datos.gob.es --skip-updates-check
```
#### Reset the local data and logs for one domain before crawling:
```
opendatacrawler -d https://datos.gob.es -rd
```
> [!CAUTION]
> `-rd` deletes the local data and logs for the selected domain before starting the crawl.

#### Save original raw metadata from the portal:
```
opendatacrawler -d https://datos.gob.es --save-raw-data
```
> [!CAUTION]
> `--save-raw-data` stores the original portal payload inside the generated metadata files. This is useful for debugging and traceability, but it increases metadata size and write time.
#### Disable schema extraction for CSV/TSV files:
```
opendatacrawler -d https://datos.gob.es --no-extract-schema
```
#### Store partial local copies when downloading CSV/TSV datasets using the sampling settings defined in `config.ini`:
```ini
[defaults]
partial_dataset_rows = 100
partial_dataset_sample_mode = first
; partial_dataset_random_seed = 1
```
```sh
opendatacrawler -d https://datos.gob.es -t csv tsv --partial-dataset
```
> [!IMPORTANT]
> `--partial-dataset` enables the feature for the current run. The number of retained data rows is read from `config.ini` (`partial_dataset_rows`), and `partial_dataset_sample_mode` controls whether the crawler keeps the first rows or a random sample after full download and processing. If `partial_dataset_random_seed` is set, the random sample is reproducible across runs.

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
1. Create a file named `<PortalName>crawler.py` inside `opendatacrawler/portals/` (e.g. `examplecrawler.py`).
2. Create a class `<PortalName>Crawler` with this constructor:
  ```python
  class ExampleCrawler:
      def __init__(self, odcrawler):
        self.odcrawler = odcrawler

        # Optional: limit requests per second (useful if the portal enforces rate limits)
        # self.odcrawler.init_rate_limit(reqs_per_sec=<RequestPerSecondLimit>)


        # Optional: authentication token if the portal requires it
        # self.token = utils.AUTH_TOKENS.get(<PortalName>)
  ```
  > [!NOTE]
  > If the portal requires authentication, make sure to define its token in the `config.ini` file (see [Requirements](#requirements))

3. Implement the required methods:
  - **`get_package_list(self)`**:  
    Returns the list of dataset or package identifiers from the portal's API. Each identifier represents a dataset entry (i.e., a collection of one or more downloadable resources), which will be fetched individually in the next step.

  - **`parse_resource(self, data, base_name)`**:  
    Normalizes a single resource object (distribution, file, link...) from the dataset into the project's internal resource schema.

  - **`get_package(self, dataset_id, metadata_file_name)`**:  
    Retrieves and normalizes the full metadata of a package of datasets given its identifier (including its associated resources using `parse_resource()`).
    
    To keep consistency across crawlers, it's recommended to structure the resource list as follows:  
    ```python
    if distributions:
        self.odcrawler.init_and_parse_resources(metadata, distributions)
    ```

4. Use helper functions from the `opendatacrawler/utils/` package and built-in logging:
  - **Network calls**: `self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=...)`
    > [!NOTE]
    >  Reuse the returned agent value and close every response. Catalog failures must raise `CatalogError`; never return partial IDs as a successful enumeration.

  - **File helpers**: `utils.generate_short_filename()`, `utils.get_mime_and_ext()`, `utils.get_extension_mime()`...

  - **Field extraction helpers**: `utils.extract_multilang_field()`, `utils.extract_mapped_field()`  
    > For more advanced usage of `utils.extract_mapped_field()` with fallback logic and JSON mappings, see [Define new mapping files](#define-new-mapping-files).

5. Return metadata using the normalized keys defined by the project, based on the [DCAT standard](https://www.w3.org/TR/vocab-dcat/).
  Each package of datasets must return a dictionary containing at least:
  - `identifier`: the package's ID from the portal.
  - `fileName`: the normalized name for saving the metadata file (provided to `get_package()`).
  - `resources`: a dictionary keyed by `fileName`, populated by `init_and_parse_resources()`.

    It is also strongly recommended to include the following for debugging and traceability:
    - `requestURL`: the exact URL used to fetch the metadata.
    - `accessURL`: the public-facing URL where a user would normally access the package.

  Each resource in the resources dictionary must contain:
  - `downloadURL`: the direct link to download the file.
  - `fileName`: the filename, including its extension, to use when saving the resource locally.
  - `mediaType`: the MIME type of the resource, which should be used to guess the proper file extension via `utils.get_extension_mime()`.

  > [!NOTE]
  > You don't need to include all possible DCAT fields, just the ones available in the portal. However, the structure of the returned metadata must remain consistent.

6. Register portal detection:  
  Update the function `detect_dms()` in `odcrawler.py` to detect your portal and return the correct crawler instance.

### Define New Mapping Files

You can also contribute to the project by defining your own JSON mapping files inside the `resources/` folder, following the naming pattern `<portal>_<field>_map.json` (lowercase) and then loading these mappings using `load_resource()` from `opendatacrawler.utils`, and assign them to a constant using the format `<PORTALNAME>CRAWLER_<FIELDNAME>_MAP` (uppercase).

These are used by `utils.extract_mapped_field()` to normalize raw values (e.g. URIs) into human-readable text, and to avoid unnecessary requests for known or static entities (such as publishers, themes, spatial areas, etc.).

#### Example of using a mapping file

In the case of needing a new field map for the field `theme` in the crawler `ExampleCrawler`, where the field contains URIs that should be normalized to an object with a human-readable name and an identifier:

1. Create a file named `resources/example_theme_map.json` with the desired content. Example:
  ```json
  {
    "https://example.org/theme/health-care": {
      "name": "Health Care",
      "identifier": "HC12345"
    }
  }
  ```
2. Load the map using `load_resource()` from `opendatacrawler.utils`, providing a fallback value for unmapped entries:
  ```python
  EXAMPLECRAWLER_THEME_MAP = load_resource(
      "example_theme_map.json",
      fallback_value={"name": "FALLBACK_VALUE", "identifier": None}
  )
  ```
3. Use `utils.extract_mapped_field()` in your crawler code to normalize a value:
  ```python
  utils.extract_mapped_field("https://example.org/theme/energy-efficiency", EXAMPLECRAWLER_THEME_MAP)
  ```
4. If the input exists in the map, the mapped value will be returned.

  Otherwise, the fallback logic is applied:
  - For URLs (as in the example below), the last segment is extracted, hyphens are replaced with spaces, and the result is converted to title case (e.g., "energy-efficiency" becomes "Energy Efficiency").  
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

- Paula Margarita García-Tapia Mateo \
✉️ paula.garciatapia@ua.es

-  Alberto Berenguer Pastor \
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
