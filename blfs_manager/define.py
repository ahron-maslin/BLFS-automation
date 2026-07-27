from . import paths

DEFAULT_BASE_URL = 'https://www.linuxfromscratch.org/blfs/view/stable/'
SYSTEMD_BASE_URL = 'https://www.linuxfromscratch.org/blfs/view/stable-systemd/' 
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT x.y; rv:10.0) Gecko/20100101 Firefox/10.0'
}

EXCEPTIONS = ['Xorg Libraries', 'Xorg Applications',
              'Xorg Fonts', 'Xorg Legacy']

EXTENSIONS = ['.bz2', '.tar.xz', '.zip', '.tar.gz', '.patch', '.tgz']

_LEGACY_PATHS = {
    'ROOT_PATH': lambda: paths.PACKAGE_DIR.parent,
    'DOWNLOAD_PATH': paths.sources_dir,
    'INSTALLED_PATH': paths.installed_log_path,
    'DB_PATH': paths.db_path,
    'DB_FILENAME': lambda: paths.DEFAULT_DB_FILENAME,
}


def __getattr__(name):
    """Resolves the former path constants through :mod:`blfs_manager.paths`.

    These used to be module-level constants computed at import time, which
    pinned every location to ``site-packages`` and made them impossible to
    override in tests or by environment. They are resolved on access instead.

    Args:
        name (str): The attribute being looked up.

    Returns:
        pathlib.Path or str: The resolved location.

    Raises:
        AttributeError: If name is not a known legacy path constant.

    """
    if name in _LEGACY_PATHS:
        return _LEGACY_PATHS[name]()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

# database value types
class DbTypes:
    NAME        = 'name'
    URL         = 'url'
    DEPS        = 'deps'
    REQUIRED    = 'required' 
    RECOMMENDED = 'recommended'
    OPTIONAL    = 'optional'
    COMMANDS    = 'commands'
    HASHES      = 'hashes'
    KCONF       = 'kconf'
    TYPE        = 'pkg_type'
