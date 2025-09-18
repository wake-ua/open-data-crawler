import argparse
from tqdm import tqdm
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import urllib3
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
    parser.add_argument("--reset-domain", required=False, action=argparse.BooleanOptionalAction,
                        help="Delete all data and logs for the specified domain before crawling")

    args = vars(parser.parse_args())

    url = args["domain"]
    d_types = [c.lower() for c in args["data_types"]] if args["data_types"] else None
    categories = [c.lower() for c in args["categories"]] if args["categories"] else None
    d_path = args["path"]
    max_sec = args["max_seconds"]
    partial = args["partial_dataset"]
    id_dataset = args["id_dataset"]
    avoid_data = args["no_dataset"]
    max_packages = args.get("max_packages")
    max_threads = args["max_threads"] if args["max_threads"] else min(32, (os.cpu_count() or 1) * 5)
    reset_domain = args.get("reset_domain")

    utils.print_intro()
    crawler = None
    try:
        if utils.is_url(url):
            crawler = OpenDataCrawler(url, path=d_path, data_types=d_types, sec=max_sec)

            if not crawler.dms:
                log_manager.move_to_domain("_unknownDomain", move_file=True)
                sys.exit(1)

            logger(None, "=" * 80, level="print")

            resume_data, downloaded_before_res, failed_before_res, failed_before_pkgs  = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)
            has_logs = None
            if reset_domain:
                log_path = os.path.join(os.getcwd(), "logs", utils.clean_url(url))
                has_logs = os.path.isdir(log_path) and any(f.endswith(".log") for f in os.listdir(log_path))

                if resume_data:
                    logger("WARNING", f"Are you absolutely sure you want to delete all data ({len(resume_data)} packages and {len(downloaded_before_res)} resources) and logs for domain '{url}' ({crawler.dms})? This action cannot be undone. [Y/N]:", level="print")
                    reset_domain_input = input().strip().lower() in {"y", "yes"}
                elif has_logs:
                    logger("WARNING", f"No resume data found, but there are logs for domain '{url}' ({crawler.dms}). Do you want to delete them? This action cannot be undone. [Y/N]:", level="print")
                    reset_domain_input = input().strip().lower() in {"y", "yes"}
                else:
                    reset_domain_input = True

                if not reset_domain_input:
                    logger("WARNING", f"Reset for domain '{crawler.domain}' was cancelled by user", level="print")
            else:
                reset_domain_input = False

            crawler.reset_domain(reset_domain_input, resume_data, has_logs)

            if reset_domain_input:
                resume_data, downloaded_before_res, failed_before_res, failed_before_pkgs  = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)
                logger(None, "=" * 80, level="print")

            if resume_data:
                logger("OK", f"Loaded resume with {len(resume_data)} packages and {len(downloaded_before_res)} downloaded resources", level="print")
                if failed_before_pkgs:
                    logger("...", f"Reattempting {len(failed_before_pkgs)} packages with {len(failed_before_res)} failed resources of accepted types ({d_types})...", level="print")
                logger(None, "=" * 80, level="print")

            logger("...", f"Obtaining packages from '{url}'...", level="print")
            packages = id_dataset if id_dataset else crawler.get_package_list()
            logger(None, "=" * 80, level="print")

            packages_to_process = list({
                pkg for pkg in packages
                if pkg not in resume_data or pkg in failed_before_pkgs
            })

            if max_packages:
                packages_to_process = packages_to_process[:max_packages]
                tqdm_total = len(packages_to_process)
                tqdm_initial = 0
            else:
                tqdm_total = len(packages)
                tqdm_initial = len(packages) - len(packages_to_process)

            if packages_to_process:
                logger("...", f"Processing {len(packages_to_process)} packages...", level="print")

                with ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="t") as executor:
                    futures = {executor.submit(crawler.process_package, pkg_id, categories, d_types, partial, avoid_data): pkg_id for pkg_id in packages_to_process}
                    try:
                        for future in tqdm(as_completed(futures), total=tqdm_total, initial=tqdm_initial, desc="Processing...", colour="green"):
                            future.result()
                    except KeyboardInterrupt:
                        logger("WARNING", "Interrupt received. Waiting for threads to finish gracefully... (this may take a while if many threads are active)", level="print")
                        executor.shutdown(wait=False, cancel_futures=True)

                logger(None, "=" * 80, level="print")
                resume_data, downloaded_after_res, failed_after_res, _ = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)
                logger("OK", f"{len(downloaded_after_res) - len(downloaded_before_res)} new resources downloaded in this run ({max(0, len(failed_after_res) - len(failed_before_res))} new failures, {len(set(failed_before_res) - set(failed_after_res))} recovered from previous failures): {len(downloaded_after_res)} successfully downloaded resources in total across {len(resume_data)} packages ({len(failed_after_res)} failed resources in total)", level="print")
            else:
                logger("OK", f"No packages left to process for '{crawler.dms}', everything is up-to-date", level="print")
        else:
            logger("ERROR", "Incorrect domain form. Must have the form 'https://domain.example' or 'http://domain.example'", level="print")
    except Exception as e:
        logger("ERROR", "Unexpected error occurred", e)
if __name__ == "__main__":
    main()
