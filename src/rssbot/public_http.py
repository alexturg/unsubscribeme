"""urllib handlers that pin each connection to validated public addresses."""
import http.client
import socket
import urllib.request

from .source_http import SourceFetchError, check_address


def public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host, port = address
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records:
        raise OSError("No addresses for source host")
    try:
        for _, _, _, _, sockaddr in records:
            check_address(sockaddr[0])
    except SourceFetchError as exc:
        raise OSError("Source resolved to a forbidden address") from exc
    last_error = None
    for family, socktype, proto, _, sockaddr in records:
        connection = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            # sockaddr is numeric: there is no second hostname resolution.
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise last_error or OSError("Cannot connect to source")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # HTTPSConnection still verifies the original hostname and sends its SNI.
        self._create_connection = public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _PublicHTTPSConnection, req, context=self._context,
            check_hostname=self._check_hostname,
        )
