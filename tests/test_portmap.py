"""Automatic port mapping, tested against fake routers (no real router is touched)."""

import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from humansout import portmap


def udp_server(handler):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(5)

    def loop():
        try:
            while True:
                data, addr = s.recvfrom(4096)
                for reply in handler(data):
                    s.sendto(reply, addr)
        except OSError:
            pass
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1]


def test_nat_pmp_maps_a_port_and_reports_the_public_address():
    def router(data):
        if data[:2] == b"\x00\x00":  # external address
            yield struct.pack("!BBHI", 0, 128, 0, 1) + socket.inet_aton("81.2.3.4")
        elif data[1] == 2:  # map TCP
            _, _, _, internal, external, lease = struct.unpack("!BBHHHI", data[:12])
            yield struct.pack("!BBHIHHI", 0, 130, 0, 1, internal, external, lease)
    sock, port = udp_server(router)
    try:
        got = portmap.natpmp("127.0.0.1", 7400, 7400, lease=600, port=port)
    finally:
        sock.close()
    assert got == {"protocol": "nat-pmp", "external_ip": "81.2.3.4", "external_port": 7400, "internal_port": 7400,
                   "lease_s": 600}


def test_upnp_finds_the_gateway_and_adds_a_mapping():
    calls = []

    class Router(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList><service>'
                    '<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>'
                    '<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></root>').encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            calls.append((self.headers["SOAPAction"], body))
            answer = "<NewExternalIPAddress>81.2.3.4</NewExternalIPAddress>" if "GetExternalIPAddress" in body else ""
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer.encode())
    http = ThreadingHTTPServer(("127.0.0.1", 0), Router)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    location = f"http://127.0.0.1:{http.server_address[1]}/rootDesc.xml"
    ssdp, ssdp_port = udp_server(lambda data: [f"HTTP/1.1 200 OK\r\nLOCATION: {location}\r\n\r\n".encode()]
                                 if b"M-SEARCH" in data else [])
    try:
        assert portmap.ssdp_search(timeout=1, target=("127.0.0.1", ssdp_port)) == [location]
        got = portmap.upnp(7400, 7400, lease=900, descriptions=[location])
    finally:
        http.shutdown()
        ssdp.close()
    assert got["protocol"] == "upnp" and got["external_ip"] == "81.2.3.4" and got["external_port"] == 7400
    add = next(body for action, body in calls if "AddPortMapping" in action)
    assert "<NewInternalPort>7400</NewInternalPort>" in add and "<NewLeaseDuration>900</NewLeaseDuration>" in add
    assert "<NewInternalClient>127.0.0.1</NewInternalClient>" in add


def test_a_carrier_grade_nat_is_not_announced(monkeypatch):
    monkeypatch.setattr(portmap, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(portmap, "natpmp", lambda *a, **k: {"protocol": "nat-pmp", "external_ip": "100.64.7.9",
                                                            "external_port": 7400, "internal_port": 7400, "lease_s": 3600})
    got = portmap.map_port(7400)
    assert got["mapped"] is False and "carrier-grade" in got["why"]


def test_a_router_that_answers_nothing_changes_nothing(monkeypatch):
    monkeypatch.setattr(portmap, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(portmap, "natpmp", lambda *a, **k: None)
    monkeypatch.setattr(portmap, "upnp", lambda *a, **k: None)
    got = portmap.map_port(7400)
    assert got["mapped"] is False and "outgoing connections" in got["why"]


def test_public_addresses():
    assert portmap.public_ip("81.2.3.4") and not portmap.public_ip("192.168.1.10")
    assert not portmap.public_ip("100.64.0.1") and not portmap.public_ip("not an address")
