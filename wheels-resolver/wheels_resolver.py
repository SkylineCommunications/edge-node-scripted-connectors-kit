import subprocess
import shutil
import sys
import re
from pathlib import Path
from subprocess import CompletedProcess
import http.client
import ssl
import argparse 
import logging
import importlib.util
import os
import enum
from tempfile import TemporaryDirectory

EXIT_SUCCESS = 0
EXIT_PIP_NOT_FOUND = 1
EXIT_CONFLICTING_DEPENDENCIES = 2
EXIT_REQUIREMENTS_NOT_FOUND = 3
EXIT_PYTHON_TOO_OLD = 4
EXIT_UNEXPECTED_ERROR = 99

# make logger available for everything in this script
logger = logging.getLogger(__name__)

# Needs to be above the class & function declarations,
# otherwise Python will already fail on the type hints defined while parsing the remainder of the file
if sys.version_info < (3, 10):
    logger.critical("ERROR: This script requires Python 3.10 or later")
    sys.exit(EXIT_PYTHON_TOO_OLD)

def configure_logging(verbose: bool, log_file: Path) -> None:
    """
    Configure logging to a logfile.

    Args:
        verbose: Whether debug logging should be enabled.
        log_file: Path to the logfile.
    """

    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, mode="w", encoding="utf-8")],
        force=True,
    )

class SupportedPlatform(enum.Enum):
    WINDOWS = "x86_64-windows-msvc"
    LINUX = "x86_64-linux-gnu"

class PipNotFoundException(Exception):
    """
    Exception raised when no valid pip executable can be found.
    """

    def __init__(self):
        self.message = """No valid pip executable found.
        Please make sure pip is installed and available in the PATH, or that you are running this script with a Python installation that includes pip.
        If you are using Python through uv, add 'pip' to your environment by executing 'uv pip install pip'. This is necessary as uv's pip (accessible through 'uv pip') does not include the 'download' command.
        """
        super().__init__(self.message)

class ConflictingDependenciesException(Exception):
    """
    Exception raised when conflicting dependencies are detected, making it impossible to resolve them.
    """

    stdout: str
    stderr: str

    def __init__(self, pip_stdout: str, pip_stderr: str):
        self.stdout = pip_stdout
        self.stderr = pip_stderr

        self.message = f"Conflicting dependencies found:\n{pip_stderr}\n\nPip output for diagnostics:\n{pip_stdout}"
        super().__init__(self.message)

class RequirementsNotFoundException(Exception):
    """
    Exception raised when the provided requirements file does not exist.
    """

    requirements: Path

    def __init__(self, requirements: Path):
        self.requirements = requirements
        self.message = f"Requirements file {requirements} not found"
        super().__init__(self.message)

class PipResolver():
    """
    Utility class to resolve the pip executable to use for dependency resolution and wheel downloading.
    """

    @staticmethod
    def resolve_pip() -> list[str]:
        """
        Resolve the pip executable to use.

        Returns:
            A list containing the command to invoke pip.

        Raises:
            PipNotFoundException: If no valid pip executable is found.
        """

        if importlib.util.find_spec("pip") is not None:
            return [str(sys.executable), "-m", "pip"]
        elif (executable_path := shutil.which("pip3")) and executable_path is not None and os.access(executable_path, os.X_OK):
            return ["pip3"]
        elif (executable_path := shutil.which("pip")) and executable_path is not None and os.access(executable_path, os.X_OK):
            return ["pip"]
        else:
            raise PipNotFoundException()

