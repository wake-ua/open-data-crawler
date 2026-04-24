import argparse
import os
import sys
import urllib3
import traceback
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from opendatacrawler import utils
from opendatacrawler.odcrawler import OpenDataCrawler
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log


def resolve_option(cli_value, section, option, cast, fallback):
    if cli_value is not None:
        return cli_value
    config_value = utils.get_config_option(section, option, cast=cast, fallback=None)
    if config_value is not None:
        return config_value
    return fallback


def resolve_portal_option(section, option, cast, fallback):
    config_value = utils.get_config_option(section, option, cast=cast, fallback=None) if section else None
    if config_value is not None:
        return config_value
    return utils.get_config_option("defaults", option, cast=cast, fallback=fallback)


def apply_portal_runtime_config(crawler, args):
    section = crawler.dms or ""

    if args.get("max_seconds") is None:
        crawler.max_sec = utils.get_config_option(section, "max_seconds", cast=int, fallback=crawler.max_sec)

    if args.get("max_threads") is None:
        crawler.max_threads = utils.get_config_option(section, "max_threads", cast=int, fallback=crawler.max_threads)

    if args.get("max_resource_threads") is None:
        crawler.max_resource_threads = utils.get_config_option(section, "max_resource_threads", cast=int, fallback=crawler.max_resource_threads)

    if args.get("save_raw_data") is None:
        crawler.save_raw_data = utils.get_config_option(section, "save_raw_data", cast=bool, fallback=crawler.save_raw_data)

    if args.get("extract_schema") is None:
        crawler.extract_schema = utils.get_config_option(section, "extract_schema", cast=bool, fallback=crawler.extract_schema)

    if args.get("reqs_per_sec") is None:
        reqs_per_sec = utils.get_config_option(section, "reqs_per_sec", cast=float, fallback=crawler.config_reqs_per_sec)
        crawler.init_rate_limit(reqs_per_sec=reqs_per_sec)

    partial_enabled = args.get("partial_dataset")
    if partial_enabled is None:
        partial_enabled = resolve_portal_option(section, "partial_dataset", bool, False)

    partial_dataset_rows = resolve_portal_option(section, "partial_dataset_rows", int, 100)
    if partial_dataset_rows is None or partial_dataset_rows <= 0:
        partial_dataset_rows = 100

    crawler.partial_dataset_rows = partial_dataset_rows if partial_enabled else None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--domain", type=str, required=True,
                        help="A data source (Ex. -d https://domain.example)")
    parser.add_argument("-t", "--data-types", "--data_types", dest="data_types", nargs="+", required=False,
                        help="data types to save (Ex. -t xls pdf) (default: all)")
    parser.add_argument("-c", "--categories", nargs="+", required=False,
                        help="Categories to save (Ex. -c crime tourism transport) (default: all)")
    parser.add_argument("-p", "--path", type=str, required=False,
                        help="Path to save data (Ex. -p /my/example/path/)")
    parser.add_argument("-s", "--max-seconds", "--max_seconds", dest="max_seconds", type=int, required=False,
                        help="Max seconds to wait for server response during file download (e.g., -s 60)")
    parser.add_argument("--reqs-per-sec", "--reqs_per_sec", dest="reqs_per_sec", type=float, required=False,
                        help="General requests-per-second limit for the crawler (e.g. --reqs-per-sec 1.5)")
    parser.add_argument("-pd", "--partial-dataset", "--partial_dataset", dest="partial_dataset", required=False, action=argparse.BooleanOptionalAction,
                        help="Enable partial local copies for CSV/TSV datasets after full processing using the row limit configured in config.ini")
    parser.add_argument("-id", "--id-dataset", "--id_dataset", dest="id_dataset", nargs="+", required=False,
                        help="Save the dataset with that id (Ex. -id edu-alu-fpa-2021) (default: all)")
    parser.add_argument("-nd", "--no-dataset", "--no_dataset", dest="no_dataset", required=False, action=argparse.BooleanOptionalAction,
                        help="No save the dataset (default: save)")
    parser.add_argument("-n", "--max-packages", "--max_packages", dest="max_packages", type=int, required=False,
                        help="Maximum number of packages to process (default: all)")
    parser.add_argument("-mt", "--max-threads", "--max_threads", dest="max_threads", type=int, required=False,
                        help="Maximum number of threads to use (default: based on CPU count, up to 32)")
    parser.add_argument("-mrt", "--max-resource-threads", "--max_resource_threads", dest="max_resource_threads", type=int, required=False,
                        help="Maximum number of threads to use per package for processing resources (default: 4)")
    parser.add_argument("-rd", "--reset-domain", "--reset_domain", dest="reset_domain", required=False, action=argparse.BooleanOptionalAction,
                        help="Delete all data and logs for the specified domain before crawling")
    parser.add_argument("-nr", "--num-resources", dest="num_resources", type=int, required=False,
                        help="Number of resources per package to download (default: all)")
    parser.add_argument("-replace", "--replace", dest="replace", required=False, action=argparse.BooleanOptionalAction,
                        help="Force re-download of datasets specified with --id-dataset (delete old metadata and data first)")
    parser.add_argument("-country", "--country", "--countries", dest="countries", nargs="+", required=False,
                        help="Filter datasets by country code (e.g. --country es gr fr)")
    parser.add_argument("--ignore-hosts", "--ignore_hosts", dest="ignore_hosts", nargs="+", required=False,
                        help="Hosts or URLs to ignore during crawling (e.g. --ignore-hosts datos.aviles.es https://datosabiertos.navarra.es)")
    parser.add_argument("--save-raw-data", "--save_raw_data", dest="save_raw_data", required=False, action=argparse.BooleanOptionalAction,
                        help="Store original raw metadata returned by the source portal (default: disabled)")
    parser.add_argument("--extract-schema", "--extract_schema", dest="extract_schema", required=False, action=argparse.BooleanOptionalAction,
                        help="Extract tabular schema from CSV/TSV files (default: enabled)")

    args = vars(parser.parse_args())

    url = args["domain"]
    d_types = [c.lower() for c in args["data_types"]] if args["data_types"] else []
    categories = [c.lower() for c in args["categories"]] if args["categories"] else []
    d_path = args["path"]
    max_sec = resolve_option(args["max_seconds"], "defaults", "max_seconds", int, None)
    reqs_per_sec = resolve_option(args.get("reqs_per_sec"), "defaults", "reqs_per_sec", float, None)
    partial = args["partial_dataset"]
    partial_dataset_rows = None
    if partial is True:
        partial_dataset_rows = utils.get_config_option("defaults", "partial_dataset_rows", cast=int, fallback=100)
        if partial_dataset_rows is None or partial_dataset_rows <= 0:
            partial_dataset_rows = 100
    id_dataset = args["id_dataset"]
    avoid_data = args["no_dataset"]
    max_packages = args.get("max_packages")
    max_threads = resolve_option(args["max_threads"], "defaults", "max_threads", int, min(16, max(4, (os.cpu_count() or 1) * 2)))
    max_resource_threads = resolve_option(args.get("max_resource_threads"), "defaults", "max_resource_threads", int, 2)
    reset_domain = args.get("reset_domain")
    num_resources = args.get("num_resources")
    replace = args.get("replace")
    countries = [c.lower() for c in args["countries"]] if args["countries"] else []
    ignore_hosts = args.get("ignore_hosts") or []
    save_raw_data = resolve_option(args.get("save_raw_data"), "defaults", "save_raw_data", bool, False)
    extract_schema = resolve_option(args.get("extract_schema"), "defaults", "extract_schema", bool, True)

    if num_resources == 0:
        avoid_data = True
        num_resources = None

    utils.print_intro()
    crawler = None
    try:
        if utils.is_url(url):
            crawler = OpenDataCrawler(
                url,
                path=d_path,
                data_types=d_types,
                categories=categories,
                partial=partial,
                partial_dataset_rows=partial_dataset_rows,
                avoid_data=avoid_data,
                max_sec=max_sec,
                reqs_per_sec=reqs_per_sec,
                max_threads=max_threads,
                max_resource_threads=max_resource_threads,
                num_resources=num_resources,
                countries=countries,
                ignore_hosts=ignore_hosts,
                save_raw_data=save_raw_data,
                extract_schema=extract_schema,
            )

            if not crawler.dms:
                log_manager.move_to_domain("_unknownDomain", move_file=True)
                sys.exit(1)

            apply_portal_runtime_config(crawler, args)

            reset_domain_input = False
            has_logs = False
            has_data = False
            if reset_domain:
                has_data = (os.path.isdir(crawler.base_domain_path) and len(os.listdir(crawler.base_domain_path)) > 0)

                log_path = os.path.join(os.getcwd(), "logs", utils.clean_url(url))
                has_logs = os.path.isdir(log_path) and any(f.endswith(".log") for f in os.listdir(log_path))

                if has_data or has_logs:
                    if has_data:
                        if countries:
                            logger("WARNING", f"Are you absolutely sure you want to delete ALL data and logs for domain '{crawler.domain}' ({crawler.dms})? This affects ALL countries. [Y/N]:", level="print")
                        else:
                            logger("WARNING", f"Are you absolutely sure you want to delete ALL data and logs for domain '{crawler.domain}' ({crawler.dms})? [Y/N]:", level="print")

                        reset_domain_input = input().strip().lower() in {"y", "yes"}
                    elif has_logs:
                        logger("WARNING", f"No resume data found, but logs exist for domain '{crawler.domain}'. Do you want to delete them? [Y/N]:", level="print")
                        reset_domain_input = input().strip().lower() in {"y", "yes"}

                    if not reset_domain_input:
                        logger("WARNING", f"Reset for domain '{crawler.domain}' was cancelled by user", level="print")
                else:
                    reset_domain_input = True

            crawler.reset_domain(reset_domain_input, has_data, has_logs)

            countries_to_process = countries or [None]
            for country in countries_to_process:
                if country is None:
                    crawler.set_country_context(None)
                else:
                    country = str(country).strip().lower()
                    if not country:
                        crawler.set_country_context(None)
                    else:
                        label = utils.get_country_label(country)
                        if not label:
                            logger("WARNING", f"Unknown country code '{country}', skipping.", level="print")
                            continue

                        logger("INFO", f"Processing data from '{label}' [{country}]", level="print")
                        crawler.set_country_context(country)

                resume_data, downloaded_before_res, failed_before_res, unavailable_before_res, incomplete_before_pkgs = utils.recover_resume(
                    save_path=crawler.save_path,
                    accepted_types=d_types,
                    num_resources=crawler.num_resources,
                    max_workers=crawler.max_threads,
                )

                if resume_data:
                    logger(None, "=" * 80, level="print")
                    logger("OK", f"Loaded resume with {len(resume_data)} packages and {len(downloaded_before_res)} downloaded resources", level="print")
                    pending_before_res = sum(len(status.get("pending_resources", [])) for status in resume_data.values())
                    accepted_types_label = f"({', '.join(f'.{ext}' for ext in d_types)})" if d_types else "(all accepted types)"
                    if incomplete_before_pkgs:
                        logger("...", f"{len(incomplete_before_pkgs)} packages are still unresolved for the current configuration {accepted_types_label}", level="print")
                        logger("...", f"{len(failed_before_res)} retryable resources remain, {len(unavailable_before_res)} resources are permanently unavailable, and {pending_before_res} resources have not been attempted yet because of the current limits", level="print")
                    logger(None, "=" * 80, level="print")

                logger("...", f"Obtaining packages from '{crawler.get_print_domain()}'...", level="print")
                if replace and id_dataset:
                    crawler.force_replace_package(id_dataset)

                packages = id_dataset if id_dataset else crawler.get_package_list()
                new_packages = [pkg for pkg in packages if pkg not in resume_data]
                incomplete_packages = [pkg for pkg in packages if pkg in incomplete_before_pkgs]

                if max_packages:
                    new_packages = new_packages[:max_packages]

                if new_packages or incomplete_packages:
                    total_to_process = len(new_packages) + len(incomplete_packages)

                    if incomplete_packages:
                        logger("...",f"Queued {total_to_process} packages ({len(new_packages)} new packages and {len(incomplete_packages)} incomplete packages)", level="print")
                    else:
                        logger("...", f"Queued {total_to_process} packages for processing", level="print")

                    logger(None, "=" * 80, level="print")

                    # ==================================================

                    original_threads = crawler.max_threads
                    if crawler.serial_metadata_phase:
                        crawler.max_threads = 1

                    if new_packages:
                        logger("...", f"Collecting metadata for {len(new_packages)} new packages...", level="print")
                        crawler.process_packages_batch(new_packages, phase="metadata", tqdm_initial=0, tqdm_desc="Collecting metadata", tqdm_colour="blue", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    crawler.max_threads = original_threads

                    # ==================================================

                    if new_packages:
                        logger("...", f"Processing resources for {len(new_packages)} new packages...", level="print")
                        crawler.process_packages_batch(new_packages, phase="resources", tqdm_initial=0, tqdm_desc="Processing resources", tqdm_colour="green", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    if incomplete_packages:
                        logger("...", f"Continuing resources for {len(incomplete_packages)} incomplete packages...", level="print")
                        crawler.process_packages_batch(incomplete_packages, phase="resources", tqdm_initial=0, tqdm_desc="Continuing pending resources", tqdm_colour="yellow", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(
                        save_path=crawler.save_path,
                        accepted_types=d_types,
                        num_resources=crawler.num_resources,
                        max_workers=crawler.max_threads,
                    )
                    crawler.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_before_res, unavailable_permanent_after_res, resume_data)
                else:
                    if not id_dataset and not avoid_data and not max_packages and not categories and not d_types:
                        logger("OK", f"No packages left to process for '{crawler.dms}', everything is up-to-date", level="print")
                    else:
                        logger("OK", f"No packages left to process for '{crawler.dms}', everything is up-to-date with the configuration provided", level="print")

        else:
            logger("ERROR", "Incorrect domain form. Must have the form 'https://domain.example' or 'http://domain.example'", level="print")
    except Exception as e:
        logger("ERROR", "Unexpected error occurred", e)

if __name__ == "__main__":
    main()
