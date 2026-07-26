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


generate_readme_rst()

long_description = read_file(
    'README.md',
    'Cannot read README.md'
)
requirements = read_file('requirements.txt')
dev_requirements = read_file('requirements-dev.txt')

trove_classifiers = [
    'Development Status :: 4 - Beta',
    'Environment :: Console',
    'Intended Audience :: End Users/Desktop',
    'License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)',
    'Operating System :: OS Independent',
    'Programming Language :: Python :: 3.10',
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

    # requests >= 2.33.0 (CVE-2026-25645) requires Python 3.10+. BLFS 11.3
    # ships Python 3.11.2, so this costs the target audience nothing.
    python_requires='>=3.10',
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
    entry_points=dict(
        console_scripts=[
            'blfs-pm=blfs_manager.blfspm:main'
        ]
    ),

    platforms=['Linux'],
)