class WebRequester:
    """
    Minimal HTTP/HTTPS client with manual redirect handling.
    Used to retrieve simple web resources without external dependencies.
    """

    __max_redirect: int

    def __init__(self, max_redirect: int = 10) -> None:
        """
        Initialize the requester.

        Args:
            max_redirect: Maximum number of redirects to follow.
        """

        self.__max_redirect = max_redirect

    def __manual_url_split(self, url: str) -> tuple[str, str, str]:
        """
        Split a URL into scheme, host, and path using simple string parsing.

        Args:
            url: URL to split.

        Returns:
            Tuple containing (scheme, host, path).
        """

        # Remove 'https://' or 'http://'
        if "://" in url:
            scheme, rest = url.split("://", 1)
        else:
            scheme = "http"
            rest = url
        
        # Split the host from the path
        if "/" in rest:
            host, path = rest.split("/", 1)
            path = "/" + path
        else:
            host = rest
            path = "/"
            
        return scheme, host, path
    
    def get(self, url: str) -> str | None:
        """
        Retrieve the contents of a URL.

        Args:
            url: Target URL.

        Returns:
            Response body as a string, or None if the request fails.
        """

        (scheme, host, path) = self.__manual_url_split(url)

        try:
            return self.__get_recursive(scheme, host, path, 0)
        except Exception as e:
            logger.warning("An exception occurred while getting %s: %s", url, e)
            return None


    def __get_recursive(self, scheme: str, host: str, path: str, rec_depth: int) -> str | None:
        """
        Perform an HTTP GET request and follow redirects recursively.

        Args:
            scheme: URL scheme (http or https).
            host: Target hostname.
            path: Request path.
            rec_depth: Current recursion depth due to the following of redirects.

        Returns:
            Response body as a string, or None on failure.
        """

        if rec_depth >= self.__max_redirect:
            return None
        
        if scheme == "https":
            # ideally the context is verified, but we have to use an unverified context since some mirrors don't have a fully valid certificate
            # this causes the request to otherwise fail
            context = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(host, context=context)
        else:
            conn = http.client.HTTPConnection(host)
        
        try:
            conn.request("GET", path)
            response = conn.getresponse()

            # Check if we need to redirect
            if response.status in (301, 302, 307, 308):
                redirect_url = response.getheader('Location')
                
                if redirect_url is None:
                    return None
                
                # the redirect URL can be absolute (e.g. https://example.com/path), but also relative (e.g. /path)
                # in the relative case we need to keep the same host name as currently already used
                (new_scheme, new_host, new_path) = self.__manual_url_split(redirect_url)
                if new_host == "":
                    new_host = host

                return self.__get_recursive(new_scheme, new_host, new_path, rec_depth + 1) # Recursive call
            
            if response.status != 200:
                logger.warning("WebRequester received non-200 response: %s %s", response.status, response.reason)
                return None

            return response.read().decode()
        finally:
            conn.close()

