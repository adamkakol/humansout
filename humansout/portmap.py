"""Ask the home router to forward one port to this node, the way torrent clients do: no settings to change.

Two standard protocols, tried in turn, with no dependencies:
- NAT-PMP (RFC 6886): one UDP request to the default gateway, port 5351;
- UPnP IGD: find the router with an SSDP search on the local network, read its description, and call
  AddPortMapping on its WANIPConnection (or WANPPPConnection) service.

Only the one port is forwarded, only to this node, and only for a lease that is renewed while the node runs;
the node answers nothing but the HumansOut protocol on it (signed public data and files). A router that
refuses, or a carrier-grade NAT that gives no public address, leaves the node as it was: it still takes part
through outgoing connections.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

LAN_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                                                   "fc00::/7", "::1/128")]
LEASE_S = 3600
DESCRIPTION = "HumansOut node"
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")


def default_gateway() -> str | None:
    """The router's address: Linux reads the routing table, macOS asks `route`."""
    route = Path("/proc/net/route")
    if route.exists():
        for line in route.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"gateway:\s*([0-9.]+)", out)
        return m.group(1) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def local_address(towards: str) -> str:
    """This machine's address on the network that reaches `towards` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, 9))
        return s.getsockname()[0]


def public_ip(address: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    return ip.is_global  # excludes private, carrier-grade NAT (100.64/10), loopback and the rest


# ---- NAT-PMP -------------------------------------------------------------------------------------------
def natpmp(gateway: str, internal_port: int, external_port: int, lease: int = LEASE_S, port: int = 5351,
           timeout: float = 2.0) -> dict | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(struct.pack("!BB", 0, 0), (gateway, port))  # external address
            data, _ = s.recvfrom(64)
            if len(data) < 12 or data[1] != 128 or struct.unpack("!H", data[2:4])[0] != 0:
                return None
            external_ip = socket.inet_ntoa(data[8:12])
            s.sendto(struct.pack("!BBHHHI", 0, 2, 0, internal_port, external_port, lease), (gateway, port))  # map TCP
            data, _ = s.recvfrom(64)
        except (socket.timeout, OSError):
            return None
    if len(data) < 16 or data[1] != 130 or struct.unpack("!H", data[2:4])[0] != 0:
        return None
    _, mapped_internal, mapped_external, granted = struct.unpack("!IHHI", data[4:16])
    return {"protocol": "nat-pmp", "external_ip": external_ip, "external_port": mapped_external,
            "internal_port": mapped_internal, "lease_s": granted}


# ---- UPnP IGD ------------------------------------------------------------------------------------------
def ssdp_search(timeout: float = 2.0, target: tuple[str, int] = ("239.255.255.250", 1900)) -> list[str]:
    """Description URLs of Internet gateway devices on this network."""
    request = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
               "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            s.sendto(request, target)
            while True:
                data, _ = s.recvfrom(4096)
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m and m.group(1).decode() not in found:
                    found.append(m.group(1).decode())
        except (socket.timeout, OSError):
            pass
    return found


def _on_this_network(url: str) -> bool:
    """A router's own address: plain http to a private (local network) IP. Anyone on the network can answer an
    SSDP search, so a LOCATION or control URL pointing anywhere else (the internet, a file:, a name) is ignored."""
    parts = urllib.parse.urlsplit(url)
    try:
        ip = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return False
    # home and office networks only: not link-local (where cloud metadata services answer), not documentation
    # or other special ranges that Python also calls "private"
    return parts.scheme == "http" and any(ip in net for net in LAN_NETWORKS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a router does not redirect: whoever does is not one


_LAN = urllib.request.OpenerDirector()
for _h in (urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
           urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler()):
    _LAN.add_handler(_h)


def _xml(data: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:  # no entity expansion from whoever answered
        raise ValueError("refusing XML with a DOCTYPE")
    return ElementTree.fromstring(data)


def _control_url(description_url: str) -> tuple[str, str] | None:
    if not _on_this_network(description_url):
        return None
    with _LAN.open(description_url, timeout=5) as resp:
        tree = _xml(resp.read(1 << 20))
    for service in tree.iter():
        if not service.tag.endswith("service"):
            continue
        kind = next((c.text for c in service if c.tag.endswith("serviceType")), None)
        control = next((c.text for c in service if c.tag.endswith("controlURL")), None)
        if kind in SERVICES and control:
            url = urllib.parse.urljoin(description_url, control)
            if urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(description_url).hostname:
                return None  # the control point must be the device that described itself
            return url, kind
    return None


def _soap(control: str, service: str, action: str, args: dict) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(control, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    with _LAN.open(req, timeout=5) as resp:
        return resp.read(1 << 20).decode("utf-8", "replace")


def upnp(internal_port: int, external_port: int, lease: int = LEASE_S, descriptions: list[str] | None = None) -> dict | None:
    for description in descriptions if descriptions is not None else ssdp_search():
        try:
            found = _control_url(description)
            if not found:
                continue
            control, service = found
            host = urllib.parse.urlparse(control).hostname or ""
            _soap(control, service, "AddPortMapping", {
                "NewRemoteHost": "", "NewExternalPort": external_port, "NewProtocol": "TCP",
                "NewInternalPort": internal_port, "NewInternalClient": local_address(host), "NewEnabled": 1,
                "NewPortMappingDescription": DESCRIPTION, "NewLeaseDuration": lease})
            answer = _soap(control, service, "GetExternalIPAddress", {})
            m = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", answer)
            return {"protocol": "upnp", "external_ip": m.group(1) if m else None, "external_port": external_port,
                    "internal_port": internal_port, "lease_s": lease, "control": control}
        except (OSError, ElementTree.ParseError, ValueError):
            continue
    return None


def map_port(internal_port: int, external_port: int | None = None, lease: int = LEASE_S) -> dict:
    """Try NAT-PMP, then UPnP. Returns the mapping and the public address to announce, or why there is none."""
    external_port = external_port or internal_port
    gateway = default_gateway()
    tried = []
    for attempt in (lambda: natpmp(gateway, internal_port, external_port, lease) if gateway else None,
                    lambda: upnp(internal_port, external_port, lease)):
        result = attempt()
        tried.append(result is not None)
        if result is None:
            continue
        if not public_ip(result.get("external_ip")):
            return {"mapped": False, "why": f"the router's outside address {result.get('external_ip')} is not public "
                                            "(carrier-grade NAT): the node keeps taking part through outgoing connections",
                    **result}
        return {"mapped": True, "public_url": f"http://{result['external_ip']}:{result['external_port']}", **result}
    return {"mapped": False, "gateway": gateway,
            "why": "the router answers neither NAT-PMP nor UPnP (or has them switched off): the node keeps taking part "
                   "through outgoing connections"}
