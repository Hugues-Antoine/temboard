# coding: utf-8
from __future__ import absolute_import

from builtins import str
from builtins import object
import functools
import json
import logging
import os
try:
    from StringIO import StringIO
except Exception:
    from io import StringIO
from csv import writer as CSVWriter

from tornado import web as tornadoweb
from tornado.concurrent import run_on_executor
from tornado.escape import json_decode, json_encode, url_escape
from tornado.gen import coroutine
from tornado.web import (
    Application as TornadoApplication,
    HTTPError,
    RequestHandler,
)
from tornado.template import Loader as TemplateLoader

from ..application import (
    get_instance,
    get_role_by_cookie,
    get_roles_by_instance,
)
from ..errors import TemboardUIError
from ..model import Session as DBSession
from ..agentclient import TemboardAgentClient
from ..toolkit.pycompat import PY2
from ..toolkit.perf import PerfCounters
from ..toolkit.utils import JSONEncoder, utcnow


logger = logging.getLogger(__name__)


def admin_required(func):
    # Similar to flask_security.roles_required, but limited to admin role.
    func.__admin_required = True
    return func


def anonymous_allowed(func):
    # Reverse of flask_security.login_required.
    #
    # In temboard, very few pages are anonymous. Thus we have implicit
    # login_required. This behaviour can be disabled by using
    # @anonymous_allowed.
    func.__anonymous_allowed = True
    return func


def serialize_querystring(query):
    return "&".join([
        "%s=%s" % (url_escape(name), url_escape(value))
        for name, value in sorted(query.items())
    ])


class Response(object):
    def __init__(
            self, status_code=200, headers=None, secure_cookies=None,
            body=u''):
        self.status_code = status_code
        self.headers = headers or {}
        self.secure_cookies = secure_cookies or {}
        self.body = body


class Redirect(Response, Exception):
    def __init__(self, location, permanent=False, secure_cookies=None):
        super(Redirect, self).__init__(
            status_code=301 if permanent else 302,
            headers={'Location': location},
            body=u'Redirected to %s' % location,
            secure_cookies=secure_cookies,
        )


class TemplateRenderer(object):
    # Flask-like HTML render function, without thread local.

    GLOBAL_NAMESPACE = {}

    def __init__(self, path):
        self.loader = TemplateLoader(path)

    def __call__(self, template, **data):
        data = dict(self.GLOBAL_NAMESPACE, **data)
        return Response(
            body=self.loader.load(template).generate(**data),
            headers={'Content-Type': 'text/html; charset=UTF-8'},
        )


template_path = os.path.realpath(__file__ + '/../../templates')
render_template = TemplateRenderer(template_path)


def csvify(data, status_code=200):
    if isinstance(data, list):
        fo = StringIO()
        writer = CSVWriter(fo)
        for row in data:
            writer.writerow(row)
        data = fo.getvalue()
    elif not isinstance(data, (str, bytes)):
        raise ValueError("Malformed CSV data")
    return Response(
        status_code=status_code,
        headers={'Content-Type': 'text/csv'},
        body=data,
    )


def jsonify(data, status_code=200):
    return Response(
        status_code=status_code,
        headers={'Content-Type': 'application/json; charset=UTF-8'},
        body=json_encode(data),
    )