class WheelsResolver:
    """
    Resolves and downloads Python wheels for Windows and Linux platforms
    and organizes them into platform-specific directories.
    """

    __windows_dir: Path
    __linux_dir: Path
    __universal_dir: Path
    __web_requester: WebRequester
    __pip_command: list[str]

    def __init__(self, output_dir: Path) -> None:
        """
        Initialize the resolver and prepare output directories.

        Args:
            output_dir: Output directory 
        """

        self.__web_requester = WebRequester(10)

        dependencies_dir = output_dir / "dependencies"
        self.__windows_dir = dependencies_dir / SupportedPlatform.WINDOWS.value
        self.__linux_dir = dependencies_dir / SupportedPlatform.LINUX.value
        self.__universal_dir = dependencies_dir / "universal"

        # if it already exists, just delete to be sure that it is empty
        if dependencies_dir.exists():
            shutil.rmtree(dependencies_dir)

        output_dir.mkdir(exist_ok=True)
        dependencies_dir.mkdir()
        self.__windows_dir.mkdir()
        self.__linux_dir.mkdir()
        self.__universal_dir.mkdir()

        self.__pip_command = PipResolver.resolve_pip()

    def __resolve_latest_glibc2_version(self, base_url: str) -> int | None:
        """
        Detect the latest available glibc 2.x version from a mirror page.

        Args:
            base_url: URL listing glibc release archives.

        Returns:
            Latest glibc minor version, or None if detection fails.
        """

        html = self.__web_requester.get(base_url)

        if html is None:
            return None

        # Extract all glibc versions
        matches = re.findall(r"glibc-2.([0-9]+)(\.[0-9]+)?\.tar\.xz", html)

        versions = {int(major) for major, _ in matches}
        
        # Get the latest version
        latest_version = max(versions)
        logger.info("Found latest glibc version 2.%s", latest_version)
        return latest_version

    def __run_pip_download(self, requirements: Path, python_version: str, platform_tags: list[str], dest: Path) -> CompletedProcess[str]:
        """
        Execute `pip download` for a given platform configuration.

        Args:
            requirements: Path to the requirements file.
            python_version: Target Python version passed to pip (e.g. "314").
            platform_tags: Platform tags passed to pip.
            dest: Directory where wheels will be downloaded.

        Returns:
            CompletedProcess containing pip execution results.
        """
        
        cmd: list[str] = [
            *self.__pip_command,
            "download",
            "-r",
            str(requirements),
            "--only-binary=:all:",
            "--dest",
            str(dest),
            "--implementation",
            "cp",
            "--python-version",
            python_version,
        ]
        
        for platform in platform_tags:
            cmd.extend(["--platform", platform])

        return subprocess.run(cmd, text=True, capture_output=True)

    @staticmethod
    def __is_universal_wheel(wheel_name: str) -> bool:
        """
        Check whether a wheel is platform-independent.

        Args:
            wheel_name: Wheel filename.

        Returns:
            True if the wheel is universal (none-any), otherwise False.
        """

        return wheel_name.endswith("-none-any.whl")


    @staticmethod
    def __package_prefix(wheel_name: str) -> str:
        """
        Extract the 'package-version' prefix from a wheel filename.

        Args:
            wheel_name: Wheel filename.

        Returns:
            Package name and version prefix.
        """

        return "-".join(wheel_name.split("-", 3)[:2])


    @staticmethod
    def __universal_wheel_map(wheels: set[str]) -> dict[str, str]:
        """
        Build a mapping of package-version to universal wheel filenames.

        Args:
            wheels: Set of wheel filenames.

        Returns:
            Dictionary mapping package-version prefix to wheel name.
        """

        return {
            WheelsResolver.__package_prefix(w): w
            for w in wheels
            if WheelsResolver.__is_universal_wheel(w)
        }
    
    def __replace_with_universal(
        self,
        wheels: set[str],
        universal_map: dict[str, str],
        wheels_source_dir: Path,
        universal_source_dir: Path
    ) -> None:
        """
        Replace platform-specific wheels with universal wheels when available.

        Args:
            wheels: Wheels to inspect for replacement.
            universal_map: Mapping of available universal wheels.
            wheels_source_dir: Directory containing the platform wheels.
            universal_source_dir: Directory where universal wheels originate.
        """

        for wheel in wheels:
            prefix = WheelsResolver.__package_prefix(wheel)

            if prefix in universal_map:
                universal_wheel = universal_map[prefix]
                (universal_source_dir / universal_wheel).replace(self.__universal_dir / universal_wheel)
                (wheels_source_dir / wheel).unlink()

    def __process_downloaded_wheels(self) -> None:
        """
        Deduplicate downloaded wheels and move shared or universal wheels
        to the universal directory.
        """

        windows = self.__extract_wheels_from_folder(self.__windows_dir)
        linux = self.__extract_wheels_from_folder(self.__linux_dir)

        self.__move_shared_wheels(windows, linux)
        self.__prefer_universal_wheels(windows, linux)
        

    def __move_shared_wheels(self, windows: set[str], linux: set[str]) -> None:
        """
        Move wheels present on both platforms to the universal directory.

        Args:
            windows: Set of Windows wheel filenames.
            linux: Set of Linux wheel filenames.
        """

        shared = windows & linux

        for wheel in shared:
            (self.__windows_dir / wheel).replace(self.__universal_dir / wheel)
            (self.__linux_dir / wheel).unlink()

        windows.difference_update(shared)
        linux.difference_update(shared)

    def __prefer_universal_wheels(self, windows: set[str], linux: set[str]) -> None:
        """
        Prefer universal wheels when one platform has a platform-specific wheel
        but the other has a universal wheel.

        Even though universal wheels are slower than their platform-specific counterparts. This reduces the package size, which is more important for us.
        """

        win_universal = self.__universal_wheel_map(windows)
        lin_universal = self.__universal_wheel_map(linux)

        self.__replace_with_universal(linux, win_universal, self.__linux_dir, self.__windows_dir)
        self.__replace_with_universal(windows, lin_universal, self.__windows_dir, self.__linux_dir)

    @staticmethod
    def __extract_wheels_from_folder(wheels_folder: Path) -> set[str]:
        """
        List all wheel filenames in a directory.

        Args:
            wheels_folder: Folder containing wheel files.

        Returns:
            Set of wheel filenames.
        """

        return {wheel.name for wheel in wheels_folder.glob("*.whl")}

    def __check_conflicting_dependencies(self, requirements: Path, python_version: str) -> None:
        """
        Perform a pip dry-run install to detect dependency conflicts.

        Args:
            requirements: Path to the requirements file.
            python_version: Target Python version used for dependency resolution (e.g. "314").

        Raises:
            ConflictingDependenciesException: If pip detects conflicting dependencies.
        """

        logger.info("Checking for possible conflicting dependencies...")

        cmd = [
            *self.__pip_command,
            "install",
            "--break-system-packages", # necessary to override an error that can occur when the Python we are running is externally managed (e.g. installed through uv). But because we are doing a dry-run, we should not have any impact and can safely override
            "--dry-run",
            "--only-binary=:all:",
            "-r",
            requirements,
            "--implementation",
            "cp",
            "--python-version",
            python_version,
        ]

        dry_run_result = subprocess.run(cmd, text=True, capture_output=True)
        if dry_run_result.returncode != 0 and "conflicting dependencies" in dry_run_result.stderr:
            raise ConflictingDependenciesException(dry_run_result.stdout, dry_run_result.stderr)

    def __download_windows_wheels(self, requirements: Path, python_version: str) -> SupportedPlatform | None:
        """
        Download wheels compatible with Windows.

        Args:
            requirements: Path to the requirements file.
            python_version: Target Python version used when resolving wheels (e.g. "314").
        """

        logger.info("Downloading Windows wheels...")
        pip_install_result = self.__run_pip_download(requirements, python_version, ["win_amd64"], self.__windows_dir)
        if pip_install_result.returncode != 0:
            logger.critical("Could not download the dependencies for Windows\nstdout: %s\nstderr: %s", pip_install_result.stdout, pip_install_result.stderr)
            return None
        
        return SupportedPlatform.WINDOWS
        
    
    def __download_linux_wheels(self, requirements: Path, python_version: str) -> SupportedPlatform | None:
        """
        Download wheels compatible with Linux using manylinux platform tags.

        Args:
            requirements: Path to the requirements file.
            python_version: Target Python version used when resolving wheels (e.g. "314").
        """

        logger.info("Downloading Linux wheels...")

        # Resolve the max available gblic version so we known which platform tags exists and we should try
        min_compatible_glibc2 = 17
        latest_glibc2 = self.__resolve_latest_glibc2_version("https://ftpmirror.gnu.org/glibc")
        if latest_glibc2 is None:
            logger.warning("Could not resolve the latest available glibc 2.X version, falling back to 2.43 as latest known version")
            latest_glibc2 = 43 # latest known at moment of writing this, so the script does not fail completely

        # try to download the dependencies, prefer the platform with the lowest glibc version
        # When providing multiple "valid" platform tags, pip will automatically take the first one in the list that works for each package.
        # Since this list contains all platforms in order old -> new. We will automatically select the package built for the lowest gclib version available, maximizing compatibility for the packages.
        
        platform_tags = ["manylinux2014_x86_64"] # old name for manylinux_2_17_x86_64, so this is also allowed for our use case
        platform_tags.extend(f"manylinux_2_{version}_x86_64" for version in range(min_compatible_glibc2, latest_glibc2))
        pip_install_result = self.__run_pip_download(requirements, python_version, platform_tags, self.__linux_dir)
        if pip_install_result.returncode != 0:
            logger.critical("Could not download the dependencies for Linux\nstdout: %s\nstderr: %s", pip_install_result.stdout, pip_install_result.stderr)
            return None
        
        return SupportedPlatform.LINUX


    def download_wheels(self, requirements: Path, python_version: str) -> list[SupportedPlatform]:
        """
        Resolve dependencies and download wheels for Windows and Linux.

        The downloaded wheels are organized into platform-specific and
        universal directories.

        Args:
            requirements: Path to the requirements file.
            python_version: Target Python version for which dependencies should be resolved (pip format, e.g. "314").
        """

        self.__check_conflicting_dependencies(requirements, python_version)

        supported_platforms = supported_platforms = [
            platform_result
            for platform_result in [
                self.__download_windows_wheels(requirements, python_version),
                self.__download_linux_wheels(requirements, python_version)
            ]
            if platform_result is not None
        ]

        self.__process_downloaded_wheels()
        return supported_platforms

    @classmethod
    def check_wheel_availability(cls, requirement_spec: str, python_version: str) -> list[SupportedPlatform]:
        """
        Check whether wheels are available for a single requirement specification.

        This reuses the normal resolution flow, but performs all work in a
        temporary directory that is removed automatically afterwards.

        Args:
            requirement_spec: Single requirement line, e.g. "requests" or
                "requests==2.33.1".
            python_version: Target Python version for pip, e.g. "314".
        """

        normalized_requirement = requirement_spec.strip()
        if normalized_requirement == "":
            raise ValueError("Requirement specification cannot be empty")

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            requirements_file = temp_path / "requirements.txt"
            output_dir = temp_path / "output"

            requirements_file.write_text(f"{normalized_requirement}\n", encoding="utf-8")

            resolver = cls(output_dir=output_dir)
            return resolver.download_wheels(requirements_file, python_version)

