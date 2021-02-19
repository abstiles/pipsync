#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field, InitVar
from functools import lru_cache, cached_property
from pathlib import Path
from typing import (
    Any,
    Iterator,
    List,
    Dict,
    Mapping,
    Iterable,
    ClassVar,
    Pattern,
    Union,
)

import toml

__title__ = "pipsync"
__version__ = "0.2.0"
__author__ = "Bryson Tyrrell"
__author_email__ = "bryson.tyrrell@gmail.com"
__license__ = "MIT"
__copyright__ = "Copyright 2020 Bryson Tyrrell"
__description__ = "Sync requirements.txt files with a project Pipfile."


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Requirement:
    """Structured requirement model"""

    git_parser: ClassVar[Pattern[str]] = re.compile(
        r"^(?:-e )?git\+.*#egg=(?P<package>.*)$"
    )

    package_name: str
    requirement_line: str

    @classmethod
    def parse_requirement_line(cls, requirement_line: str) -> "Requirement":
        """Create a Requirement object from a line of a requirements.txt file"""
        if git_repo := cls.git_parser.match(requirement_line):
            name = git_repo["package"].rstrip("\n")
        else:
            name = requirement_line.rstrip("\n").split("==")[0]
        return cls(name, requirement_line.rstrip("\n"))

    @classmethod
    def from_data(
        cls, package_name: str, package_data: Dict[str, Any]
    ) -> "Requirement":
        """Create a Requirement object from a Pipfile.lock package entry"""

        if version := package_data.get("version"):
            return cls(package_name, f"{package_name}{version}")

        # Git installed packages will not have version values
        if git_url := package_data.get("git"):
            prefix = "-e " if package_data.get("editable") else ""
            ref = f"@{git_ref}" if (git_ref := package_data.get("ref")) else ""
            return cls(package_name, f"{prefix}git+{git_url}{ref}#egg={package_name}")

        return cls(package_name, package_name)


class PackageProcessor:
    """Processor for parsing package dependency data in a pipenv project"""

    def __init__(
        self,
        root: Union[str, Path],
        force: bool = False,
        in_place: bool = False,
        include_dev: bool = False,
    ):
        self.root = Path(root).expanduser()
        self.pipfile = Pipfile(self.root / "Pipfile")
        self.pipfile_lock = PipfileLock(self.root / "Pipfile.lock")
        self.force = force
        self.in_place = in_place
        self.include_dev = include_dev

    @cached_property
    def graph(self) -> List[Dict[str, Any]]:
        """Get the dependency graph from pipenv"""

        return [
            *json.loads(
                subprocess.run(
                    ("pipenv", "graph", "--json"),
                    capture_output=True,
                    check=True,
                    text=True,
                    cwd=self.root,
                ).stdout
            )
        ]

    @cached_property
    def dependency_map(self) -> Dict[str, List[str]]:
        """Get a mapping of package names to package dependencies"""

        return {
            dependency["package"]["package_name"]: [
                subdep["package_name"] for subdep in dependency["dependencies"]
            ]
            for dependency in self.graph
        }

    def generate_requirements(self, requirements_list: List[Requirement]) -> List[str]:
        """Given a list of requirements, generate an updated version"""

        requirements = {
            requirement.package_name: requirement for requirement in requirements_list
        }
        environment = (
            self.pipfile.all_packages
            if self.include_dev
            else self.pipfile.default_packages
        )
        if self.include_dev:
            version_map = {**requirements, **self.pipfile_lock.dev}
        else:
            version_map = {**requirements, **self.pipfile_lock.default}

        def generate_dependencies(requirements: Iterable[str]) -> Iterator[str]:
            for requirement in requirements:
                yield requirement
                try:
                    yield from self.dependency_map[requirement]
                except KeyError:
                    logger.warning(
                        "Package %s not found in dependency graph", requirement
                    )

        root_requirements = requirements.keys() & set(environment)
        full_dependencies = {*generate_dependencies(root_requirements)}
        if self.in_place and not self.force:
            # Avoid deleting anything if rewriting a file in-place unless the
            # force flag is set, since doing so is destructive.
            full_dependencies |= requirements.keys()
        else:
            if missing := (requirements.keys() - full_dependencies):
                if self.in_place:
                    for package in missing:
                        logger.info("Force Sync: package '%s' removed", package)
                else:
                    for package in missing:
                        if (
                            not self.include_dev
                            and package in self.pipfile.dev_packages
                        ):
                            logger.info(
                                "Skipping dev dependency: package '%s'", package
                            )
                        else:
                            logger.info(
                                "Missing dependency in Pipfile: package '%s'", package
                            )
        return [version_map[dep].requirement_line for dep in sorted(full_dependencies)]


