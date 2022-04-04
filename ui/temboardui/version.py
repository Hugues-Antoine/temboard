import sys
from platform import python_version


__version__ = "8.0.dev0"


# This output is parsed by tests/conftest.py::pytest_report_header.
VERSION_FMT = """\
temBoard %(temboard)s
System %(distname)s %(distversion)s
Python %(python)s (%(pythonbin)s)
Tornado %(tornado)s
libpq %(libpq)s
psycopg2 %(psycopg2)s
SQLAlchemy %(sqlalchemy)s
"""


def format_version():
    return VERSION_FMT % inspect_versions()


def inspect_versions():
    from .toolkit.versions import (
        format_pq_version,
        read_distinfo,
        read_libpq_version,
    )
    from psycopg2 import __version__ as psycopg2_version
    from tornado import version as tornado_version
    from sqlalchemy import __version__ as sqlalchemy_version

    distinfos = read_distinfo()

    return dict(
        distname=distinfos['NAME'],
        distversion=distinfos['VERSION'],
        libpq=format_pq_version(read_libpq_version()),
        psycopg2=psycopg2_version,
        python=python_version(),
        pythonbin=sys.executable,
        sqlalchemy=sqlalchemy_version,
        temboard=__version__,
        tornado=tornado_version,
    )
