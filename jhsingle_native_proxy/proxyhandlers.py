from tornado import web, httpclient, ioloop, httputil
import os
import aiohttp
import socket
from simpervisor import SupervisedProcess
from datetime import datetime
from asyncio import Lock
from .util import url_path_join
from .websocket import WebSocketHandlerMixin, pingable_ws_connect
from tornado.log import app_log
from jupyterhub.services.auth import HubOAuthenticated
from urllib.parse import urlunparse, urlparse


class AddSlashHandler(web.RequestHandler):
    """Add trailing slash to URLs that need them."""
    #@web.authenticated
    def get(self, *args):
        src = urlparse(self.request.uri)
        dest = src._replace(path=src.path + '/')
        self.redirect(urlunparse(dest))


class ProxyHandler(HubOAuthenticated, WebSocketHandlerMixin):
    """
    A tornado request handler that proxies HTTP and websockets from
    a given host/port combination. This class is not meant to be
    used directly as a means of overriding CORS. This presents significant
    security risks, and could allow arbitrary remote code access. Instead, it is
    meant to be subclassed and used for proxying URLs from trusted sources.
    Subclasses should implement open, http_get, post, put, delete, head, patch,
    and options.
    """
    def __init__(self, *args, **kwargs):
        self.proxy_base = ''
        self.absolute_url = kwargs.pop('absolute_url', False)
        self.host_whitelist = kwargs.pop('host_whitelist', ['localhost', '127.0.0.1'])
        super().__init__(*args, **kwargs)

    @property
    def log(self):
        """use tornado's logger"""
        return app_log

    # Support all the methods that tornado does by default except for GET which
    # is passed to WebSocketHandlerMixin and then to WebSocketHandler.

    async def open(self, port, proxied_path):
        raise NotImplementedError('Subclasses of ProxyHandler should implement open')

    async def http_get(self, host, port, proxy_path=''):
        '''Our non-websocket GET.'''
        raise NotImplementedError('Subclasses of ProxyHandler should implement http_get')

    def post(self, host, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement this post')

    def put(self, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement this put')

    def delete(self, host, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement delete')

    def head(self, host, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement head')

    def patch(self, host, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement patch')

    def options(self, host, port, proxy_path=''):
        raise NotImplementedError('Subclasses of ProxyHandler should implement options')

    def on_message(self, message):
        """
        Called when we receive a message from our client.
        We proxy it to the backend.
        """
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.write_message(message, binary=isinstance(message, bytes))

    def on_ping(self, data):
        """
        Called when the client pings our websocket connection.
        We proxy it to the backend.
        """
        self.log.debug('jupyter_server_proxy: on_ping: {}'.format(data))
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.protocol.write_ping(data)

    def on_pong(self, data):
        """
        Called when we receive a ping back.
        """
        self.log.debug('jupyter_server_proxy: on_pong: {}'.format(data))

    def on_close(self):
        """
        Called when the client closes our websocket connection.
        We close our connection to the backend too.
        """
        if hasattr(self, 'ws'):
            self.ws.close()

    def _record_activity(self):
        """Record proxied activity as API activity
        avoids proxied traffic being ignored by the notebook's
        internal idle-shutdown mechanism
        """
        self.settings['api_last_activity'] = datetime.utcnow()

    def _get_context_path(self, port):
        """
        Some applications need to know where they are being proxied from.
        This is either:
        - {base_url}/proxy/{port}
        - {base_url}/proxy/absolute/{port}
        - {base_url}/{proxy_base}
        """
        if self.proxy_base:
            return url_path_join(self.base_url, self.proxy_base)
        if self.absolute_url:
            return url_path_join(self.base_url, 'proxy', 'absolute', str(port))
        else:
            return url_path_join(self.base_url, 'proxy', str(port))

    def get_client_uri(self, protocol, host, port, proxied_path):
        context_path = self._get_context_path(port)
        if self.absolute_url:
            client_path = url_path_join(context_path, proxied_path)
        else:
            client_path = proxied_path

        client_uri = '{protocol}://{host}:{port}{path}'.format(
            protocol=protocol,
            host=host,
            port=port,
            path=client_path
        )
        if self.request.query:
            client_uri += '?' + self.request.query

        return client_uri

    def _build_proxy_request(self, host, port, proxied_path, body):

        headers = self.proxy_request_headers()

        client_uri = self.get_client_uri('http', host, port, proxied_path)
        # Some applications check X-Forwarded-Context and X-ProxyContextPath
        # headers to see if and where they are being proxied from.
        if not self.absolute_url:
            context_path = self._get_context_path(port)
            headers['X-Forwarded-Context'] = context_path
            headers['X-ProxyContextPath'] = context_path

        req = httpclient.HTTPRequest(
            client_uri, method=self.request.method, body=body,
            headers=headers, **self.proxy_request_options())
        return req

    def _check_host_whitelist(self, host):
        if callable(self.host_whitelist):
            return self.host_whitelist(self, host)
        else:
            return host in self.host_whitelist

    #@web.authenticated - handled in subclass
    async def proxy(self, host, port, proxied_path):
        '''
        This serverextension handles:
            {base_url}/proxy/{port([0-9]+)}/{proxied_path}
            {base_url}/proxy/absolute/{port([0-9]+)}/{proxied_path}
            {base_url}/{proxy_base}/{proxied_path}
        '''

        if not self._check_host_whitelist(host):
            self.set_status(403)
            self.write("Host '{host}' is not whitelisted. "
                       "See https://jupyter-server-proxy.readthedocs.io/en/latest/arbitrary-ports-hosts.html for info.".format(host=host))
            return

        if 'Proxy-Connection' in self.request.headers:
            del self.request.headers['Proxy-Connection']

        self._record_activity()

        if self.request.headers.get("Upgrade", "").lower() == 'websocket':
            # We wanna websocket!
            # jupyterhub/jupyter-server-proxy@36b3214
            self.log.info("we wanna websocket, but we don't define WebSocketProxyHandler")
            self.set_status(500)

        body = self.request.body
        if not body:
            if self.request.method == 'POST':
                body = b''
            else:
                body = None

        client = httpclient.AsyncHTTPClient()

        req = self._build_proxy_request(host, port, proxied_path, body)

        response = await client.fetch(req, raise_error=False)
        # record activity at start and end of requests
        self._record_activity()

        # For all non http errors...
        if response.error and type(response.error) is not httpclient.HTTPError:
            self.set_status(500)
            self.write(str(response.error))
        else:
            self.set_status(response.code, response.reason)

            # clear tornado default header
            self._headers = httputil.HTTPHeaders()

            for header, v in response.headers.get_all():
                if header not in ('Content-Length', 'Transfer-Encoding',
                                  'Content-Encoding', 'Connection'):
                    # some header appear multiple times, eg 'Set-Cookie'
                    self.add_header(header, v)

            if response.body:
                self.write(response.body)

    async def proxy_open(self, host, port, proxied_path=''):
        """
        Called when a client opens a websocket connection.
        We establish a websocket connection to the proxied backend &
        set up a callback to relay messages through.
        """

        if not self._check_host_whitelist(host):
            self.set_status(403)
            self.log.info("Host '{host}' is not whitelisted. "
                          "See https://jupyter-server-proxy.readthedocs.io/en/latest/arbitrary-ports-hosts.html for info.".format(host=host))
            self.close()
            return

        if not proxied_path.startswith('/'):
            proxied_path = '/' + proxied_path

        client_uri = self.get_client_uri('ws', host, port, proxied_path)
        headers = self.request.headers
        current_loop = ioloop.IOLoop.current()
        ws_connected = current_loop.asyncio_loop.create_future()

        def message_cb(message):
            """
            Callback when the backend sends messages to us
            We just pass it back to the frontend
            """
            # Websockets support both string (utf-8) and binary data, so let's
            # make sure we signal that appropriately when proxying
            self._record_activity()
            if message is None:
                self.close()
            else:
                self.write_message(message, binary=isinstance(message, bytes))

        def ping_cb(data):
            """
            Callback when the backend sends pings to us.
            We just pass it back to the frontend.
            """
            self._record_activity()
            self.ping(data)

        async def start_websocket_connection():
            self.log.info('Trying to establish websocket connection to {}'.format(client_uri))
            self._record_activity()
            request = httpclient.HTTPRequest(url=client_uri, headers=headers)
            self.ws = await pingable_ws_connect(request=request,
                                                on_message_callback=message_cb, on_ping_callback=ping_cb)
            ws_connected.set_result(True)
            self._record_activity()
            self.log.info('Websocket connection established to {}'.format(client_uri))

        current_loop.add_callback(start_websocket_connection)
        # Wait for the WebSocket to be connected before resolving.
        # Otherwise, messages sent by the client before the
        # WebSocket successful connection would be dropped.
        await ws_connected


    def proxy_request_headers(self):
        '''A dictionary of headers to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request.'''
        return self.request.headers.copy()

    def proxy_request_options(self):
        '''A dictionary of options to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request.'''
        return dict(follow_redirects=False)

    def check_xsrf_cookie(self):
        '''
        http://www.tornadoweb.org/en/stable/guide/security.html
        Defer to proxied apps.
        '''
        pass

    def select_subprotocol(self, subprotocols):
        '''Select a single Sec-WebSocket-Protocol during handshake.'''
        if isinstance(subprotocols, list) and subprotocols:
            self.log.info('Client sent subprotocols: {}'.format(subprotocols))
            return subprotocols[0]
        return super().select_subprotocol(subprotocols)


class LocalProxyHandler(ProxyHandler):
    """
    A tornado request handler that proxies HTTP and websockets
    from a port on the local system. Same as the above ProxyHandler,
    but specific to 'localhost'.
    """
    async def http_get(self, port, proxied_path):
        return await self.proxy(port, proxied_path)

    async def open(self, port, proxied_path):
        return await self.proxy_open('localhost', port, proxied_path)

    def post(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def put(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def delete(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def head(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def patch(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def options(self, port, proxied_path):
        return self.proxy(port, proxied_path)

    def proxy(self, port, proxied_path):
        return super().proxy('localhost', port, proxied_path)


class RemoteProxyHandler(ProxyHandler):
    """
    A tornado request handler that proxies HTTP and websockets
    from a port on a specified remote system.
    """

    async def http_get(self, host, port, proxied_path):
        return await self.proxy(host, port, proxied_path)

    def post(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    def put(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    def delete(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    def head(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    def patch(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    def options(self, host, port, proxied_path):
        return self.proxy(host, port, proxied_path)

    async def open(self, host, port, proxied_path):
        return await self.proxy_open(host, port, proxied_path)

    def proxy(self, host, port, proxied_path):
        return super().proxy(host, port, proxied_path)


class SuperviseAndProxyHandler(LocalProxyHandler):
    '''Manage a given process and requests to it '''

    def __init__(self, *args, **kwargs):
        self.requested_port = 0
        self.mappath = {}

        super().__init__(*args, **kwargs)

    def initialize(self, state, authtype, *args, **kwargs):
        self.state = state
        if 'proc_lock' not in state:
            state['proc_lock'] = Lock()

        self.authtype = authtype

        super().initialize(*args, **kwargs)

    name = 'process'

    @property
    def port(self):
        """
        Allocate either the requested port or a random empty port for use by
        application
        """
        if 'port' not in self.state:
            sock = socket.socket()
            sock.bind(('', self.requested_port))
            self.state['port'] = sock.getsockname()[1]
            sock.close()
        return self.state['port']

    def get_cwd(self):
        """Get the current working directory for our process
        Override in subclass to launch the process in a directory
        other than the current.
        """
        return os.getcwd()

    def get_env(self):
        '''Set up extra environment variables for process. Typically
           overridden in subclasses.'''
        return {}

    def get_timeout(self):
        """
        Return timeout (in s) to wait before giving up on process readiness
        """
        return 5

    async def _http_ready_func(self, p):
        url = 'http://localhost:{}'.format(self.port)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    # We only care if we get back *any* response, not just 200
                    # If there's an error response, that can be shown directly to the user
                    self.log.debug('Got code {} back from {}'.format(resp.status, url))
                    return True
            except aiohttp.ClientConnectionError:
                self.log.debug('Connection to {} refused'.format(url))
                return False

    async def ensure_process(self):
        """
        Start the process
        """
        # We don't want multiple requests trying to start the process at the same time
        # FIXME: Make sure this times out properly?
        # Invariant here should be: when lock isn't being held, either 'proc' is in state &
        # running, or not.
        async with self.state['proc_lock']:
            if 'proc' not in self.state:
                # FIXME: Prevent races here
                # FIXME: Handle graceful exits of spawned processes here
                cmd = self.get_cmd()
                server_env = os.environ.copy()

                # Set up extra environment variables for process
                server_env.update(self.get_env())

                timeout = self.get_timeout()

                self.log.info(cmd)

                proc = SupervisedProcess(self.name, *cmd, env=server_env, ready_func=self._http_ready_func, ready_timeout=timeout, log=self.log)
                self.state['proc'] = proc

                try:
                    await proc.start()

                    is_ready = await proc.ready()

                    if not is_ready:
                        await proc.kill()
                        raise web.HTTPError(500, 'could not start {} in time'.format(self.name))
                except:
                    # Make sure we remove proc from state in any error condition
                    del self.state['proc']
                    raise

    @web.authenticated
    async def oauth_proxy(self, port, path):
        return await self.core_proxy(port, path)

    async def core_proxy(self, port, path):
        if not path.startswith('/'):
            path = '/' + path

        if self.mappath:
            if callable(self.mappath):
                raise Exception("Not implemented: path = call_with_asked_args(self.mappath, {'path': path})")
            else:
                path = self.mappath.get(path, path)

        self.log.debug('In proxy')

        await self.ensure_process()

        self.log.debug('In proxy ensured process')

        return await super().proxy(self.port, path)

    async def proxy(self, port, path):
        if self.authtype == 'oauth':
            return await self.oauth_proxy(port, path)
        else:
            return await self.core_proxy(port, path)

    async def http_get(self, path):
        self.log.info('SuperviseAndProxyHandler http_get {} {}'.format(self.port, path))
        return await self.proxy(self.port, path)

    async def open(self, path):
        await self.ensure_process()
        return await super().open(self.port, path)

    def post(self, path):
        return self.proxy(self.port, path)

    def put(self, path):
        return self.proxy(self.port, path)

    def delete(self, path):
        return self.proxy(self.port, path)

    def head(self, path):
        return self.proxy(self.port, path)

    def patch(self, path):
        return self.proxy(self.port, path)

    def options(self, path):
        return self.proxy(self.port, path)


def _make_serverproxy_handler(name, command, environment, timeout, absolute_url, port, mappath):
    """
    Create a SuperviseAndProxyHandler subclass with given parameters
    """
    # FIXME: Set 'name' properly
    class _Proxy(SuperviseAndProxyHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.name = name
            self.proxy_base = name
            self.absolute_url = absolute_url
            self.requested_port = port
            self.mappath = mappath

        @property
        def process_args(self):
            return {
                'port': self.port,
                'base_url': self.base_url,
                '--': '--'
            }
        @property
        def base_url(self):
            return self.settings.get('base_url', '/')

        @property
        def hub_users(self):
            return {self.settings['user']}

        @property
        def hub_groups(self):
            if self.settings['group']:
                return {self.settings['group']}
            return set()

        def _render_template(self, value):
            args = self.process_args
            if type(value) is str:
                return value.format(**args)
            elif type(value) is list:
                return [self._render_template(v) for v in value]
            elif type(value) is dict:
                return {
                    self._render_template(k): self._render_template(v)
                    for k, v in value.items()
                }
            else:
                raise ValueError('Value of unrecognized type {}'.format(type(value)))

        def get_cmd(self):
            if callable(command):
                raise Exception("Not implemented: self._render_template(call_with_asked_args(command, self.process_args))")
            else:
                return self._render_template(command)

        def get_env(self):
            if callable(environment):
                raise Exception("return self._render_template(call_with_asked_args(environment, self.process_args))")
            else:
                return self._render_template(environment)

        def get_timeout(self):
            return timeout

    return _Proxy