class CallableHandler(RequestHandler):
    # Adapt flask-like callable in Tornado Handler API.

    @property
    def executor(self):
        # To enable @run_on_executor methods, we must have executor property.
        return self.application.executor

    def compute_etag(self):
        # We don't want automatic caching for API.
        return None

    def initialize(self, callable_, blueprint=None, methods=None, logger=None):
        self.callable_ = callable_
        self.logger = logger or logging.getLogger(__name__)
        self.request.blueprint = blueprint
        self.request.config = self.application.config
        # run_on_executor searches for `executor` attribute of first argument.
        # Thus, we bind executor to request object.
        self.request.executor = self.executor
        self.request.handler = self
        self.SUPPORTED_METHODS = methods or ['GET']

    def get_current_user(self):
        cookie = self.get_secure_cookie('temboard')
        if not cookie:
            return

        try:
            return get_role_by_cookie(self.db_session, cookie.decode("utf-8"))
        except Exception as e:
            self.logger.error("Failed to get role from cookie: %s ", e)

    @coroutine
    def get(self, *args, **kwargs):
        try:
            response = yield self.callable_(self.request, *args, **kwargs)
        except Redirect as r:
            response = r

        if response is None:
            response = u''
        if isinstance(response, (dict, str)):
            response = Response(body=response)
        self.write_response(response)

    # Let's use one handler for all supported methods.
    post = get
    delete = get

    def write_response(self, response):
        # Should be in a middleware.
        if response.status_code in (301, 302, 401):
            response.secure_cookies['referer_uri'] = self.request.uri

        self.set_status(response.status_code)
        for k, v in list(response.headers.items()):
            if not isinstance(v, list):
                v = [v]
            self.clear_header(k)
            for v1 in v:
                self.add_header(k, v1)

        for k, v in list(response.secure_cookies.items()):
            self.set_secure_cookie(k, v, expires_days=30)

        self.finish(response.body)


class Error404Handler(RequestHandler):
    def write_error(self, status_code, **kwargs):
        content_type = self.request.headers.get("Content-Type", '')
        if content_type.startswith("application/json"):
            self.write({'error': 'Not found.'})
        else:
            self.write('404: Not found.')


class DatabaseHelper(object):
    @classmethod
    def add_middleware(cls, func):
        @functools.wraps(func)
        def database_middleware(request, *args):
            request.db_session = request.handler.db_session = DBSession()
            try:
                response = func(request, *args)
            except Exception:
                # Expunge objects before rollback to implement
                # expire_on_rollback=False. This allow templates to reuse
                # request.instance object and joined object without triggering
                # lazy load.
                request.db_session.expunge_all()
                request.db_session.rollback()
                raise
            else:
                request.db_session.commit()
            finally:
                request.db_session.close()
                del request.db_session

            return response

        return database_middleware


class ErrorHelper(object):
    @classmethod
    def add_middleware(cls, func):
        @functools.wraps(func)
        def error_middleware(request, *args):
            try:
                return func(request, *args)
            except Redirect:
                raise
            except TemboardAgentClient.Error as e:
                code = e.response.status
                message = e.message
            except TemboardUIError as e:
                code = e.code
                message = e.message
            except HTTPError as e:
                code = e.status_code
                message = e.log_message
                if code == 404:
                    message += " Plugin may not be activated on agent side"
            except Exception as e:
                # Show traceback for developer, and HTML page for user.
                logger.exception("Unhandled Error:")
                code = 500
                message = str(e)

            logger.error("Request failed: %s %s.", code, message)
            return make_error(request, code, message)

        return error_middleware