def execute_resolve_mode(package: str, python_version: str, python_version_pip_format: str) -> None:
    """
    Execute the 'resolve' mode of the script with the given arguments.

    Args:
        package: Single requirement specification to check, e.g. 'requests' or 'requests==2.33.1'.
        python_version: Target Python version for pip, e.g. "3.14".
        python_version_pip_format: Target Python version in pip format, e.g. "314".
    """
    logger.info("Checking wheel availability for %s for Python version %s", package, python_version)
    supported_platforms = WheelsResolver.check_wheel_availability(package, python_version_pip_format)
    if supported_platforms:
        print(f"Wheels are available for platforms: [{', '.join(p.value for p in supported_platforms)}]")
    else:
        print("No compatible wheels found")

def execute_download_mode(requirements: Path, python_version: str, python_version_pip_format: str, output: Path) -> None:
    """
    Execute the 'download' mode of the script with the given arguments.
    
    Args:
        requirements: Path to the requirements file.
        python_version: Target Python version for pip (e.g. "3.14").
        python_version_pip_format: Target Python version in pip format (e.g. "314").
        output: Path to the output directory where wheels will be downloaded.
    """
    if not requirements.exists():
        raise RequirementsNotFoundException(requirements)

    logger.info("Downloading dependencies from %s for Python version %s to %s", requirements.absolute(), python_version, output.absolute())
    wheels_resolver = WheelsResolver(output_dir=output)
    supported_platforms = wheels_resolver.download_wheels(requirements, python_version_pip_format)
    if supported_platforms:
        print(f"Successfully downloaded wheels for platforms: [{', '.join(p.value for p in supported_platforms)}]")
    else:
        print("Could not download wheels for any platform")

