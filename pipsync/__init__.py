#!/usr/bin/env python3
import argparse
import json
import logging
import os

import toml

__title__ = "pipsync"
__version__ = "0.2.0"
__author__ = "Bryson Tyrrell"
__author_email__ = "bryson.tyrrell@gmail.com"
__license__ = "MIT"
__copyright__ = "Copyright 2020 Bryson Tyrrell"
__description__ = "Sync requirements.txt files with a project Pipfile."


logger = logging.getLogger(__name__)


def configure_logger(verbose=False):
    """Configure the logger. For use when invoked as a CLI tool."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")


def arguments():
    parser = argparse.ArgumentParser(prog="pipsync", description=__description__)
    parser.add_argument("PATH", help="Project root / Pipfile location")

    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        help="Exclude top level directories from requirements.txt file search",
    )

    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Remove packages in requirements.txt files that are not in the Pipfile",
    )

    parser.add_argument(
        "--dev", action="store_true", help="Include dev-packages from Pipfile."
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose console output."
    )

    parser.add_argument(
        "--version", action="version", version="pipsync {}".format(__version__)
    )

    parsed_args = parser.parse_args()

    if parsed_args.PATH.endswith("Pipfile.lock"):
        parsed_args.DIR = os.path.dirname(parsed_args.PATH)
    else:
        if not os.path.isfile(os.path.join(parsed_args.PATH, "Pipfile.lock")):
            logger.info("Pipfile.lock not found at given path")
            raise SystemExit(1)
        else:
            parsed_args.DIR = parsed_args.PATH

    if parsed_args.exclude:
        parsed_args.exclude = [
            os.path.join(parsed_args.DIR, d) for d in parsed_args.exclude
        ]

    return parsed_args


def find_requirements_files(root_dir: str, exclude: list):
    file_list = list()

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [
            d
            for d in dirs
            if os.path.join(root, d) not in exclude and not d.startswith(".")
        ]
        for file in files:
            if file.lower() == "requirements.txt":
                file_list.append(os.path.join(root, file))

    return file_list


def get_pipfile_packages(base_dir, include_dev=False):
    pipfile = toml.load(os.path.join(base_dir, "Pipfile"))

    with open(os.path.join(base_dir, "Pipfile.lock"), "r") as f:
        pipfile_lock = json.load(f)

    values = dict()

    def get_version(package, package_db):
        # Git installed packages will not have version values
        version = package_db[package].get("version")

        if not version:
            git_url = package_db[package].get("git")
            git_ref = package_db[package].get("ref")
            is_editable = package_db[package].get("editable")
            if git_url:
                prefix = '-e ' if is_editable else ''
                ref = f'@{git_ref}' if git_ref else ''
                return {"pip": f"{prefix}git+{git_url}{ref}#egg={package}"}
        else:
            return {"pip": f"{package}{version}"}

    package_data = dict(pipfile["packages"])
    if include_dev:
        package_data.update(pipfile["dev-packages"])
    locked_packages = pipfile_lock["default"]
    if include_dev:
        locked_packages.update(pipfile_lock["develop"])

    for package_name in locked_packages:
        if package_name in package_data.keys():
            values[package_name.lower()] = get_version(package_name, locked_packages)

    return values


def parse_requirements(file_path):
    with open(file_path, "r") as rf:
        requirements_raw = rf.read().splitlines()
    requirements = {}
    for line in requirements_raw:
        try:
            name = line.split("==")[0]
        except IndexError:
            continue
        requirements[name] = {"pip": line}
    return requirements


def generate_requirements(pipfile_packages, requirements_packages, force=False):
    """force: The generated requirements text will remove any packages that are not
    listed in the pipfile_packages.
    """
    content = str()
    for p in [i.lower() for i in requirements_packages.keys()]:
        try:
            content += pipfile_packages[p]["pip"] + "\n"
        except KeyError:
            if force:
                logger.info(f"Force Sync: package '{p}' removed")
            else:
                content += p + "\n"

    return content


def write_requirements(content, path):
    with open(path, "w") as f:
        f.write(content)


def main():
    args = arguments()
    configure_logger(args.verbose)

    requirements_files = find_requirements_files(args.DIR, args.exclude)
    if not requirements_files:
        logger.warning("No requirements.txt files found.")
        raise SystemExit

    pipfile_packages = get_pipfile_packages(args.DIR, args.dev)

    synced_count = 0
    skipped_count = 0
    for file in requirements_files:
        if req_content := generate_requirements(
            pipfile_packages, parse_requirements(file), args.force
        ):
            logger.info(f"Syncing file: {file}")
            write_requirements(req_content, file)
            synced_count += 1
        else:
            logger.debug(f"Empty requirements file: {file}")
            skipped_count += 1

    logger.info(f"Synced {synced_count} files | Skipped {skipped_count} files")


if __name__ == "__main__":
    main()
