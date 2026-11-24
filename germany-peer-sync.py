#!/usr/bin/env python3
"""Apply a leased set of public peers to the Germany VPN interface."""

import base64
import fcntl
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


INTERFACE = "awg-de"
CONFIG = Path("/etc/amnezia/amneziawg/awg-de.conf")
LEASE = Path("/run/most-germany.lease")
NETWORK = ipaddress.IPv4Network("10.8.0.0/22")
OBFS = {"Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
        "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5"}


def command(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, timeout=10).stdout.strip()


def parse_peers(payload):
    peers = json.loads(payload)
    if not isinstance(peers, dict) or len(peers) > NETWORK.num_addresses - 3:
        raise ValueError("Invalid peer list")
    rows = []
    addresses = set()
    for public_key, value in peers.items():
        raw = base64.b64decode(public_key, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode() != public_key:
            raise ValueError("Invalid public key")
        address = ipaddress.IPv4Address(value)
        if (address not in NETWORK or address in addresses
                or address in {NETWORK.network_address, NETWORK.broadcast_address, NETWORK.network_address + 1}):
            raise ValueError("Invalid client address")
        addresses.add(address)
        rows.append(f"[Peer]\nPublicKey = {public_key}\nAllowedIPs = {address}/32\n")
    return rows


def apply(peers):
    base = command("awg-quick", "strip", INTERFACE)
    if "[Peer]" in base:
        raise ValueError("The Germany interface configuration must contain only [Interface]")
    with tempfile.NamedTemporaryFile(mode="w") as config:
        config.write(base + "\n\n" + "\n".join(peers))
        config.flush()
        command("awg", "syncconf", INTERFACE, config.name)


def describe():
    params = {}
    for line in CONFIG.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in OBFS | {"ListenPort"}:
            params[key.strip()] = value.strip()
    params["PublicKey"] = command("awg", "show", INTERFACE, "public-key")
    return params


def main():
    expire = sys.argv[1:] == ["expire"]
    if len(sys.argv) > 1 and not expire:
        raise ValueError("Invalid arguments")
    if not expire:
        payload = sys.stdin.read(131073)
        if payload.strip() == "status":
            print(json.dumps(describe()))
            return
        if len(payload) > 131072:
            raise ValueError("Peer list too large")
        peers = parse_peers(payload)

    with open("/run/lock/most-germany.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if expire:
            if LEASE.exists() and time.monotonic() - float(LEASE.read_text()) < 75:
                return
            apply([])
            LEASE.unlink(missing_ok=True)
            return

        apply(peers)
        LEASE.write_text(str(time.monotonic()))
        print(json.dumps(describe()))


if __name__ == "__main__":
    main()
