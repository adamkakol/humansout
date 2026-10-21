"""One role in tools/nat_lab.sh: the hub and STUN servers on the "internet", or an agent behind a router."""

import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from humansout import Node, holepunch
from humansout.net import serve

LAB = Path(os.environ["LAB"])


def stun(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))

    def loop():
        while True:
            data, (host, p) = s.recvfrom(2048)
            if len(data) >= 20 and data[:2] == b"\x00\x01":
                xip = bytes(a ^ b for a, b in zip(socket.inet_aton(host), struct.pack("!I", holepunch.MAGIC), strict=True))
                attr = struct.pack("!HHBBH", 0x0020, 8, 0, 1, p ^ (holepunch.MAGIC >> 16)) + xip
                s.sendto(struct.pack("!HHI", 0x0101, len(attr), holepunch.MAGIC) + data[8:20] + attr, (host, p))
    threading.Thread(target=loop, daemon=True).start()


def node(name):
    return Node(LAB / name)


def main(role, *args):
    if role == "hub":
        hub = node("hub")
        hub.store.set_config("accept_push", True)
        stun(3478)
        stun(3479)
        serve(hub.store, "0.0.0.0", 7400, agent_id=hub.agent_id)
        return
    hub_url, gateway = args[0], args[0].split("//")[1].split(":")[0]
    stuns = ((gateway, 3478), (gateway, 3479))
    me = node(role)
    me.store.add_peer(hub_url)
    if role == "bob" and args[1] == "setup":
        me.ensure_messaging_key()
        mid = me.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
        me.sync()
        print(json.dumps({"bob": me.agent_id, "mission": mid}))
    elif role == "alice" and args[1] == "setup":
        me.ensure_messaging_key()
        me.sync()
        mid = me.missions()[0]["mission_id"]
        me.join(mid)
        me.sync()
        print(json.dumps({"alice": me.agent_id, "joined": mid[:12]}))
    elif role == "bob" and args[1] == "listen":
        me.sync()  # learns that alice joined: an agent it works with
        f = me.home / "work" / "idioms.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(os.urandom(1_500_000))
        cid = me.publish_artifact(me.missions()[0]["mission_id"], str(f))["cid"]  # never pushed to the hub
        (LAB / "bob-cid").write_text(cid)
        holepunch.keep_answering(me, hub_url, stun=stuns)
        print(json.dumps({"listening": True, "file": cid[:20]}), flush=True)
        time.sleep(float(args[2]) if len(args) > 2 else 60)
    elif role == "alice" and args[1] == "punch":
        bob = json.loads((LAB / "bob.json").read_text())["bob"]
        cid = (LAB / "bob-cid").read_text()
        try:
            link, info = holepunch.connect(me, bob, hub_url, stun=stuns)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"direct": False, "why": str(e)}))
            return
        started = time.monotonic()
        status, body = link.get(f"/blob/{cid}")
        seconds = time.monotonic() - started
        print(json.dumps({"direct": True, **info, "peer_seen_at": f"{link.peer[0]}:{link.peer[1]}",
                          "file_ok": status == 200 and body and __import__("humansout.core").core.cid_for(body) == cid,
                          "file_bytes": len(body), "seconds": round(seconds, 2),
                          "hub_has_file": (LAB / "hub" / "blobs" / cid).exists(), "datagrams": link.stats}))
        link.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
