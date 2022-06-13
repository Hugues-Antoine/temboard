import logging
import os
import datetime
import getpass
import sys
from argparse import _VersionAction
from platform import python_version
from socket import getfqdn
from textwrap import dedent

from ..toolkit.configuration import OptionSpec
from ..httpd import HTTPDService
from ..postgres import Postgres
from ..toolkit import taskmanager, validators as v
from ..toolkit.app import BaseApplication, define_core_arguments
from ..toolkit.proctitle import ProcTitleManager
from ..toolkit.tasklist.sqlite3_engine import TaskListSQLite3Engine
from ..toolkit.versions import (
    format_pq_version,
    read_distinfo,
    read_libpq_version,
)
from ..notification import NotificationMgmt
from ..routing import Router
from ..version import __version__


logger = logging.getLogger('temboardagent.scripts.agent')


class TemboardAgentApplication(BaseApplication):
    PROGRAM = "temboard-agent"
    VERSION = __version__

    DEFAULT_CONFIGFILES = [
        '/etc/temboard-agent/temboard-agent.conf',
        'temboard-agent.conf',
    ]
    DEFAULT_PLUGINS = [
        "activity",
        "administration",
        "dashboard",
        "maintenance",
        "monitoring",
        "pgconf",
        "statements",
    ]

    def main(self, argv, environ):
        parser = self.create_parser(
            description="temBoard agent {}.".format(__version__),
        )
        self.define_arguments(parser)
        args = parser.parse_args(argv)

        command_name = getattr(args, 'command_fullname', 'serve')
        command = self.commands[command_name]

        setproctitle = ProcTitleManager(prefix='temboard-agent: ')
        setproctitle.setup()

        self.router = Router()

        task_queue = taskmanager.Queue()
        event_queue = taskmanager.Queue()

        self.worker_pool = taskmanager.WorkerPoolService(
            app=self, setproctitle=setproctitle, name='worker pool',
            task_queue=task_queue, event_queue=event_queue)
        self.services.append(self.worker_pool)

        self.scheduler = taskmanager.SchedulerService(
            app=self, setproctitle=setproctitle, name='scheduler',
            task_queue=task_queue, event_queue=event_queue)
        self.services.append(self.scheduler)

        self.httpd = HTTPDService(
            self, setproctitle=setproctitle, name='main process',
        )

        self.bootstrap(args=args, environ=environ, service=command.is_service)
        self.log_versions()
        config = self.config

        # TaskList engine setup must be done before we load the plugins
        self.scheduler.task_list_engine = TaskListSQLite3Engine(
            os.path.join(config.temboard['home'], 'agent_tasks.db')
        )

        self.apply_config()

        if config.postgresql.instance:
            setproctitle.prefix += config.postgresql.instance + ': '

        self.start_datetime = datetime.datetime.now()
        self.reload_datetime = None
        self.pid = os.getpid()
        self.user = getpass.getuser()

        self.bootstrap_plugins()

        # Boostraping action logs table
        NotificationMgmt.bootstrap(config)

        return command.main(args)

    def apply_config(self):
        self.postgres = Postgres(app=self, **self.config.postgresql)
        return super().apply_config()

    def bootstrap_plugins(self):
        for plugin_name, plugin in self.plugins.items():
            if hasattr(plugin, 'bootstrap'):
                logger.debug("Boostraping plugin %s", plugin_name)
                plugin.bootstrap()

    def check_compatibility(self, pg_version):
        # check for compatibility with plugins
        for name, plugin in self.plugins.items():
            if pg_version < plugin.PG_MIN_VERSION[0]:
                logger.error(
                    "%s plugin is incompatible with Postgres below %s",
                    name, plugin.PG_MIN_VERSION[1],
                )

    def define_arguments(self, parser):
        define_core_arguments(parser)
        parser.add_argument(
            '-V', '--version',
            action=VersionAction,
            help='show version and exit',
        )
        parser.add_argument(
            '-d', '--daemon',
            action='store_true', dest='temboard_daemonize',
            help="Run in background.",
        )
        parser.add_argument(
            '-p', '--pid-file',
            action='store', dest='temboard_pidfile',
            help="PID file.",
        )
        super(TemboardAgentApplication, self).define_arguments(parser)

    def init_specs(self, app_specs):
        specs = super().init_specs(app_specs)

        def add_specs(*new_specs):
            for spec in new_specs:
                specs[str(spec)] = spec

        # These are *core* because they are needed to load plugins.
        s = 'postgresql'
        add_specs(
            OptionSpec(
                s, 'host', default='/var/run/postgresql', validator=v.dir_),
            OptionSpec(s, 'instance', default='main'),
            OptionSpec(s, 'port', default=5432, validator=v.port),
            OptionSpec(s, 'user', default='postgres'),
            OptionSpec(s, 'password'),
            OptionSpec(s, 'dbname', default='postgres'),
        )

        return specs

    def core_specs(self):
        for spec in super().core_specs():
            yield spec

        for name, spec in self.config_specs.items():
            if name.startswith('postgresql_'):
                yield spec

    def log_versions(self):
        versions = VersionAction.inspect_versions()
        logger.debug(
            "Running on %s %s.",
            versions['distname'], versions['distversion'])
        logger.debug(
            "Using Python %s (%s).",
            versions['python'], versions['pythonbin'])
        logger.debug(
            "Using libpq %s, Psycopg2 %s.",
            versions['libpq'], versions['psycopg2'],
        )

    def reload(self):
        super().reload()
        self.reload_datetime = datetime.datetime.now()


class VersionAction(_VersionAction):
    fmt = dedent("""\
    temBoard agent %(temboard)s (%(temboardbin)s)
    System %(distname)s %(distversion)s
    Python %(python)s (%(pythonbin)s)
    libpq %(libpq)s
    psycopg2 %(psycopg2)s
    """)

    def __call__(self, parser, *_):
        print((self.fmt % self.inspect_versions()).strip())
        parser.exit()

    @classmethod
    def inspect_versions(cls):
        from psycopg2 import __version__ as psycopg2_version

        distinfos = read_distinfo()

        return dict(
            temboard=__version__,
            temboardbin=sys.argv[0],
            psycopg2=psycopg2_version,
            python=python_version(),
            pythonbin=sys.executable,
            distname=distinfos['NAME'],
            distversion=distinfos['VERSION'],
            libpq=format_pq_version(read_libpq_version()),
        )


def list_options_specs():
    # Generate each option specs.
    section = 'temboard'
    yield OptionSpec(section, 'ui_url', validator=v.url)
    yield OptionSpec(section, 'daemonize', default=False)
    yield OptionSpec(section, 'pidfile', default='/run/temboard-agent.pid')
    yield OptionSpec(
        section, 'address', default='0.0.0.0', validator=v.address)
    yield OptionSpec(section, 'port', validator=v.port, default=2345)
    yield OptionSpec(
        section, 'ssl_cert_file',
        default=OptionSpec.REQUIRED, validator=v.file_)
    yield OptionSpec(
        section, 'ssl_key_file',
        default=OptionSpec.REQUIRED, validator=v.file_)
    yield OptionSpec(section, 'ssl_ca_cert_file', validator=v.file_)
    yield OptionSpec(section, 'key')
    yield OptionSpec(
        section, 'users', default='users', validator=v.file_,
    )
    yield OptionSpec(section, 'hostname', default=getfqdn(), validator=v.fqdn)
    home = os.environ.get('HOME', '/var/lib/temboard-agent')
    yield OptionSpec(section, 'home', default=home, validator=v.writeabledir)


app = TemboardAgentApplication(specs=list_options_specs())
