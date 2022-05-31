import logging
import os

from ..daemon import daemonize
from ..toolkit.app import SubCommand
from ..toolkit.services import ServicesManager
from .app import app


logger = logging.getLogger(__name__)


@app.command
class Serve(SubCommand):
    """Combined web and worker services."""

    is_service = True

    def main(self, args):
        # Purge all legacy data queues
        home = self.app.config.temboard.home
        if os.path.exists(home):
            [os.remove(os.path.join(home, f))
             for f in os.listdir(home) if f.endswith('.q')]

        if self.app.config.temboard.daemonize:
            daemonize(self.app.config.temboard.pidfile)

        self.app.httpd.services = services = ServicesManager()
        services.add(self.app.worker_pool)
        services.add(self.app.scheduler)

        logger.info("Starting main process.")

        with services:
            self.app.httpd.run()

        return 0