class Pipfile:
    """Parser for a Pipfile"""

    def __init__(self, pipfile_path: Union[str, Path]):
        self.path = Path(pipfile_path).expanduser()
        if not is_readable_file(pipfile_path):
            raise ValueError(f"Pipfile not found at path {pipfile_path}")

    @lru_cache(maxsize=1)
    def parse(self) -> Mapping[str, Any]:
        """Get the Pipfile contents"""
        return toml.load(self.path)

    @property
    def default_packages(self) -> Dict[str, str]:
        """Default package map"""
        return dict(self.parse()["packages"])

    @property
    def dev_packages(self) -> Dict[str, str]:
        """Development package map"""
        return dict(self.parse()["dev-packages"])

    @property
    def all_packages(self) -> Dict[str, str]:
        """Map containing all packages"""
        return {**self.parse()["packages"], **self.parse()["dev-packages"]}


class PipfileLock:
    """Parser for a Pipfile.lock"""

    def __init__(self, pipfile_lock_path: Union[str, Path]):
        self.path = Path(pipfile_lock_path).expanduser()
        if not is_readable_file(pipfile_lock_path):
            raise ValueError(f"Pipfile.lock not found at path {pipfile_lock_path}")

    @lru_cache(maxsize=1)
    def parse(self) -> Dict[str, Dict[str, Any]]:
        """Get the contents of Pipfile.lock"""
        with open(self.path, "r") as f:
            if isinstance(contents := json.load(f), dict):
                return contents
            raise ValueError("Unexpected format of Pipfile.lock contents")

    @cached_property
    def default(self) -> Dict[str, Requirement]:
        """Map of default packages to versioned requirement"""
        return {
            pkg[0]: Requirement.from_data(*pkg)
            for pkg in self.parse()["default"].items()
        }

    @cached_property
    def dev(self) -> Dict[str, Requirement]:
        """Map of develop packages to versioned requirement"""
        return {
            pkg[0]: Requirement.from_data(*pkg)
            for pkg in self.parse()["develop"].items()
        }


def detect_root() -> Path:
    """Use pipenv to find the root if possible"""

    return Path(
        subprocess.run(
            ("pipenv", "--where"), capture_output=True, check=True, text=True
        ).stdout.rstrip("\n")
    )


def configure_logger(verbose: bool = False) -> None:
    """Configure the logger. For use when invoked as a CLI tool."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipsync", description=__description__)
    parser.add_argument(
        "PATH", nargs="?", type=Path, help="Project root / Pipfile location"
    )

    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        type=Path,
        help="Exclude top level directories from requirements.txt file search",
    )

    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Remove packages in requirements.txt files that are not in the Pipfile",
    )

    parser.add_argument(
        "-i",
        "--in-place",
        action="store_true",
        help="Edit requirements.txt files in place instead of looking for direct dependency files",
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

    if parsed_args.PATH:
        path = parsed_args.PATH.expanduser()
        if path.stem == "Pipfile":
            path = path.parent
        parsed_args.PATH = path
        if not is_readable_file(path / "Pipfile.lock"):
            logger.info("Pipfile.lock not found at given path")
            raise SystemExit(1)

    return parsed_args


def is_readable_file(path: Union[str, Path]) -> bool:
    """Returns True iff path is a readable file"""
    return os.path.isfile(path) and os.access(path, os.R_OK)


def find_dependency_files(
    root_dir: Union[str, Path], name: str, exclude: List[Path]
) -> List[Path]:
    """Get a list of all paths matching name"""
    return [*recursive_search(root_dir, name, exclude)]


def recursive_search(
    root_dir: Union[str, Path], name: str, exclude: List[Path]
) -> Iterator[Path]:
    """Generate paths to files with the given name"""

    for root, dirs, _ in os.walk(root_dir):
        path = Path(root)
        dirs[:] = [d for d in dirs if path / d not in exclude and not d.startswith(".")]
        searched_file = path / name
        if is_readable_file(searched_file):
            yield searched_file


def main() -> None:
    args = arguments()
    configure_logger(args.verbose)

    if args.PATH:
        root = args.PATH
    else:
        root = detect_root()
    processor = PackageProcessor(root, args.force, args.in_place, args.dev)
    excludes = [root / dir for dir in args.exclude]
    if args.in_place:
        dependency_filename = "requirements.txt"
    else:
        dependency_filename = "requirements.direct.txt"

    dependency_files = find_dependency_files(root, dependency_filename, excludes)
    if not dependency_files:
        logger.warning("No %s files found.", dependency_filename)
        raise SystemExit

    synced_count = 0
    skipped_count = 0
    for file_path in dependency_files:
        with open(file_path, "r") as rf:
            requirements = [Requirement.parse_requirement_line(line) for line in rf]
        if req_content := processor.generate_requirements(requirements):
            logger.info("Syncing file: %s", file_path)
            with open(file_path.parent / "requirements.txt", "w") as f:
                print("\n".join(req_content), file=f)
            synced_count += 1
        else:
            logger.debug("Empty requirements file: %s", file_path)
            skipped_count += 1

    logger.info("Synced %s files | Skipped %s files", synced_count, skipped_count)


if __name__ == "__main__":
    main()
