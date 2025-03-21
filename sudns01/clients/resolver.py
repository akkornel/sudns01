# vim: ts=4 sw=4 noet

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
import codecs
import logging
import socket
from typing import NoReturn

# PyPi imports
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.tsig
import dns.update

# Local imports
from sudns01.clients.exceptions import *

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
		self._resolver.lifetime = RESOLVER_TIMEOUT

		self._resolver_nocache = dns.resolver.Resolver(configure=True)
		self._resolver.retry_servfail = True
		self._resolver.lifetime = RESOLVER_TIMEOUT

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

	def get_txt(self,
		query: dns.name.Name | str,
		cached: bool = True,
		search: bool = True,
		raise_on_cdname: bool = True,
	) -> list[bytes | tuple[bytes]]:
		"""Return TXT records for a name.

		Do an TXT query and return the results.

		TXT records do not have a defined encoding.  The work of decoding is
		left up to the client.  In this method's description, "strings" means
		"strings of bytes".

		A single TXT record may contain multiple strings.  If a TXT record
		contains a single string, that TXT record's corresponding list entry
		will contain a single bytes object.  Otherwise, the corresponding list
		entry will contain a tuple of bytes objects.

		Results are returned in the order provided by the DNS server.

		:param query: The DNS name to look up.

		:param cached: If True, use cached lookups.

		:param search: If True, use any configured search domains.

		:param raise_on_cdname: If True, if the lookup of query results in following a CNAME or DNAME, raise an exception.  Otherwise, do a lookup of the query's parent.

		:returns: The name of the zone containing the given FQDN.

		:raises ResolverError: There was a problem looking up the name.  Maybe you can retry?

		:raises ResolverErrorPermanent: There was a permanent problem doing your DNS lookup.
		"""

		debug(
			f"Resolver running get_txt for {query} with " +
			('cached ' if cached else '') +
			('search' if search else '')
		)

		# Do we use our caching resolver, or not?
		resolver = (self._resolver if cached is True else self._resolver_nocache)

		# Do the lookup!
		try:
			reply = resolver.resolve(
				qname=query,
				rdclass=dns.rdataclass.IN,
				rdtype=dns.rdatatype.TXT,
				search=search,
				raise_on_no_answer=False,
			)
		except (
			dns.resolver.NXDOMAIN,
		):
			warning(f"NXDOMAIN for {query}")
			return list()
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

		# Do our CNAME/DNAME check
		self._check_has_cdname(
			answer=reply,
			raise_on_cdname=raise_on_cdname,
		)

		# If we did not get any answers, that's OK.
		if reply.rrset is None:
			debug(f"Found no TXT records for {query}")
			return list()

		# Extract the strings from the list of results.
		# At this point, we have a list of tuples.
		text_tuples = list(
			map(lambda x: x.strings, reply.rrset)
		)

		# Replace all single-item tuples with their single item.
		# This gives us a list containing either bytes or tuples of bytes.
		# This is what we return to the client!
		debug(f"Found {len(text_tuples)} result(s)")
		return list(
			(entries if len(entries) > 1 else entries[0])
			for entries in text_tuples
		)

	def get_zone_name(self,
		query: dns.name.Name | str,
		cached: bool = True,
		raise_on_cdname: bool = True,
	) -> dns.name.Name:
		"""Return the zone name for a FQDN.

		A Zone is a collection of DNS records.  When making changes to DNS,
		instead of providing the Fully-Qualified Domain Name (FQDN) for every
		record, you strip off the zone part of the FQDN.

		For example, take FQDN "blargh.stanford.edu".  The zone is
		"stanford.edu".  Now, take FQDN
		"133.96.19.34.bc.googleusercontent.com.".  In that case, the zone is
		"googleusercontent.com"!

		To find out the zone name, you can look up the SOA (Start of Authority)
		record for a FQDN.  The information comes in the Authority section of
		the answer, with the zone as the name attached to the record.

		This takes a FQDN, and returns a the name of the zone.

		:param query: The DNS name to look up.

		:param cached: If True, use cached lookups.

		:param raise_on_cdname: If True, if the lookup of query results in following a CNAME or DNAME, raise an exception.  Otherwise, do a lookup of the query's parent.

		:returns: The name of the zone containing the given FQDN.

		:raises ValueError: The name you gave is not "absolute" (it's not a FQDN).

		:raises ResolverError: There was a problem looking up the name.  Maybe you can retry?

		:raises ResolverErrorCDName: A CNAME or DNAME was encountered during the lookup.

		:raises ResolverErrorPermanent: There was a permanent problem doing your DNS lookup.
		"""

		debug(
			f"Resolver running get_soa for {query} with " +
			('raise_on_cdname ' if raise_on_cdname else '') +
			('cached ' if cached else '')
		)
		if isinstance(query, dns.name.Name) and not query.is_absolute():
			raise ValueError(f"{query} is not a FQDN")

		# Do we use our caching resolver, or not?
		resolver = (self._resolver if cached is True else self._resolver_nocache)

		# Do the lookup!
		try:
			answer = resolver.resolve(
				qname=query,
				rdtype=dns.rdatatype.SOA,
				search=False,
                raise_on_no_answer=False,
			)
		except (
			dns.resolver.NXDOMAIN,
		):
			warning(f"NXDOMAIN for {query}")
			raise ResolverErrorPermanent(f"NXDOMAIN for {query}")
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

		# Check if we got a CNAME or DNAME in the response.
		# If we did, we might *or* might not want to raise the exception.
		try:
			self._check_has_cdname(
				answer=answer,
				raise_on_cdname=raise_on_cdname,
			)
		except ResolverErrorCDName:
			if raise_on_cdname:
				raise
			else:
				parent_query = answer.response.question[0].name.parent()
				info(f"Trying a zone lookup for parent {parent_query}")
				return self.get_zone_name(
					query=parent_query,
					cached=cached,
					raise_on_cdname=raise_on_cdname,
				)

		# We can find SOA record in one of two places:
		# * If we queried the zone, we'll get our SOA in the Answer section.
		# * Otherwise, we'll get it in the Authority section.
		for answer_record in answer.response.answer:
			if answer_record.rdtype is dns.rdatatype.SOA:
				debug('Found a SOA record in the Answer section!')
				return answer_record.name
		if len(answer.response.authority) > 0:
			debug('Found SOA record in the Authority section')
			return answer.response.authority[0].name

		# Per RFC 1034 3.7, including the SOA record in the Authority section
		# is optional.  But, we're asking explicitly for an SOA record here.
		# So, error out if we didn't get one.
		raise ResolverErrorPermanent(f"No SOA record received for {query}")

	@staticmethod
	def _check_has_cdname(
		answer: dns.resolver.Answer,
		raise_on_cdname: bool,
	) -> bool | NoReturn:
		"""Did the resolver end up taking us through a CNAME or DNAME?

		For example, "www.stanford.edu" is currently a CNAME to
		pantheon-systems.map.fastly.net., so we'd end up getting the SOA for
		"fastly.net", which is not what we want.  Check to see if our answer
		contains any CNAMEs.  If it does, we need to move our SOA query 'up'
		one level (for example, from "www.stanford.edu" to "stanford.edu".

		:param answer: The DNS resolver answer to inspect.

		:param raise_on_cdname: If a CNAME or DNAME is found, should we raise an exception?

		:returns: True if a CNAME or DNAME was found, and if raise_on_cdname is False; else False.
		"""
		redirects_found = 0

		# Check for any CNAMEs or DNAMEs, and count/log them
		if len(answer.response.answer) > 0:
			for response_answer in answer.response.answer:
				if response_answer.rdtype in (
					dns.rdatatype.CNAME,
					dns.rdatatype.DNAME,
				):
					if raise_on_cdname:
						cname_dname_log = warning
					else:
						cname_dname_log = info
					cname_dname_log(
						f"Found {response_answer.rdtype.name} to {response_answer[0]} in response"
					)
					redirects_found += 1

		# Decide if we need to take action.
		if redirects_found > 0:
			# Do we fail, or do we search one level up?
			if raise_on_cdname:
				raise ResolverErrorCDName(
					f"Lookup of {answer.response.question} returned at least one CNAME or DNAME."
				)
			else:
				return True
		else:
			return False
