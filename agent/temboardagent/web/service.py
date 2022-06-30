import logging
import ssl
from socket import error as SocketError
from wsgiref.simple_server import make_server

from bottle import debug, default_app

from ..errors import UserError
from ..toolkit.services import Service


logger = logging.getLogger(__name__)


class HTTPDService(Service):
    def setup(self):
        try:
            bottle = default_app()
            debug(self.app.debug)

            self.server = make_server(
                self.app.config.temboard.address,
                self.app.config.temboard.port,
                app=bottle,
            )
        except SocketError as e:
            raise UserError("Failed to start HTTPS server: {}.".format(e))
        try:
            logger.debug(
                "Using SSL key %s.", self.app.config.temboard.ssl_key_file)
            logger.debug(
                "Using SSL certificate %s.",
                self.app.config.temboard.ssl_cert_file)
            self.server.socket = ssl.wrap_socket(
                self.server.socket,
                keyfile=self.app.config.temboard.ssl_key_file,
                certfile=self.app.config.temboard.ssl_cert_file,
                server_side=True,
            )
        except Exception as e:
            raise UserError("Failed to setup SSL: {}.".format(e))
        self.server.timeout = 1

    def serve1(self):
        self.server.handle_request()
