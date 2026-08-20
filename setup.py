# -*- coding: utf-8 -*-
#
# you can install this to a local test virtualenv like so:
#   virtualenv venv
#   ./venv/bin/pip install --editable .
#   ./venv/bin/pip install --editable .[dev]  # with dev requirements, too

from __future__ import print_function

import os.path
import subprocess
import sys

from setuptools import setup

from blfs_manager import __VERSION__


def generate_readme_rst():
    """
    Generate README.rst from README.md via pandoc.

    In case of errors, we show a message having the error that we got and
    exit the program.
    """

    pandoc_cmd = [
        'pandoc',
        '--from=markdown',
        '--to=rst',
        '--output=README.rst',
        'README.md'
    ]

    if os.path.exists('README.rst'):
        return
    try:
        subprocess.call(pandoc_cmd)
    except (IOError, OSError) as e:
        print('Could not run "pandoc". Error: %s' % e, file=sys.stderr)
        print('Generating only a stub instead of the real documentation.')


def read_file(filename, alt=None):
    """
    Read the contents of filename or give an alternative result instead.
    """
    lines = None

    try:
        with open(filename, encoding='utf-8') as f:
            lines = f.read()
    except IOError:
        lines = [] if alt is None else alt
    return lines


def read_requirements(filename):
    """
    Read a requirements file into a list of requirement strings, as
    install_requires/extras_require expect -- not the raw file contents.
    """
    return [
        line.strip()
        for line in read_file(filename, alt='').splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]


generate_readme_rst()

long_description = read_file(
    'README.md',
    'Cannot read README.md'
)
requirements = read_requirements('requirements.txt')
dev_requirements = read_requirements('requirements-dev.txt')

trove_classifiers = [
    'Development Status :: 4 - Beta',
    'Environment :: Console',
    'Intended Audience :: End Users/Desktop',
    'License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)',
    'Operating System :: OS Independent',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Programming Language :: Python :: Implementation :: CPython',
    'Programming Language :: Python :: Implementation :: PyPy',
    'Programming Language :: Python',
    'Topic :: Education',
]

setup(
    name='blfs-pm',
    version=__VERSION__,
    maintainer='Aharon Maslin',
    maintainer_email='aronmas613@gmail.com',

    license='LGPL',
    url='https://github.com/ahron-maslin/BLFS-automation',

    # BLFS 11.3 ships Python 3.11.2; no runtime dependency requires newer
    # (highest floor among pinned deps is Python 3.10).
    python_requires='>=3.11.2',
    install_requires=requirements,
    extras_require=dict(
        dev=dev_requirements
    ),

    description='Package manager for Beyond Linux from Scratch (BLFS) system',
    long_description=long_description,
    long_description_content_type='text/markdown',
    keywords=['BLFS', 'LFS', 'Package Manager', 'automation'],
    classifiers=trove_classifiers,

    packages=["blfs_manager"],
    include_package_data=True,
    package_data={'blfs_manager': ['lfs-deps-*']},
    entry_points=dict(
        console_scripts=[
            'blfs-pm=blfs_manager.blfspm:main'
        ]
    ),

    platforms=['Linux'],
)