class InstanceHelper(object):
    # This helper class implements all operations related to instance dedicated
    # request.

    INSTANCE_PARAMS = r'/(.+)/([0-9]{1,5})'
    PROXY_PREFIX = r'/proxy' + INSTANCE_PARAMS
    SERVER_PREFIX = r'/server' + INSTANCE_PARAMS

    @classmethod
    def add_middleware(cls, callable_):
        # Wraps an HTTP handler callable related to a Postgres instance

        @functools.wraps(callable_)
        def instance_middleware(request, address, port, *args):
            # Swallow adddress and port arguments.
            request.instance = cls(request)
            request.instance.fetch_instance(address, port)
            return callable_(request, *args)

        return instance_middleware

    def __init__(self, request):
        self.request = request
        self._xsession = False

    def __getattr__(self, name):
        return getattr(self.instance, name)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.instance.hostname)

    def __str__(self):
        return "%s:%s" % (self.instance.hostname, self.instance.pg_port)

    def check_active_plugin(self, name):
        '''
        Ensure that the plugin is active for given instance
        '''
        if name not in [p.plugin_name for p in self.instance.plugins]:
            raise HTTPError(408, "Plugin %s not activated." % name)

    def fetch_instance(self, address, port):
        self.instance = get_instance(self.request.db_session, address, port)
        if not self.instance:
            raise HTTPError(404)
        self.agent_id = '%s:%s' % (
            self.instance.agent_address,
            self.instance.agent_port,
        )

    @property
    def cookie_name(self):
        return 'temboard_%s_%s' % (
            self.instance.agent_address, self.instance.agent_port,
        )

    @property
    def xsession(self):
        if self._xsession is False:
            self._xsession = self.request.handler.get_secure_cookie(
                self.cookie_name)
        return self._xsession

    def format_url(self, path=''):
        return "/server/%s/%s%s" % (self.agent_address, self.agent_port, path)

    def redirect(self, path):
        raise Redirect(location=self.format_url(path))

    def client(self):
        return TemboardAgentClient.factory(
            self.request.config,
            self.instance.agent_address,
            self.instance.agent_port,
            self.instance.agent_key,
            self.request.current_user.role_name,
        )

    def request_agent(self, path, method='GET', query=None, body=None):
        client = self.client()

        pathinfo = path
        if query:
            pathinfo += "?" + serialize_querystring(query)

        headers = {}
        xsession = self.xsession
        if xsession:
            headers['X-Session'] = xsession

        try:
            response = client.request(
                method=method,
                path=pathinfo,
                headers=headers,
                body=body,
            )
            response.raise_for_status()
        except OSError as e:
            raise HTTPError(500, (
                "Failed to contact agent %s:%s: %s. Is it running?"
                % (self.instance.agent_address, self.instance.agent_port, e)))
        except ConnectionError as e:
            raise HTTPError(500, str(e))
        except TemboardAgentClient.Error as e:
            raise HTTPError(e.response.status, e.message)
        except Exception as e:
            logger.error("Proxied request failed: %s", e)
            raise HTTPError(500, "Unhandled error")
        else:
            return response.json()

    def get(self, *args, **kwargs):
        kwargs['method'] = 'GET'
        return self.request_agent(*args, **kwargs)

    def post(self, *args, **kwargs):
        kwargs['method'] = 'POST'
        return self.request_agent(*args, **kwargs)

    def get_profile(self):
        i = self.instance
        if i.agent_key:
            # Try without key to detect agent upgrade.
            client = self.client()
            try:
                logger.debug(
                    "Testing session handling of agent for %s.",
                    self.instance)
                headers = {'X-Session': '__bad_session__'}
                res = client.get('/profile', headers=headers)
                res.raise_for_status()
            except TemboardAgentClient.Error as e:
                if 406 == e.response.status:
                    logger.debug("Agent for %s still validates X-Session.", i)
                    logger.warning("You should upgrade agent for %s.", i)
                else:
                    raise
            else:
                logger.info(
                    "Detected agent upgrade for %s. Dropping legacy key.", i)
                self.instance.agent_key = None
                self.request.db_session.commit()

        if self.instance.agent_key:  # Agent 7.X
            try:
                return self.get("/profile")
            except HTTPError as e:
                if 401 == e.status_code:
                    logger.debug("Legacy agent login required for %s.", i)
                    self.redirect('/login')
                elif 406 == e.status_code and 'X-Session' in e.log_message:
                    logger.debug("Bad X-Session: %s Re-login.", e.log_message)
                    self.redirect('/login')
                else:
                    logger.error("XXX: %s", e)
                    raise
        else:
            # Agent 8+ does not have users anymore. Use UI user.
            return {"username": self.request.current_user.role_name}

    def get_username(self):
        try:
            agent_username = self.get_profile()['username']
        except Exception:
            agent_username = None
        return agent_username


