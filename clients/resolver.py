#vim: ts=4 sw=4 noet

# Copyright 2025 The Board of Trustees of the Leland Stanford Junior University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A simple DNS resolver

This contains a simple DNS resolver for internal use.  There are a few special
DNS lookups that we need to do:

* Doing a SOA lookup, to figoure out a zone from an FQDN.

* Doing TXT lookups.
"""

# stdlib imports
import logging
import socket

# PyPi imports
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.resolver
import dns.tsig
import dns.update

# Local imports
from clients.exceptions import *

RESOLVER_TIMEOUT: int = 10
"""What timeout do we use?
"""

CACHE_CLEAN_INTERVAL: int = 300
"""How often do we clean out cached records?
"""

# Set up logging
logger = logging.getLogger(__name__)
exception = logger.exception
error = logger.error
warning = logger.warning
info = logger.info
debug = logger.debug


class ResolverClient():
	"""A stub DNS resolver, for DNS lookups we need to do.

	This wrap's dnspython's stub resolver.  Both caching and non-caching
    options are available within the same instance.
	"""

	_resolver: dns.resolver.Resolver
	"""A resolver with a cache"""

	_resolver_nocache: dns.resolver.Resolver
	"""A resolver without a cache"""

	def __init__(
		self,
	):
		self._resolver = dns.resolver.Resolver(configure=True)
		self._resolver.cache = dns.resolver.Cache(cleaning_interval=CACHE_CLEAN_INTERVAL)
		self._resolver.retry_servfail = True

		self._resolver_nocache = dns.resolver.Resolver(configure=True)
		self._resolver.retry_servfail = True

	def get_ip(self,
		query: dns.name.Name | str,
		cached: bool = True,
		ipv6: bool = True,
		search: bool = True,
	) -> list[str]:
		"""Return a list of IPs for a query.

		Do an A query (and, if enabled, also a AAAA query), and return the
		results.  IPv6 (if enabled) IPs are returned before IPv4.  Within each
		address type, results are returned in the order provided by the DNS
		server.

		:param query: The DNS name to look up.

		:param cached: If True, use cached names.

		:param ipv6: If True, include IPv6 addresses in the list.

		:param search: If True, use any configured search domains.

		:returns: A list of IP addresses, which may be empty.

		:raises ResolverError: There was a problem looking up the name.  Maybe you can retry?

		:raises ResolverErrorPermanent: There was a permanent problem doing your DNS lookup.
		"""

		debug(
			f"Resolver running get_ip for {query} with " +
			('cached ' if cached else '') +
			('ipv6 ' if ipv6 else '') +
			('search' if search else '')
		)

		# Do we use our caching resolver, or not?
		resolver = (self._resolver if cached is True else self._resolver_nocache)

		# Do we want A and AAAA records, or just A records?
		family = (socket.AF_UNSPEC if ipv6 is True else socket.AF_INET)

		# Do the lookup!
		try:
			answers = resolver.resolve_name(
				name=query,
				family=family,
				search=search,
				lifetime=RESOLVER_TIMEOUT,
			)
			addresses = list(answers.addresses())
		except (
			dns.resolver.NXDOMAIN,
			dns.resolver.NoAnswer,
		):
			warning(f"NXDOMAIN or no answer for {query}")
			addresses = list()
		except (
			dns.resolver.NoNameservers,
			dns.resolver.LifetimeTimeout,
		) as e:
			exception("Either NoNameservers or LifetimeTimeout for {query}")
			raise ResolverError(
				'All nameservers returned errors or did not respond.'
			)
		except dns.resolver.YXDOMAIN:
			exception(f"YXDOMAIN for {query}")
			raise ResolverErrorPermanent(
				'A YXDOMAIN error happened.  What?!'
			)

		debug(f"Found {len(addresses)} result(s)")
		return addresses

