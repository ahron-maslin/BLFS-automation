from multiprocessing.pool import ThreadPool
import os
import re
import json
import logging
import requests
from urllib3.util import Retry
from bs4 import BeautifulSoup as bs4
from requests.adapters import HTTPAdapter

from .define import DEFAULT_BASE_URL, HEADERS, DbTypes
from . import paths

database = {}

class DbEntry:
    """
    Represents a package's entry in the database.

    Attributes:
        name (str): The package name.
        url (list): A list of package URLs.
        deps (dict): A dictionary with three keys, "REQUIRED", "RECOMMENDED", and "OPTIONAL". The value for each key is a list of the package dependencies.
        commands (list): A list of commands to run the package.
        hashes (list): A list of package MD5 hashes.
        kconf (list): A list of kernel configuration options.
        pkg_type (str): The type of package, either "BLFS" or "external".
    """
    def __init__(self, name, url, deps, commands, hashes, kconf, pkg_type):
        self.name = name
        self.url = url
        self.deps = deps
        self.commands = commands
        self.hashes = hashes
        self.kconf = kconf
        self.pkg_type = pkg_type


def url_get(url, headers=None, timeout=30):
    """
    Sends an HTTP GET request to the specified URL.

    Args:
        url (str): The URL to send the request to.
        headers (dict): A dictionary of headers to include in the request.
        timeout (int): The maximum amount of time to wait for a response.

    Returns:
        requests.Response: A response object containing the HTTP response.
    """
    session = requests.Session()

    retries = Retry(total=5,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504])

    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))

    with session as s:
        response = s.get(url.rstrip(), headers=headers or HEADERS, timeout=timeout)
        response.raise_for_status()

    return response


def strip_text(string):
    """
    Strips whitespace from a string.

    Args:
        string (str): The string to strip.

    Returns:
        str: The stripped string.
    """
    return re.sub(r'\n\s+', ' ', string)


def filter_ftp(url_list):
    """
    Filters out duplicate FTP URLs from a list.

    Args:
        url_list (list): A list of URLs to filter.

    Returns:
        list: A new list with duplicate FTP URLs removed.
    """
    newlist = []
    for i, url in enumerate(url_list):
        if 'texlive' in url:  # the texlive package only contains ftp urls
            return url_list
        if (i % 2) == 0 or not url.startswith('ftp://'):
            newlist.append(url)
    return newlist


def collect_package_info(package, element_class, element):
    """
    Collects package information from a BeautifulSoup object.

    Args:
        package (bs4.element.Tag): The BeautifulSoup object to collect information from.
        element_class (str): The class name of the package element.
        element (str): The name of the package element.

    Returns:
        None
    """
    pkg_name = strip_text(package.find(
        element, class_=element_class).text).strip()  # string of name

    pkg_deps = {DbTypes.REQUIRED: [], DbTypes.RECOMMENDED: [], DbTypes.OPTIONAL: []}
    for c in pkg_deps:
        for d in package.find_all('p', class_=c):
            for e in d.find_all('a', title=True, class_='xref'):  # grab blfs deps
                pkg_deps[c].append(strip_text(e['title']))

            for e in d.find_all('a', class_='ulink'):  # grab external deps
                pkg_deps[c].append(strip_text(e.text))
                database[strip_text(e.text)] = DbEntry(
                                                strip_text(e.text), [e['href']],
                                                {
                                                    DbTypes.REQUIRED: [],
                                                    DbTypes.RECOMMENDED: [],
                                                    DbTypes.OPTIONAL: []
                                                }, 
                                                [],[None],[],'external').__dict__

    pkg_commands = list(
        map(lambda d: d.text, package.find_all('kbd', class_='command')))

    pkg_kernel_conf = list(map(lambda a: a.text, package.select(
        'div.kernel pre.screen code.literal')))

    pkg_urls = []
    pkg_hashes = []
    u = list(map(lambda x: x['href'], package.select(
        'div.itemizedlist a.ulink')))
    if package.find('div', class_='itemizedlist'):  # if package has urls add to array
        for d in package.find_all('div', class_='itemizedlist'):
            for e in d.find_all('a', class_='ulink'):
                pkg_urls.append(e['href'])
            for f in d.find_all('p'):
                if 'Download MD5 sum:' in f.getText():
                    pkg_hashes.extend(f.getText().split()[-1:])

    logging.info("Downloading info for {0}".format(pkg_name))
    database[pkg_name] = DbEntry(pkg_name, 
                                 filter_ftp(pkg_urls),
                                 pkg_deps,
                                 pkg_commands,
                                 pkg_hashes,
                                 pkg_kernel_conf,
                                 'BLFS').__dict__

def bootstrap(lfs_flavor=DEFAULT_BASE_URL):
    """
    Downloads package information for the specified LFS flavor.

    Args:
        lfs_flavor (str): The LFS flavor to download information for.

    Returns:
        None
    """
    pkg_count = 0
    base_url = f'{lfs_flavor}longindex.html'

    logging.info("Collecting base URLs....")
    res = url_get(base_url)
    soup = bs4(res.text, 'html.parser')

    #version = soup.h4.text.split()[-1]
    element = soup.find('a', attrs={"id": "package-index"}
                   ).parent.next_sibling.next_sibling
    
    # for every url... check if has href... if not add to array
    pkg_urls = list(map(lambda v: lfs_flavor +
                 v['href'] if not '#' in v['href'] else None, element.find_all('a', href=True)))
    pkg_urls = list(filter(None, pkg_urls))

    threads = ThreadPool(10).imap_unordered(url_get, pkg_urls)

    for thread in threads:
        pkg_count += 1
        soup = bs4(thread.text, 'html.parser')  # get webpage contents

        if len(soup.find_all('div', class_='sect2')) > 1:  
            # if soup is module instead of std package
            for module in soup.find_all('div', class_='sect2'):
                if module.find_all('div', class_='package'):  # limit to modules only
                    # call function on module
                    collect_package_info(module, "sect2", "h2")
        else:
            collect_package_info(soup, "sect1", "h1")  # call function on std package

    if pkg_count == len(pkg_urls):
        logging.info('Database successfully downloaded!')
    else:
        logging.error('Not all packages have been downloaded...')
        logging.error(f'Number of urls: {len(pkg_urls)}')
        logging.error(f'Number of downloaded packages: {pkg_count}'.format(pkg_count))
        exit(1)

    # Write via a temporary file so an interrupted scrape cannot leave behind a
    # truncated database that every later run would happily load.
    db_path = paths.db_path()
    paths.ensure_writable(db_path.parent)
    tmp_path = f'{db_path}.tmp'
    with open(tmp_path, 'w') as file:
        json.dump(database, file)
    os.replace(tmp_path, db_path)