def add_json_middleware(func):
    @functools.wraps(func)
    def json_middleware(request, *args):
        if 'POST' == request.method:
            if request.body:
                try:
                    request.json = json_decode(request.body)
                except Exception as e:
                    raise HTTPError(400, str(e))
            else:
                logger.debug("Empty body for POST request.")
                request.json = None
        else:
            # Set empty body.
            request.json = None

        return func(request, *args)
    return json_middleware


def add_user_instance_middleware(func):
    # Ensures user is allowed to access to the instance
    @functools.wraps(func)
    def user_instance_middleware(request, address, port, *args):
        user = request.current_user = request.handler.current_user

        if user is None:
            # Not logged in
            raise HTTPError(401, "Restricted area.")

        allowed_roles = get_roles_by_instance(request.handler.db_session,
                                              address,
                                              port)
        roles = [role.role_name for role in allowed_roles if role]
        if user.role_name not in roles:
            raise HTTPError(403, "Restricted area.")

        return func(request, address, port, *args)

    return user_instance_middleware


class UserHelper(object):
    @classmethod
    def add_middleware(cls, func):

        @functools.wraps(func)
        def user_middleware(request, *args):
            role = request.current_user = request.handler.current_user

            anonymous_allowed = getattr(func, '__anonymous_allowed', False)
            if not anonymous_allowed and role is None:
                logger.debug("Redirecting anonymous to /login.")
                raise Redirect('/login')

            admin_required = getattr(func, '__admin_required', False)
            if admin_required and not role.is_admin:
                logger.debug("Refusing access to non-admin user.")
                raise HTTPError(403)

            return func(request, *args)

        return user_middleware


# Ensure @functools.wraps preserves User middleware attributes.
functools.WRAPPER_UPDATES += (
    '__admin_required',
    '__anonymous_allowed',
)


class Blueprint(object):
    def __init__(self, plugin_name=None):
        self.plugin_name = plugin_name
        self.rules = []
        self.perf = PerfCounters.setup(service='web', plugin=plugin_name)

    def add_rules(self, rules):
        self.rules.extend(rules)

    def generic_proxy(self, url, methods=None):
        # Pass-through implementation for /proxy/address/port/…
        url = r'(%s)' % url

        @self.instance_proxy(url, methods)
        def generic_instance_proxy(request, path):
            if request.blueprint and request.blueprint.plugin_name:
                request.instance.check_active_plugin(
                    request.blueprint.plugin_name)
            body = request.instance.request_agent(
                path=url_escape(path, plus=False),
                method=request.method,
                body=request.json,
            )
            return jsonify(body)

    def instance_proxy(self, url, methods=None):
        # decorator for /proxy/address/port/… handlers.
        url = InstanceHelper.PROXY_PREFIX + url
        return self.route(url, methods=methods, with_instance=True, json=True)

    def instance_route(self, url, methods=None, **kwargs):
        # Helper to declare a route with instance URL prefix and middleware.
        return self.route(
            url=InstanceHelper.SERVER_PREFIX + url,
            methods=methods,
            with_instance=True,
            **kwargs
        )

    def route(self, url, methods=None, with_instance=False, json=None):
        # Implements flask-like route registration of a simple synchronous
        # callable.

        # Enable JSON middleware on /json/ handlers.
        if json is None:
            json = url.startswith('/json/')

        def decorator(func):
            logger_name = func.__module__ + '.' + func.__name__

            func = UserHelper.add_middleware(func)
            if with_instance:
                func = InstanceHelper.add_middleware(func)

                if url.startswith('/server/') or url.startswith('/proxy/'):
                    # Limit user/instance access control to /server/ and
                    # /proxy/.
                    # Admin area access control has already been performed
                    func = add_user_instance_middleware(func)

            if json:
                func = add_json_middleware(func)
            func = DatabaseHelper.add_middleware(func)
            func = ErrorHelper.add_middleware(func)

            @run_on_executor
            @functools.wraps(func)
            def sync_request_wrapper(request, *args):
                start = utcnow() if self.perf else None
                status = 500
                try:
                    response = func(request, *args)
                    if hasattr(response, 'status_code'):
                        status = response.status_code
                    else:  # JSON data
                        status = 200
                    return response
                except (Redirect, HTTPError) as e:
                    status = e.status_code
                    raise
                except Exception as e:
                    # Since async traceback is useless, spit here traceback and
                    # forge simple HTTPError(500), no HTML error.
                    logger.exception("Unhandled Error:")
                    raise HTTPError(500, str(e))
                finally:
                    if self.perf:
                        response_time = utcnow() - start
                        instance_helper = getattr(request, 'instance', None)
                        if (
                                with_instance and
                                hasattr(instance_helper, 'instance')):
                            agent = request.instance.agent_id
                        else:
                            agent = 'undefined'

                        logger.debug(
                            "method=%s url=%s status=%s handler=%s"
                            " response_time=%s plugin=%s agent=%s service=web",
                            request.method, request.path, status,
                            logger_name, response_time.total_seconds(),
                            self.plugin_name or 'main', agent,
                        )

            rules = [(
                url, CallableHandler, dict(
                    blueprint=self,
                    callable_=sync_request_wrapper,
                    methods=methods or ['GET'],
                    logger=logging.getLogger(logger_name),
                ),
            )]
            self.add_rules(rules)
            return func

        return decorator


