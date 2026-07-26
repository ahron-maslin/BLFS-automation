import tarfile
import zipfile
import os
import logging
from shutil import rmtree
import wget
from termcolor import colored

from .define import DbTypes, ROOT_PATH, EXCEPTIONS, EXTENSIONS, DOWNLOAD_PATH, INSTALLED_PATH
from .utils import run_cmd, md5_check, safe_extract, rlinput, check_dir

class Commands(object):
    """Class to handle installation of BLFS packages.

    Args:
        database (dict): Dictionary containing information on BLFS packages.
        installed (list): List of installed BLFS packages.

    Attributes:
        database (dict): Dictionary containing information on BLFS packages.
        installed (list): List of installed BLFS packages.

    """

    def __init__(self, database, installed):
        """Initializes Commands with a database of BLFS packages and a list of installed packages.

        Args:
            database (dict): Dictionary containing information on BLFS packages.
            installed (list): List of installed BLFS packages.

        """
        self.database = database
        self.installed = installed
        # Set once a build directory exists; cleanup() may fire before then.
        self.package_dir = None

    def write_installed_log(self):
        """Writes the list of installed packages to a logfile."""
        # Write-then-rename: a crash or power loss mid-write must not truncate
        # the record of what is already on the system.
        tmp_path = f'{INSTALLED_PATH}.tmp'
        try:
            with open(tmp_path, 'w') as install_file:
                for i in self.installed:
                    install_file.write(f'{i}\n')
            os.replace(tmp_path, INSTALLED_PATH)
        except OSError as exc:
            logging.error(colored(
                f'Could not write installed-package log ({exc}) - '
                f'run as root so progress can be recorded.', "red"))
    
    def cleanup(self, signum, frame):
        """Cleans up installation when the user interrupts it using the keyboard interrupt (ctrl-c).

        Args:
            signum (int): The signal number.
            frame (object): The current stack frame.

        """
        os.chdir(ROOT_PATH)
        if self.package_dir and os.path.exists(self.package_dir):
            rmtree(self.package_dir, ignore_errors=True)

        logging.error(colored('Installation interrupted - exiting.', 'red'))
        self.write_installed_log()
        exit(1)

    def check_pkg_status(self, pkg, kconf=False):
        """Checks the status of the given package and logs any relevant information.

        Args:
            pkg (str): The name of the package to check.
            kconf (bool): If True, logs kernel configuration information.

        """
        if pkg not in self.database:
            return
        if self.database[pkg][DbTypes.TYPE] != 'BLFS':
            urls = self.database[pkg][DbTypes.URL]
            where = f', you can download it at {urls[0]}' if urls else ''
            logging.info(f'"{pkg}" is not a BLFS package{where}')
        if kconf:
            if self.database[pkg][DbTypes.KCONF]:
                logging.info('This package requires some kernel configuration before installation.\n')
                for conf in self.database[pkg][DbTypes.KCONF]:
                    logging.info(f'{conf}\n')

    def search(self, pkg):
        """Searches for the given package in the database and logs any relevant information.

        Args:
            pkg (str): The name of the package to search for.

        """
        if len(pkg) < 3:
            logging.error(colored('The inputted value needs to be at least 3 characters.', 'red'))
            raise SystemExit(1)
        if pkg in self.database:
            logging.info(f'"{pkg}" package exists in database.')
            return

        matches = self.find_matches(pkg)
        if not matches:
            logging.error(colored(f'"{pkg}" package not found in database.', "red"))
            raise SystemExit(1)

        logging.info(colored(
            f'"{pkg}" package not found in database, but we found similar ones.\n', "blue"))
        for item in matches:
            logging.info(item)
        raise SystemExit(1)

    def find_matches(self, pkg):
        """Returns database entries whose name contains pkg, case-insensitively.

        Args:
            pkg (str): The substring to look for.

        Returns:
            list: Matching package names.

        """
        needle = pkg.lower()
        return [name for name in self.database if needle in name.lower()]

    def list_commands(self, pkg):
        """Lists the installation commands for a given BLFS package.

        Args:
            pkg (str): The name of the package to list commands for.

        Returns:
            list: A list of the installation commands for the given package.

        """
        self.search(pkg)
        self.check_pkg_status(pkg, kconf=True)

        commands_list = list(map(lambda x: x, self.database[pkg][DbTypes.COMMANDS]))
        return commands_list

    def build_pkg(self, pkg, force=None):
        """
        Installs a given BLFS package on the system.

        Args:
            pkg (str): The name of the package to install.
            force (bool, optional): A flag to force the installation even if the package is already installed.

        Returns:
            None
        """
        self.search(pkg)
        pkg_queue = self.list_deps(pkg)
        self.download_deps(pkg_queue)
        for package in pkg_queue:
            self.install_package(package, force)
    
    def install_package(self, pkg, force):
        """
        Installs a BLFS package on the system.

        Args:
            pkg (str): The name of the package to install.
            force (bool): A flag to force the installation even if the package is already installed.

        Returns:
            None
        """
        if pkg in self.installed and not force:
            logging.info(colored(f'"{pkg}" has already been installed - skipping', "blue"))
            return

        if pkg in EXCEPTIONS:
            logging.info(colored(
                f'"{pkg}" is a book section, not a single package - '
                f'follow the BLFS book to install it manually.', "blue"))
            return

        if self.database.get(pkg, {}).get(DbTypes.TYPE) != 'BLFS':
            self.check_pkg_status(pkg)
            logging.info(colored(
                f'"{pkg}" is not a BLFS package - install it manually, '
                f'then re-run to continue.', "blue"))
            return

        logging.info(colored(f'Installing {pkg}.\n', "green"))
        os.chdir(DOWNLOAD_PATH)

        if not self._extract_source(pkg):
            return

        self.package_dir = os.getcwd()
        try:
            if not self._run_install_commands(pkg):
                logging.error(colored(
                    f'Build of "{pkg}" failed - leaving {self.package_dir} '
                    f'in place so you can inspect it.', "red"))
                return
            # Record regardless of --force: the package really is on the system
            # now, and dropping it would make the next run rebuild it.
            if pkg not in self.installed:
                self.installed.append(pkg)
            self.write_installed_log()
            logging.info(colored(f'Successfully installed {pkg}!', "green"))
        finally:
            os.chdir(DOWNLOAD_PATH)

        rmtree(self.package_dir, ignore_errors=True)
        self.package_dir = None

    def _extract_source(self, pkg):
        """Extracts the source tarball/zip for pkg and enters its directory.

        Args:
            pkg (str): The name of the package to extract.

        Returns:
            bool: True if the source was extracted and entered, else False.

        """
        urls = self.database[pkg][DbTypes.URL]
        if not urls:
            logging.error(colored(
                f'No download URL recorded for "{pkg}" - skipping.', "red"))
            return False

        archive = os.path.basename(urls[0])
        if not os.path.isfile(archive):
            logging.error(colored(
                f'Source archive "{archive}" for {pkg} is missing - '
                f'run with -d to download it first.', "red"))
            return False

        try:
            if tarfile.is_tarfile(archive):
                with tarfile.open(archive, 'r') as tar_ref:
                    safe_extract(tar_ref)
                    top_level = tar_ref.getnames()[0].split('/', 1)[0]
                os.chdir(top_level)
                return True

            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive, 'r') as zip_ref:
                    zip_new_dir = os.path.splitext(archive)[0]
                    zip_ref.extractall(zip_new_dir)
                os.chdir(zip_new_dir)
                return True
        except (OSError, tarfile.TarError, zipfile.BadZipFile, IndexError) as exc:
            logging.error(colored(f'Could not extract {archive}: {exc}', "red"))
            return False

        logging.error(colored(
            f'"{archive}" is neither a tar nor a zip archive - skipping.', "red"))
        return False

    def _run_install_commands(self, pkg):
        """Prompts for and runs each build command for pkg.

        Args:
            pkg (str): The name of the package being built.

        Returns:
            bool: True if every command the user chose to run succeeded.

        """
        for command in self.list_commands(pkg):
            answer = input(
                f'Should I run \n"{command}"\n <Y/n/m (modify)/q (quit)> ').strip().lower()[:1]

            if answer == 'n':
                continue
            if answer == 'q':
                logging.info(colored('Aborting this package.', "blue"))
                return False
            if answer == 'm':
                command = rlinput('Custom command to run: ', command)

            if run_cmd(command) != 0:
                return False
        return True

    def download_deps(self, dlist):
        """Downloads all urls in dlist (can be all urls or just some dependencies).

        Args:
            dlist (list): A list of package dependencies to download.

        Returns:
            None

        Raises:
            None

        """
        check_dir()
        for pkg in dlist:
            if pkg in EXCEPTIONS:
                logging.info(f'"{pkg}" package must be installed manually.')
                continue
            if pkg not in self.database:
                logging.warning(colored(
                    f'"{pkg}" is referenced as a dependency but is not in the '
                    f'database - install it manually.', "yellow"))
                continue

            urls = self.database[pkg][DbTypes.URL]
            hashes = self.database[pkg][DbTypes.HASHES]
            self.check_pkg_status(pkg)

            for index, url in enumerate(urls):
                if not any(ext in url for ext in EXTENSIONS):
                    continue
                # The book lists one MD5 sum for the tarball; extra URLs are
                # patches and have none. zip() used to silently drop them.
                hash_val = hashes[index] if index < len(hashes) else None
                filename = os.path.basename(url)

                if os.path.isfile(filename):
                    logging.info(colored(f'{filename} already has been downloaded', "blue"))
                    continue

                logging.info(colored(f'\nDownloading: {url}\n', "green"))
                try:
                    wget.download(url, filename)
                except Exception as exc:
                    # A half-written file would masquerade as a good download.
                    if os.path.isfile(filename):
                        os.remove(filename)
                    logging.error(colored(f'\nFailed to download {url}: {exc}', "red"))
                    continue
                print(f'\nSuccessfully downloaded {url}')
                md5_check(filename, hash_val)

    def list_deps(self, pkg, rec=None, opt=None):
        """Lists all dependencies (can be required, recommended, and/or optional).

        Args:
            pkg (str): The name of the package to list dependencies for.
            rec (bool): Whether to list recommended dependencies.
            opt (bool): Whether to list optional dependencies.

        Returns:
            A list of dependencies.

        Raises:
            None

        """
        if pkg not in self.database:
            self.search(pkg)
            return [pkg]

        order = []
        # None = unseen, False = on the current path, True = emitted
        state = {}
        # Each frame is (package, is_root, children_already_queued)
        stack = [(pkg, True, False)]

        while stack:
            node, is_root, expanded = stack.pop()

            if expanded:
                if not state.get(node):
                    state[node] = True
                    order.append(node)
                continue

            if state.get(node) is True:
                continue
            if state.get(node) is False:
                # Already on the path above us: a dependency cycle. BLFS
                # documents these as "install X, then rebuild it later"; we
                # break the edge and keep the first position we chose.
                continue

            state[node] = False
            stack.append((node, is_root, True))
            for dep in reversed(self._dep_edges(node, is_root, rec, opt)):
                if state.get(dep) is not True:
                    stack.append((dep, False, False))

        return order

    def _dep_edges(self, pkg, is_root, rec, opt):
        """Returns the dependencies of pkg that should be pulled into the build.

        Required dependencies always apply. Recommended and optional ones are
        only expanded for the package the user actually asked for -- pulling
        every optional dependency transitively would drag in most of the book.

        Args:
            pkg (str): The package whose edges are being resolved.
            is_root (bool): True if this is the package the user requested.
            rec (bool): Whether to include recommended dependencies.
            opt (bool): Whether to include optional dependencies.

        Returns:
            list: The dependency names to expand.

        """
        if pkg not in self.database:
            return []

        deps = self.database[pkg][DbTypes.DEPS]
        edges = list(deps[DbTypes.REQUIRED])
        if is_root and (rec or opt):
            edges.extend(deps[DbTypes.RECOMMENDED])
        if is_root and opt:
            edges.extend(deps[DbTypes.OPTIONAL])
        return edges
