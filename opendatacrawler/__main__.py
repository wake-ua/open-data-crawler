import argparse
from tqdm import tqdm
import os
import traceback
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import utils
from setup_logger import logger
from odcrawler import OpenDataCrawler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--domain", type=str, required=True,
                        help="A data source (Ex. -d https://domain.example)")
    parser.add_argument("-m", "--save_meta", required=False,
                        action=argparse.BooleanOptionalAction,
                        help="Save dataset metadata (default: not save)")
    parser.add_argument("-t", "--data_types", nargs="+", required=False,
                        help="data types to save (Ex. -t xls pdf) (default: all)")
    parser.add_argument("-c", "--categories", nargs="+", required=False,
                        help="Categories to save (Ex. -c crime tourism transport) (default: all)")
    parser.add_argument("-p", "--path", type=str, required=False,
                        help="Path to save data (Ex. -p /my/example/path/)")
    parser.add_argument("-s", "--max_seconds", type=int, required=False,
                        help="Max seconds to wait for server response during file download (e.g., -s 60)")
    parser.add_argument("-pd", "--partial_dataset", required=False,
                        action=argparse.BooleanOptionalAction,
                        help="Save partial dataset (default: not save)")
    parser.add_argument("-id", "--id_dataset", nargs="+", required=False,
                        help="Save the dataset with that id (Ex. -id edu-alu-fpa-2021) (default: all)")
    parser.add_argument("-nd", "--no_dataset", required=False,
                        action=argparse.BooleanOptionalAction,
                        help="No save the dataset (default: save)")
    parser.add_argument("-mt", "--max_threads", type=int, required=False,
        help="Maximum number of threads to use (default: based on CPU count, up to 32)")

    args = vars(parser.parse_args())

    url = args["domain"]
    save_meta = args["save_meta"]
    d_types = [c.lower() for c in args["data_types"]] if args["data_types"] else None
    categories = [c.lower() for c in args["categories"]] if args["categories"] else None
    d_path = args["path"]
    max_sec = args["max_seconds"]
    partial = args["partial_dataset"]
    id_dataset = args["id_dataset"]
    avoid_data = args["no_dataset"]
    max_threads = args["max_threads"] if args["max_threads"] else min(32, (os.cpu_count() or 1) * 5)

    utils.print_intro()
    crawler = None

    try:
        if utils.check_url(url):
            crawler = OpenDataCrawler(url, path=d_path, data_types=d_types, sec=max_sec)

            if not crawler.dms:
                sys.exit(1)

            logger(None, "=" * 80, level="print")

            resume_data, downloaded_before_res, failed_before_res, failed_before_pkgs  = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)
         
            if resume_data:
                logger("OK", f"Loaded resume with {len(resume_data)} packages and {len(downloaded_before_res)} downloaded resources", level="print")
                logger("...", f"Reattempting {len(failed_before_pkgs)} packages with {len(failed_before_res)} failed resources of accepted types ({d_types})", level="print")
                logger(None, "=" * 80, level="print")

            logger("...", f"Obtaining packages from '{url}'...", level="print")
            packages = id_dataset if id_dataset else crawler.get_package_list()
            logger(None, "=" * 80, level="print")

            packages_to_process = list({
                pkg for pkg in packages
                if pkg not in resume_data or pkg in failed_before_pkgs
            })
            
            if packages_to_process:
                logger("...", f"Processing {len(packages_to_process)} packages...", level="print")

                with ThreadPoolExecutor(max_workers=max_threads) as executor:
                    futures = {
                        executor.submit(
                            crawler.process_package,
                            pkg_id,
                            categories,
                            d_types,
                            partial,
                            save_meta,
                            avoid_data
                        ): pkg_id for pkg_id in packages_to_process
                    }

                    try:
                        for future in tqdm(as_completed(futures), total=len(futures), desc="Processing...", colour="green"):
                            future.result()
                    except KeyboardInterrupt:
                        logger("WARNING", "Interrupt received. Waiting for threads to finish gracefully... (this may take a while if many threads are active)", level="print")
                        executor.shutdown(wait=False, cancel_futures=True)
                        
                logger(None, "=" * 80, level="print")

                resume_data, downloaded_after_res, failed_after_res, _ = utils.recover_resume(save_path=crawler.save_path, accepted_types=d_types)

                logger("OK", f"{len(downloaded_after_res) - len(downloaded_before_res)} new resources downloaded in this run ({max(0, len(failed_after_res) - len(failed_before_res))} new failures, {len(set(failed_before_res) - set(failed_after_res))} recovered from previous failures): {len(downloaded_after_res)} successfully downloaded resources in total across {len(resume_data)} packages ({len(failed_after_res)} failed resources in total)", level="print")
            else:
                logger("ERROR", "No packages to process or an error occurred.")
        else:
            logger("ERROR", "Incorrect domain form. Must have the form 'https://domain.example' or 'http://domain.example'")
    except Exception:
        logger("ERROR", "Unexpected error occurred", f"\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()