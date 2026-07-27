import readline
import hashlib
import os
import re
import subprocess
import json
import logging
from termcolor import colored


from .define import SYSTEMD_BASE_URL, DEFAULT_BASE_URL
from .bootstrapper import bootstrap
from . import paths


def load_db(systemd=False):
    """
    Load the database file.

    Parameters:
        systemd (bool): A boolean indicating whether the database is for Systemd or not.

    Returns:
        dict: A dictionary containing the loaded JSON data from the database file.
    """
    # Seeds from the read-only copy shipped in the wheel when present, so a
    # fresh install does not re-scrape ~1600 book pages on first run.
    db_path = paths.ensure_db()
    if not os.path.exists(db_path):
        logging.info('Downloading database, (this is a one time process)')
        bootstrap(SYSTEMD_BASE_URL if systemd else DEFAULT_BASE_URL)
    with open(db_path, 'r') as database:
        return json.load(database)
        
def load_installed_log():
    """
    Load the list of installed packages.

    Returns:
        list: A list containing the names of installed packages.
    """
    try:
        with open(paths.installed_log_path(), 'r') as i:
            installed = [line.rstrip() for line in i]
    except FileNotFoundError:
        installed = []
    return installed


def rlinput(prompt, prefill=''):
    """
    Readline input with pre-filled text.

    Parameters:
        prompt (str): The input prompt.
        prefill (str): The text to pre-fill the input field.

    Returns:
        str: The user input string.
    """
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def check_dir():
    """
    Check if the download directory exists and create it if it doesn't.

    Returns:
        None
    """
    os.chdir(paths.ensure_sources_dir())
    return


def change_dir(cmd):
    """
    Get the directory path from a 'cd' command.

    Parameters:
        cmd (str): The command string.

    Returns:
        str: The directory path.
    """
    for i, w in enumerate(cmd):
        if w == 'cd':
            return cmd[i+1]
    return ''


def md5_check(file, hash):
    """
    Check if a downloaded file's MD5 hash matches the expected hash.

    Parameters:
        file (str): The path to the downloaded file.
        hash (str): The expected MD5 hash.

    Returns:
        None
    Raises:
        OSError: If the downloaded file does not match the expected hash.
    """
    if not hash or not re.fullmatch(r'[0-9a-fA-F]{32}', str(hash).strip()):
        logging.warning(colored(
            f'No usable MD5 sum recorded for {file} - integrity NOT verified.', 'yellow'))
        return

    digest = hashlib.md5()
    with open(file, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    file_hash = digest.hexdigest()

    if str(hash).strip().lower() != file_hash:
        os.remove(file)
        raise OSError(
            f'Downloaded file does not match the MD5 hash!\n'
            f'  file:     {file}\n'
            f'  expected: {hash}\n'
            f'  actual:   {file_hash}\n')


def run_cmd(command):
    """
    Run a command in the shell.

    Parameters:
        command (str): The command string.

    Returns:
        int: The exit status of the command. 0 means success.
    """
    logging.info(colored(f'Running {command}', 'green'))
    returncode = subprocess.call(['/bin/sh', '-c', command])

    if returncode != 0:
        logging.error(colored(
            f'Command failed with exit status {returncode}: {command}', 'red'))
        return returncode

    # `cd` inside `sh -c` dies with the subshell, so replay it in our own process.
    target = change_dir(re.sub(r'\s+', ' ', command).split())
    if target:
        try:
            os.chdir(target)
        except OSError as exc:
            logging.error(colored(f'Could not change directory: {exc}', 'red'))
            return 1
    return 0


def is_within_directory(directory, target):
    """
    Check if a target path is within a specified directory.

    Parameters:
        directory (str): The directory path.
        target (str): The target path.

    Returns:
        bool: True if the target is within the directory, False otherwise.
    """
    # os.path.commonprefix() is a *string* operation: it happily reports that
    # "/src/foo-evil" lives inside "/src/foo". Compare resolved path components.
    abs_directory = os.path.realpath(directory)
    abs_target = os.path.realpath(target)
    return os.path.commonpath([abs_directory, abs_target]) == abs_directory


def safe_extract(tar, path=".", members=None, *, numeric_owner=False): 
    """
    Safely extract files from a tar archive.

    Parameters:
        tar (tarfile.TarFile): The TarFile object.
        path (str): The destination directory path.
        members (list): A list of TarInfo objects to extract.
        numeric_owner (bool): A flag indicating whether to use numeric owner values or not.

    Returns:
        None
    Raises:
        Exception: If there is an attempted path traversal in the TarFile.
    """                   
    for member in tar.getmembers():
        member_path = os.path.join(path, member.name)
        if not is_within_directory(path, member_path):
            raise Exception(
                f'Attempted Path Traversal in Tar File: {member.name}')
        # A symlink/hardlink whose target escapes the extraction root lets a
        # later member be written outside it.
        if member.issym() or member.islnk():
            link_path = os.path.join(os.path.dirname(member_path), member.linkname)
            if not is_within_directory(path, link_path):
                raise Exception(
                    f'Attempted Link Traversal in Tar File: '
                    f'{member.name} -> {member.linkname}')
    tar.extractall(path, members, numeric_owner=numeric_owner)

def print_deps(pkg_list):
    """
    Print a list of packages to install in order.

    Parameters:
        pkg_list (list): A list of package names.

    Returns:
        None
    """
    logging.info(colored("Install packages in this order:\n", "green"))
    for pkg in pkg_list:
        logging.info(colored(pkg, attrs=['bold']))

def print_commands(cmd_list, pkg):
    """
    Print a list of commands for a package.

    Parameters:
        cmd_list (list): A list of command strings.
        pkg (str): The package name.

    Returns:
        None
    """
    logging.info(colored(f'Listing commands for {pkg}\n', "green"))
    for i, command in enumerate(cmd_list):
        logging.info(f'Command {i+1}:')
        logging.info(colored(command, attrs=['bold']))
        print()
