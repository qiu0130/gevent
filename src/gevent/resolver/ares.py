# Copyright (c) 2011-2015 Denis Bilenko. See LICENSE for details.
"""
c-ares based hostname resolver.
"""
from __future__ import absolute_import, print_function, division
import os
import sys

from _socket import getaddrinfo
from _socket import gaierror
from _socket import error

from gevent._compat import string_types
from gevent._compat import text_type

from gevent._compat import reraise
from gevent._compat import PY3

from gevent.hub import Waiter
from gevent.hub import get_hub

from gevent.socket import AF_UNSPEC
from gevent.socket import AF_INET
from gevent.socket import AF_INET6
from gevent.socket import SOCK_STREAM
from gevent.socket import SOCK_DGRAM
from gevent.socket import SOCK_RAW
from gevent.socket import AI_NUMERICHOST

from gevent._config import config
from gevent._config import AresSettingMixin

from .cares import channel, InvalidIP # pylint:disable=import-error,no-name-in-module
from . import _lookup_port as lookup_port
from . import _resolve_special
from . import AbstractResolver

__all__ = ['Resolver']


class Resolver(AbstractResolver):
    """
    Implementation of the resolver API using the `c-ares`_ library.

    This implementation uses the c-ares library to handle name
    resolution. c-ares is natively asynchronous at the socket level
    and so integrates well into gevent's event loop.

    In comparison to :class:`gevent.resolver_thread.Resolver` (which
    delegates to the native system resolver), the implementation is
    much more complex. In addition, there have been reports of it not
    properly honoring certain system configurations (for example, the
    order in which IPv4 and IPv6 results are returned may not match
    the threaded resolver). However, because it does not use threads,
    it may scale better for applications that make many lookups.

    There are some known differences from the system resolver.

    - ``gethostbyname_ex`` and ``gethostbyaddr`` may return
      different for the ``aliaslist`` tuple member. (Sometimes the
      same, sometimes in a different order, sometimes a different
      alias altogether.)

    - ``gethostbyname_ex`` may return the ``ipaddrlist`` in a
      different order.

    - ``getaddrinfo`` does not return ``SOCK_RAW`` results.

    - ``getaddrinfo`` may return results in a different order.

    - Handling of ``.local`` (mDNS) names may be different, even
      if they are listed in the hosts file.

    - c-ares will not resolve ``broadcasthost``, even if listed in
      the hosts file.

    - This implementation may raise ``gaierror(4)`` where the
      system implementation would raise ``herror(1)``.

    - The results for ``localhost`` may be different. In
      particular, some system resolvers will return more results
      from ``getaddrinfo`` than c-ares does, such as SOCK_DGRAM
      results, and c-ares may report more ips on a multi-homed
      host.

    - The system implementation may return some names fully qualified, where
      this implementation returns only the host name. This appears to be
      the case only with entries found in ``/etc/hosts``.

    - c-ares supports a limited set of flags for ``getnameinfo`` and
      ``getaddrinfo``; unknown flags are ignored. System-specific flags
      such as ``AI_V4MAPPED_CFG`` are not supported.

    .. caution::

        This module is considered extremely experimental on PyPy, and
        due to its implementation in cython, it may be slower. It may also lead to
        interpreter crashes.

    .. versionchanged:: 1.5.0
       This version of gevent typically embeds c-ares 1.15.0 or newer. In
       that version of c-ares, domains ending in ``.onion`` `are never
       resolved <https://github.com/c-ares/c-ares/issues/196>`_ or even
       sent to the DNS server.

    .. _c-ares: http://c-ares.haxx.se
    """

    cares_class = channel

    def __init__(self, hub=None, use_environ=True, **kwargs):
        if hub is None:
            hub = get_hub()
        self.hub = hub
        if use_environ:
            for setting in config.settings.values():
                if isinstance(setting, AresSettingMixin):
                    value = setting.get()
                    if value is not None:
                        kwargs.setdefault(setting.kwarg_name, value)
        self.cares = self.cares_class(hub.loop, **kwargs)
        self.pid = os.getpid()
        self.params = kwargs
        self.fork_watcher = hub.loop.fork(ref=False)
        self.fork_watcher.start(self._on_fork)

    def __repr__(self):
        return '<gevent.resolver_ares.Resolver at 0x%x ares=%r>' % (id(self), self.cares)

    def _on_fork(self):
        # NOTE: See comment in gevent.hub.reinit.
        pid = os.getpid()
        if pid != self.pid:
            self.hub.loop.run_callback(self.cares.destroy)
            self.cares = self.cares_class(self.hub.loop, **self.params)
            self.pid = pid

    def close(self):
        if self.cares is not None:
            self.hub.loop.run_callback(self.cares.destroy)
            self.cares = None
        self.fork_watcher.stop()

    def gethostbyname(self, hostname, family=AF_INET):
        hostname = _resolve_special(hostname, family)
        return self.gethostbyname_ex(hostname, family)[-1][0]

    def gethostbyname_ex(self, hostname, family=AF_INET):
        if PY3:
            if isinstance(hostname, str):
                hostname = hostname.encode('idna')
            elif not isinstance(hostname, (bytes, bytearray)):
                raise TypeError('Expected es(idna), not %s' % type(hostname).__name__)
        else:
            if isinstance(hostname, text_type):
                hostname = hostname.encode('ascii')
            elif not isinstance(hostname, str):
                raise TypeError('Expected string, not %s' % type(hostname).__name__)

        while True:
            ares = self.cares
            try:
                waiter = Waiter(self.hub)
                ares.gethostbyname(waiter, hostname, family)
                result = waiter.get()
                if not result[-1]:
                    raise gaierror(-5, 'No address associated with hostname')
                return result
            except gaierror:
                if ares is self.cares:
                    if hostname == b'255.255.255.255':
                        # The stdlib handles this case in 2.7 and 3.x, but ares does not.
                        # It is tested by test_socket.py in 3.4.
                        # HACK: So hardcode the expected return.
                        return ('255.255.255.255', [], ['255.255.255.255'])
                    raise
                # "self.cares is not ares" means channel was destroyed (because we were forked)

    def _lookup_port(self, port, socktype):
        return lookup_port(port, socktype)

    def _getaddrinfo(self, host, port, family=0, socktype=0, proto=0, flags=0):
        # pylint:disable=too-many-locals,too-many-branches
        if isinstance(host, text_type):
            host = host.encode('idna')
        if not isinstance(host, bytes) or (flags & AI_NUMERICHOST) or host in (
                b'localhost', b'ip6-localhost'):
            # this handles cases which do not require network access
            # 1) host is None
            # 2) host is of an invalid type
            # 3) AI_NUMERICHOST flag is set
            # 4) It's a well-known alias. TODO: This is special casing that we don't
            #    really want to do. It's here because it resolves a discrepancy with the system
            #    resolvers caught by test cases. In gevent 20.4.0, this only worked correctly on
            #    Python 3 and not Python 2, by accident.
            return getaddrinfo(host, port, family, socktype, proto, flags)
            # we also call _socket.getaddrinfo below if family is not one of AF_*

        port, socktypes = self._lookup_port(port, socktype)

        socktype_proto = [(SOCK_STREAM, 6), (SOCK_DGRAM, 17), (SOCK_RAW, 0)]
        if socktypes:
            socktype_proto = [(x, y) for (x, y) in socktype_proto if x in socktypes]
        if proto:
            socktype_proto = [(x, y) for (x, y) in socktype_proto if proto == y]

        ares = self.cares

        if family == AF_UNSPEC:
            ares_values = _Values(self.hub, 2)
            ares.gethostbyname(ares_values, host, AF_INET)
            ares.gethostbyname(ares_values, host, AF_INET6)
        elif family == AF_INET:
            ares_values = _Values(self.hub, 1)
            ares.gethostbyname(ares_values, host, AF_INET)
        elif family == AF_INET6:
            ares_values = _Values(self.hub, 1)
            ares.gethostbyname(ares_values, host, AF_INET6)
        else:
            raise gaierror(5, 'ai_family not supported: %r' % (family, ))

        values = ares_values.get()
        if len(values) == 2 and values[0] == values[1]:
            values.pop()

        result = []
        result4 = []
        result6 = []

        for addrs in values:
            if addrs.family == AF_INET:
                for addr in addrs[-1]:
                    sockaddr = (addr, port)
                    for socktype4, proto4 in socktype_proto:
                        result4.append((AF_INET, socktype4, proto4, '', sockaddr))
            elif addrs.family == AF_INET6:
                for addr in addrs[-1]:
                    if addr == '::1':
                        dest = result
                    else:
                        dest = result6
                    sockaddr = (addr, port, 0, 0)
                    for socktype6, proto6 in socktype_proto:
                        dest.append((AF_INET6, socktype6, proto6, '', sockaddr))

        # As of 2016, some platforms return IPV6 first and some do IPV4 first,
        # and some might even allow configuration of which is which. For backwards
        # compatibility with earlier releases (but not necessarily resolver_thread!)
        # we return 4 first. See https://github.com/gevent/gevent/issues/815 for more.
        result += result4 + result6

        if not result:
            raise gaierror(-5, 'No address associated with hostname')

        return result

    def getaddrinfo(self, host, port, family=0, socktype=0, proto=0, flags=0):
        while True:
            ares = self.cares
            try:
                return self._getaddrinfo(host, port, family, socktype, proto, flags)
            except gaierror:
                if ares is self.cares:
                    raise

    def _gethostbyaddr(self, ip_address):
        if PY3:
            if isinstance(ip_address, str):
                ip_address = ip_address.encode('idna')
            elif not isinstance(ip_address, (bytes, bytearray)):
                raise TypeError('Expected es(idna), not %s' % type(ip_address).__name__)
        else:
            if isinstance(ip_address, text_type):
                ip_address = ip_address.encode('ascii')
            elif not isinstance(ip_address, str):
                raise TypeError('Expected string, not %s' % type(ip_address).__name__)

        waiter = Waiter(self.hub)
        try:
            self.cares.gethostbyaddr(waiter, ip_address)
            return waiter.get()
        except InvalidIP:
            result = self._getaddrinfo(ip_address, None, family=AF_UNSPEC, socktype=SOCK_DGRAM)
            if not result:
                raise
            _ip_address = result[0][-1][0]
            if isinstance(_ip_address, text_type):
                _ip_address = _ip_address.encode('ascii')
            if _ip_address == ip_address:
                raise
            waiter.clear()
            self.cares.gethostbyaddr(waiter, _ip_address)
            return waiter.get()

    def gethostbyaddr(self, ip_address):
        ip_address = _resolve_special(ip_address, AF_UNSPEC)
        while True:
            ares = self.cares
            try:
                return self._gethostbyaddr(ip_address)
            except gaierror:
                if ares is self.cares:
                    raise

    def _getnameinfo(self, sockaddr, flags):
        if not isinstance(flags, int):
            raise TypeError('an integer is required')
        if not isinstance(sockaddr, tuple):
            raise TypeError('getnameinfo() argument 1 must be a tuple')

        address = sockaddr[0]
        if not PY3 and isinstance(address, text_type):
            address = address.encode('ascii')

        if not isinstance(address, string_types):
            raise TypeError('sockaddr[0] must be a string, not %s' % type(address).__name__)

        port = sockaddr[1]
        if not isinstance(port, int):
            raise TypeError('port must be an integer, not %s' % type(port))

        waiter = Waiter(self.hub)
        result = self._getaddrinfo(address, str(sockaddr[1]), family=AF_UNSPEC, socktype=SOCK_DGRAM)
        if not result:
            reraise(*sys.exc_info())
        elif len(result) != 1:
            raise error('sockaddr resolved to multiple addresses')
        family, _socktype, _proto, _name, address = result[0]

        if family == AF_INET:
            if len(sockaddr) != 2:
                raise error("IPv4 sockaddr must be 2 tuple")
        elif family == AF_INET6:
            address = address[:2] + sockaddr[2:]

        self.cares.getnameinfo(waiter, address, flags)
        node, service = waiter.get()

        if service is None:
            if PY3:
                # ares docs: "If the query did not complete
                # successfully, or one of the values was not
                # requested, node or service will be NULL ". Python 2
                # allows that for the service, but Python 3 raises
                # an error. This is tested by test_socket in py 3.4
                err = gaierror('nodename nor servname provided, or not known')
                err.errno = 8
                raise err
            service = '0'
        return node, service

    def getnameinfo(self, sockaddr, flags):
        while True:
            ares = self.cares
            try:
                return self._getnameinfo(sockaddr, flags)
            except gaierror:
                if ares is self.cares:
                    raise


class _Values(object):
    # helper to collect the results of multiple c-ares calls
    # and ignore errors unless nothing has succeeded

    # QQQ could probably be moved somewhere - hub.py?

    __slots__ = ['count', 'values', 'error', 'waiter']

    def __init__(self, hub, count):
        self.count = count
        self.values = []
        self.error = None
        self.waiter = Waiter(hub)

    def __call__(self, source):
        self.count -= 1
        if source.exception is None:
            self.values.append(source.value)
        else:
            self.error = source.exception
        if self.count <= 0:
            self.waiter.switch(None)

    def get(self):
        self.waiter.get()
        if self.values:
            return self.values
        assert error is not None
        raise self.error # pylint:disable=raising-bad-type
