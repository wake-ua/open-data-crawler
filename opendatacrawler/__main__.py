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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--domain", type=str, required=True,
                        help="A data source (Ex. -d https://domain.example)")
    parser.add_argument("-t", "--data_types", nargs="+", required=False,
                        help="data types to save (Ex. -t xls pdf) (default: all)")
    parser.add_argument("-c", "--categories", nargs="+", required=False,
                        help="Categories to save (Ex. -c crime tourism transport) (default: all)")
    parser.add_argument("-p", "--path", type=str, required=False,
                        help="Path to save data (Ex. -p /my/example/path/)")
    parser.add_argument("-s", "--max_seconds", type=int, required=False,
                        help="Max seconds to wait for server response during file download (e.g., -s 60)")
    parser.add_argument("-pd", "--partial_dataset", required=False, action=argparse.BooleanOptionalAction,
                        help="Save partial dataset (default: not save)")
    parser.add_argument("-id", "--id_dataset", nargs="+", required=False,
                        help="Save the dataset with that id (Ex. -id edu-alu-fpa-2021) (default: all)")
    parser.add_argument("-nd", "--no_dataset", required=False, action=argparse.BooleanOptionalAction,
                        help="No save the dataset (default: save)")
    parser.add_argument("-n", "--max_packages", type=int, required=False,
                        help="Maximum number of packages to process (default: all)")
    parser.add_argument("-mt", "--max_threads", type=int, required=False,
                        help="Maximum number of threads to use (default: based on CPU count, up to 32)")
    parser.add_argument("-mrt", "--max_resource_threads", type=int, required=False,
                        help="Maximum number of threads to use per package for processing resources (default: 4)")
    parser.add_argument("-rd", "--reset-domain", required=False, action=argparse.BooleanOptionalAction,
                        help="Delete all data and logs for the specified domain before crawling")
    parser.add_argument("-nr", type=int, required=False,
                        help="Number of resources per package to download (default: all)")
    parser.add_argument("-replace", required=False, action=argparse.BooleanOptionalAction,
                        help="Force re-download of datasets specified with --id_dataset (delete old metadata and data first)")
    parser.add_argument("-country", "--countries", nargs="+", required=False, 
                        help="Filter datasets by country code (e.g. -country es gr fr)")

    args = vars(parser.parse_args())

    url = args["domain"]
    d_types = [c.lower() for c in args["data_types"]] if args["data_types"] else []
    categories = [c.lower() for c in args["categories"]] if args["categories"] else []
    d_path = args["path"]
    max_sec = args["max_seconds"]
    partial = args["partial_dataset"]
    id_dataset = args["id_dataset"]
    avoid_data = args["no_dataset"]
    max_packages = args.get("max_packages")
    max_threads = args["max_threads"] if args["max_threads"] else min(32, (os.cpu_count() or 1) * 5)
    max_resource_threads = args["max_resource_threads"] if args.get("max_resource_threads") else 4
    reset_domain = args.get("reset_domain")
    num_resources = args.get("nr")
    replace = args.get("replace")
    countries = [c.lower() for c in args["countries"]] if args["countries"] else []

    if num_resources == 0:
        avoid_data = True
        num_resources = None

    utils.print_intro()
    crawler = None
    try:
        if utils.is_url(url):
            crawler = OpenDataCrawler(url, path=d_path, data_types=d_types, categories=categories, partial=partial, avoid_data=avoid_data, max_sec=max_sec, max_threads=max_threads, max_resource_threads=max_resource_threads, num_resources=num_resources, countries=countries)

            if not crawler.dms:
                log_manager.move_to_domain("_unknownDomain", move_file=True)
                sys.exit(1)

            logger(None, "=" * 80, level="print")

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
                logger(None, "=" * 80, level="print")

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

                resume_data, downloaded_before_res, failed_before_res, unavailable_before_res, failed_before_pkgs = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)

                if resume_data:
                    logger("OK", f"Loaded resume with {len(resume_data)} packages and {len(downloaded_before_res)} downloaded resources", level="print")
                    if failed_before_pkgs:
                        logger("...", f"Reattempting {len(failed_before_pkgs)} packages with {len(failed_before_res)} failed resources of accepted types ({", ".join(f".{ext}" for ext in d_types)})...", level="print")
                    logger(None, "=" * 80, level="print")

                logger("...", f"Obtaining packages from '{crawler.get_print_domain()}'...", level="print")
                if replace and id_dataset:
                    crawler.force_replace_package(id_dataset)

                packages = id_dataset if id_dataset else crawler.get_package_list()
                new_packages = [pkg for pkg in packages if pkg not in resume_data]
                failed_packages = [pkg for pkg in packages if pkg in failed_before_pkgs]

                if max_packages:
                    new_packages = new_packages[:max_packages]

                if new_packages or failed_packages:
                    total_to_process = len(new_packages) + len(failed_packages)

                    if failed_packages:
                        logger("...",f"Queued {total_to_process} packages ({len(new_packages)} new packages and {len(failed_packages)} previously failed packages)", level="print")
                    else:
                        logger("...", f"Queued {total_to_process} packages for processing", level="print")

                    # ==================================================

                    original_threads = crawler.max_threads
                    crawler.max_threads = 1

                    if new_packages:
                        logger("...", f"Collecting metadata for {len(new_packages)} new packages...", level="print")
                        crawler.process_packages_batch(new_packages, phase="metadata", tqdm_initial=0, tqdm_desc="Collecting metadata", tqdm_colour="blue", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    if failed_packages:
                        logger("...", f"Collecting metadata for {len(failed_packages)} failed packages...", level="print")
                        crawler.process_packages_batch(failed_packages, phase="metadata", tqdm_initial=0, tqdm_desc="Collecting metadata (failed)", tqdm_colour="cyan", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res )

                    crawler.max_threads = original_threads

                    # ==================================================

                    if new_packages:
                        logger("...", f"Processing resources for {len(new_packages)} new packages...", level="print")
                        crawler.process_packages_batch(new_packages, phase="resources", tqdm_initial=0, tqdm_desc="Processing resources", tqdm_colour="green", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    if failed_packages:
                        logger("...", f"Reprocessing resources for {len(failed_packages)} failed packages...", level="print")
                        crawler.process_packages_batch(failed_packages, phase="resources", tqdm_initial=0, tqdm_desc="Reprocessing failed resources", tqdm_colour="yellow", downloaded_before_res=downloaded_before_res, failed_before_res=failed_before_res, unavailable_permanent_before_res=unavailable_before_res)

                    resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)
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
