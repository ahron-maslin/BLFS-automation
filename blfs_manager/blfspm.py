import argparse
import signal
import logging

from .utils import load_db, load_installed_log, print_commands, print_deps
from .commands import Commands
from blfs_manager import __VERSION__, __PKGNAME__


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.info(f'{__PKGNAME__} {__VERSION__}')

    parser = argparse.ArgumentParser(
        description='A simple package that lists, downloads, and installs any valid BLFS package along with any dependencies.\n', 
        prog='blfs-pm')
    parser.add_argument('-a', '--all', help='Downloads ALL BLFS packages - uses a lot of time and space.\n', action='store_true')
    parser.add_argument(
        '-b', '--build', help='Install a given package on the LFS system with all of it\'s dependencies.\n', metavar='PACKAGE', default=False)
    parser.add_argument('-c', '--commands',
                        help='List installation (without installing) commands for a given package.\n', metavar='PACKAGE', default=False)
    parser.add_argument('-d', '--download',
                        help='Downloads a given BLFS package with all of its dependencies.\n', metavar='PACKAGE')
    parser.add_argument(
        '-f', '--force', help='Force package installation even though it is already installed\n', action='store_true')
    parser.add_argument(
        '-l', '--list', help='Lists all of the dependencies for a given BLFS package in order of installation.\n', metavar='PACKAGE', default=False)
    parser.add_argument('-o', '--optional',
                        help='List/download optional packages.\n', action='store_true')
    parser.add_argument('-r', '--recommended',
                        help='List/download recommended packages.\n', action='store_true')
    parser.add_argument('-s', '--search', help='Search for a given package. (Case Sensitive)\n', metavar='PACKAGE')
    parser.add_argument('--systemd', help='Pass this flag if you built LFS with Systemd', action='store_true')
    parser.add_argument('-V', '--version', action='version',
                        version=f'{__PKGNAME__} {__VERSION__}')
    args = parser.parse_args()

    db = load_db(args.systemd)
    installed = load_installed_log()

    action = Commands(db, installed)
    signal.signal(signal.SIGINT, action.cleanup)

    try:
        if args.download:
            action.download_deps(action.list_deps(
                args.download, args.recommended, args.optional))
        elif args.list:
            print_deps(action.list_deps(args.list, args.recommended, args.optional))
        elif args.commands:
            print_commands(action.list_commands(args.commands), args.commands)
        elif args.all:
            action.download_deps(list(db))
        elif args.build:
            action.build_pkg(args.build, args.force)
        elif args.search:
            action.search(args.search)
        else:
            parser.print_help()
    finally:
        # Persist progress even when a build aborts partway through.
        action.write_installed_log()
