"""Domain check before a first email: skip an address whose domain cannot receive mail at all.

Only the domain is checked (MX record, or an A record as the SMTP fallback). A mailbox that no longer exists on a
working domain ("Address not found") cannot be detected this way: probing mailboxes needs outbound port 25, which
GitHub runners block.
"""
from __future__ import annotations

from functools import lru_cache

import dns.exception
import dns.resolver


@lru_cache(maxsize=4096)
def domain_accepts_mail(domain: str) -> bool | None:
    """True: has a mail server. False: the domain does not exist or has no MX/A (or a null MX). None: lookup failed."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 8.0
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        return any(hosts)                              # RFC 7505 null MX ("0 .") means "accepts no mail"
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass
    except (dns.exception.Timeout, dns.resolver.NoNameservers):
        return None
    try:
        resolver.resolve(domain, "A")
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except (dns.exception.Timeout, dns.resolver.NoNameservers):
        return None