class WebApplication(TornadoApplication, Blueprint):
    def __init__(self, *a, **kwargs):
        super(WebApplication, self).__init__(*a, **kwargs)
        Blueprint.__init__(self)

    def configure(self, **settings):
        # Runtime configuration of application.
        #
        # This way, we can initialize app at import time to register handlers.
        # Then configure it at run time once configuration is parsed.

        self.settings.update(settings)

        # This comes from Tornado's __init__
        if self.settings.get('debug'):
            logger.info("Enabling Tornado's autoreload.")
            self.settings.setdefault('autoreload', True)
            self.settings.setdefault('compiled_template_cache', False)
            self.settings.setdefault('static_hash_cache', False)
            self.settings.setdefault('serve_traceback', True)

        self.start_time = utcnow()

    def add_rules(self, rules):
        if hasattr(self, 'wildcard_router'):  # Tornado 4.5+
            self.wildcard_router.add_rules(rules)
        elif not self.handlers:
            self.add_handlers(r'.*$', rules)
        else:
            rules = [tornadoweb.URLSpec(*r) for r in rules]
            self.handlers[0][1].extend(rules)


def make_error(request, code, message):
    # If ?noerror=1 is set, only return HTTP error code.
    if "1" == request.handler.get_argument('noerror', None):
        logger.debug("Hide error %s %s for ?noerror=1.", code, message)
        return Response(200)

    data = dict(code=code, error=message)

    if hasattr(request, 'json'):
        return Response(code, body=data)

    if hasattr(request, 'instance'):
        data['instance'] = request.instance

    template = 'unauthorized.html' if 403 == code else 'error.html'
    response = render_template(
        template,
        nav=True, role=getattr(request, 'current_user', 'anonymous'),
        **data
    )
    response.status_code = code
    return response


# Change default cls argument to custom encoder.
if PY2:
    defaults = list(json.dumps.__defaults__)
    defaults[4] = JSONEncoder
    json.dumps.__defaults__ = tuple(defaults)
else:
    if json.dumps.__kwdefaults__ is None:
        json.dumps.__kwdefaults__ = dict()
    json.dumps.__kwdefaults__['cls'] = JSONEncoder


app = WebApplication()
# Hijack tornado.web access_log to log request in temboardui namespace.
tornadoweb.access_log = logging.getLogger('temboardui.access')