def add_download_parser(subparsers: argparse._SubParsersAction) -> None:
    """
    Adds the parser for the 'download' mode to the given subparsers collection.
    
    Args:
        subparsers: The subparsers collection to which the 'download' parser will be added.
    """
    download_parser = subparsers.add_parser(
        "download",
        help="Resolve and download wheels from a requirements file"
    )

    download_parser.add_argument(
        "-r", "--requirements",
        required=True,
        help="Path to the requirements.txt or requirements.in file to parse"
    )

    download_parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path to the output folder. WARNING! If an existing folder is provided, which already has a 'dependencies' subfolder, then the existing 'dependencies' subfolder will be deleted and replaced with the new one containing the downloaded wheels."
    )

    download_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enables verbose logging"
    )

    download_parser.add_argument(
        "-l", "--log-file",
        default="wheels_resolver.log",
        help="Path to the logfile. Defaults to wheels_resolver.log in the current working directory."
    )

def add_resolve_parser(subparsers: argparse._SubParsersAction) -> None:
    """
    Adds the parser for the 'resolve' mode to the given subparsers collection.
    
    Args:
        subparsers: The subparsers collection to which the 'resolve' parser will be added.
    """
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve for which platforms wheels are available for a single requirement specification"
    )

    resolve_parser.add_argument(
        "-P", "--package",
        required=True,
        help="Single requirement specification to check, e.g. 'requests' or 'requests==2.33.1'."
    )

    resolve_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enables verbose logging"
    )

    resolve_parser.add_argument(
        "-l", "--log-file",
        default="wheels_resolver.log",
        help="Path to the logfile. Defaults to wheels_resolver.log in the current working directory."
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="WheelResolver.py",
        description="This program downloads all Python wheels for Linux and Windows for the dependencies specified in the provided requirements.txt file"
    )
    
    subparsers = parser.add_subparsers(dest="mode", required=True)
    add_download_parser(subparsers)
    add_resolve_parser(subparsers)
    args = parser.parse_args()

    log_file = Path(args.log_file)
    configure_logging(args.verbose, log_file)

    python_version: str = "3.14"
    python_version_pip_format = "314"

    # Run the selected mode
    try:
        if args.mode == "resolve":
            execute_resolve_mode(args.package, python_version, python_version_pip_format)
        elif args.mode == "download":
            execute_download_mode(Path(args.requirements), python_version, python_version_pip_format, Path(args.output))
        else:
            pass # argparse ensures this branch is never reached as providing an invalid mode will already stop the application with an error message
    except PipNotFoundException as e:
        logger.critical(e.message)
        sys.exit(EXIT_PIP_NOT_FOUND)
    except ConflictingDependenciesException as e:
        logger.critical(e.message)
        sys.exit(EXIT_CONFLICTING_DEPENDENCIES)
    except RequirementsNotFoundException as e:
        logger.critical(e.message)
        sys.exit(EXIT_REQUIREMENTS_NOT_FOUND)
    except Exception as e:
        logger.critical("An unexpected error occurred: %s", e)
        sys.exit(EXIT_UNEXPECTED_ERROR)
