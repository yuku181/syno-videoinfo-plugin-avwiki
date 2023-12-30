"""The implementation of the doh function."""
import base64
import concurrent
import concurrent.futures
import json
import logging
import socket
import struct
import urllib
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from scraper.functions import Args, Func

_logger = logging.getLogger(__name__)

# define a global set to store registered hosts
_registered_hosts = set()

# define a global thread pool executor
_executor = concurrent.futures.ThreadPoolExecutor()

# define default DoH configuration
_doh_timeout = 5
_doh_cache: Dict[str, str] = {}
_doh_resolvers = [
    # https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https
    "1.0.0.1",
    "1.1.1.1",

    # https://support.quad9.net/hc/en-us
    "9.9.9.9",
    "149.112.112.112",

    # https://support.opendns.com/hc/en-us
    "208.67.220.220",
    "208.67.222.222",

    # https://developers.google.com/speed/public-dns/docs/doh
    "dns.google",

    # https://adguard-dns.io/public-dns.html
    "dns.adguard-dns.com",
]


def _patched_getaddrinfo(host, *args, **kwargs):
    """Patched version of socket.getaddrinfo."""
    if host not in _registered_hosts:
        return _orig_getaddrinfo(host, *args, **kwargs)

    # check if the host is already resolved
    if host in _doh_cache:
        ip = _doh_cache[host]
        _logger.info("Resolved [%s] to [%s] (cached)", host, ip)
        return _orig_getaddrinfo(ip, *args, **kwargs)

    # resolve the host using DoH
    futures = []
    for resolver in _doh_resolvers:
        futures.append(_executor.submit(_doh_query, resolver, host))

    for future in concurrent.futures.as_completed(futures):
        ip = future.result()
        if ip is not None:
            _logger.info("Resolved [%s] to [%s]", host, ip)
            _doh_cache[host] = ip
            host = ip
            break

    return _orig_getaddrinfo(host, *args, **kwargs)


# monkey patch socket.getaddrinfo
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = _patched_getaddrinfo


def _doh_query(resolver: str, host: str) -> Optional[str]:
    """Query the IP address of the given host using the given DoH resolver."""

    # construct DNS query message (RFC 1035)
    header = b"".join(
        [
            b"\x00\x00",  # ID: 0
            b"\x01\x00",  # FLAGS: standard recursive query
            b"\x00\x01",  # QDCOUNT: 1
            b"\x00\x00",  # ANCOUNT: 0
            b"\x00\x00",  # NSCOUNT: 0
            b"\x00\x00",  # ARCOUNT: 0
        ]
    )
    question = b"".join(
        [
            b"".join(
                [
                    struct.pack("B", len(item)) + item.encode("utf-8")
                    for item in host.split(".")
                ]
            )
            + b"\x00",  # QNAME: domain name sequence
            b"\x00\x01",  # QTYPE: A
            b"\x00\x01",  # QCLASS: IN
        ]
    )
    message = header + question

    try:
        # send GET request to DoH resolver (RFC 8484)
        b64message = base64.b64encode(message).decode("utf-8").rstrip("=")
        url = f"https://{resolver}/dns-query?dns={b64message}"
        headers = {"Content-Type": "application/dns-message"}
        _logger.info("DoH request: %s", url)

        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=_doh_timeout) as response:
            _logger.info("Resolver(%s) response: %s", resolver, response.status)
            if response.status != 200:
                return None
            resp_body = response.read()

        # parse DNS response message (RFC 1035)
        # name(compressed):2 + type:2 + class:2 + ttl:4 + rdlength:2 = 12 bytes
        first_rdata_start = len(header) + len(question) + 12
        # rdata(A record) = 4 bytes
        first_rdata_end = first_rdata_start + 4
        # convert rdata to IP address
        return socket.inet_ntoa(resp_body[first_rdata_start:first_rdata_end])
    except Exception as e:
        _logger.error("Resolver(%s) request error: %s", resolver, e)
        return None


def _doh_query_json(resolver: str, host: str) -> Optional[str]:
    """Query the IP address of the given host using the given DoH resolver."""
    url = f"https://{resolver}/dns-query?name={host}&type=A"
    headers = {"Accept": "application/dns-json"}
    _logger.info("DoH request: %s", url)
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=_doh_timeout) as response:
            _logger.info("Resolver(%s) response: %s", resolver, response.status)
            if response.status != 200:
                return None
            response_body = response.read().decode("utf-8")
            _logger.debug("<==  body: %s", response_body)
            answer = json.loads(response_body)["Answer"]
            return answer[0]["data"]
    except Exception as e:
        _logger.error("Resolver(%s) request error: %s", resolver, e)
        return None


@dataclass(init=False)
class DohArgs(Args):
    """Arguments for the doh function."""

    hosts: List[str]

    def parse(self, rawargs: dict, _) -> "DohArgs":
        self.hosts = rawargs.get("hosts", [])
        if "host" in rawargs:
            self.hosts.append(rawargs["host"])
        return self


@Func("doh", DohArgs)
def doh(args: DohArgs, _) -> None:
    """Put the given hosts into the registered hosts set."""
    _registered_hosts.update(args.hosts)
