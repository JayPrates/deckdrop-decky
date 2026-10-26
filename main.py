# DeckDrop — Decky Loader backend.
#
# Receives files from the DeckDrop app on a Windows PC over the local network. Nothing is written until the
# transfer is accepted on the Deck; accepted files go to ~/Documents/DeckDrop.
#   - discovery: small JSON "hello" datagrams broadcast on UDP 47401 (the same scheme ClipLink uses);
#   - transfer: one TCP connection to port 47400 per batch of files:
#       PC   -> {"type": "offer", "files": [{"path": "Photos/a.jpg", "size": 123}, ...], ...}\n
#       Deck <- {"ok": true, "accepted": true, "folder": "..."}\n  once accepted, or {"ok": false, "error": "..."}\n
#       PC   -> the files' bytes back to back, in offer order
#       Deck <- {"ok": true, "done": true, "saved": n}\n
#     While the bytes arrive the Deck may send {"ok": false, "error": "..."}\n (e.g. cancelled); the PC checks for it.
#   - parallel transfer (v2): an offer with "streams": n gets "streams" and a "token" back with the acceptance. The PC
#     then opens n-1 more connections, each starting with {"type": "data", "token": ...}\n, and every connection
#     (the first one included) carries blocks: a 16-byte little-endian header (file index u32, offset u64, length u32)
#     and that many bytes, in any order across connections, ending with a header whose index is 0xFFFFFFFF. Empty
#     files are never sent; the Deck creates them. Several connections fill Wi-Fi better than one.

import asyncio
import collections
import json
import os
import random
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time

import decky

APP = "deckdrop"
PROTOCOL = 2
TCP_PORT = int(os.environ.get("DECKDROP_TCP_PORT", "47400"))       # overridable for tests on one machine
UDP_PORT = int(os.environ.get("DECKDROP_UDP_PORT", "47401"))
BIND = os.environ.get("DECKDROP_BIND", "0.0.0.0")                 # 127.0.0.1 for tests
DISCOVERY_PORT = int(os.environ.get("DECKDROP_DISCOVERY_PORT", "47401"))   # where hellos go; peers answer the sender
FOLDER_NAME = "DeckDrop"
HELLO_EVERY = 10                 # seconds between discovery broadcasts
PEER_TIMEOUT = 35                # a device that hasn't said hello for this long is gone
APPROVAL_TIMEOUT = 120           # an offer nobody answers is declined after this long
IDLE_TIMEOUT = 30                # a transfer that receives nothing for this long has failed
MAX_OFFER = 8 * 1024 * 1024      # longest offer line (the file list)
MAX_FILES = 20000
CHUNK = 1024 * 1024
MAX_STREAMS = 4                  # parallel connections per transfer
MAX_BLOCK = 8 * 1024 * 1024
BLOCK = struct.Struct("<IQI")    # file index, offset, length
END_OF_STREAM = 0xFFFFFFFF
SPARE_SPACE = 64 * 1024 * 1024   # left free on the disk after a transfer
PART = ".deckdrop-part"          # suffix of files still arriving
HISTORY = 20
DEFAULTS = {"id": "", "enabled": True, "notify": True, "folder": ""}     # folder "" = ~/Documents/DeckDrop


class Cancelled(Exception):
    pass


class BadData(Exception):
    """The PC sent blocks that don't fit the files it offered."""


def write_at(f, data, offset):
    """Writes data at offset in an incoming file (several connections fill one file at once)."""
    if hasattr(os, "pwrite"):
        fd = f["fh"].fileno()
        view = memoryview(data)
        while view:
            n = os.pwrite(fd, view, offset)
            view, offset = view[n:], offset + n
    else:                                    # Windows (tests): no pwrite
        with f["lock"]:
            f["fh"].seek(offset)
            f["fh"].write(data)


def system_env():
    # Decky's backend is a PyInstaller bundle; give system tools their own libraries back.
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=system_env())
    except (OSError, subprocess.SubprocessError) as e:
        decky.logger.info("%s failed: %s", cmd[0], e)
        return None


def device_name():
    host = socket.gethostname() or "Linux"
    return "Steam Deck" if host.lower() == "steamdeck" else host


def format_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def steam_uid():
    """uid of the (non-root) user running Steam, i.e. the person using this machine, or None."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open("/proc/%s/comm" % pid) as f:
                if f.read().strip() not in ("steam", "steamwebhelper"):
                    continue
            uid = os.stat("/proc/" + pid).st_uid
        except OSError:
            continue
        if uid != 0:
            return uid
    return None


def find_user():
    """(home, uid, gid, how) of the person files are for.

    Decky may run the backend as root, and outside SteamOS its idea of "the user" (DECKY_USER / DECKY_USER_HOME) can
    be root or a "deck" account that doesn't exist, so the owner of the Steam process wins. uid/gid are None when
    unknown. how says where the answer came from; "fallback" ones are looked at again later (Steam may not be up yet).
    """
    try:
        import pwd
    except ImportError:                     # not Linux (tests)
        return getattr(decky, "DECKY_USER_HOME", "") or os.path.expanduser("~"), None, None, "decky"
    candidates = []
    if os.geteuid() != 0:
        candidates.append((os.geteuid(), "backend user"))
    uid = steam_uid()
    if uid is not None:
        candidates.append((uid, "Steam's user"))
    try:
        candidates.append((pwd.getpwnam(getattr(decky, "DECKY_USER", "") or "deck").pw_uid, "fallback"))
    except KeyError:
        pass
    for uid, how in candidates:
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            continue
        if uid != 0 and pw.pw_dir and pw.pw_dir != "/" and os.path.isdir(pw.pw_dir):
            return pw.pw_dir, pw.pw_uid, pw.pw_gid, how
    home = getattr(decky, "DECKY_USER_HOME", "") or os.path.expanduser("~")
    return home, None, None, "fallback"


def documents_dir(home):
    """The desktop's Documents folder (localized names like ~/Documentos come from user-dirs.dirs), else ~/Documents."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", home)
                    # Set to $HOME itself means "no Documents folder".
                    if os.path.isabs(value) and os.path.normpath(value) != os.path.normpath(home):
                        return os.path.normpath(value)
    except (OSError, UnicodeDecodeError):
        pass
    return os.path.join(home, "Documents")


# ------------------------------------------------------------------ network helpers

def interface_addresses():
    """(local IPv4 addresses, broadcast addresses) of the interfaces that are up."""
    ips, broadcasts = [], ["255.255.255.255"]
    r = run(["ip", "-o", "-4", "addr", "show", "up"]) if os.name == "posix" else None
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[1] == "lo":
                continue
            if "inet" in parts:
                ips.append(parts[parts.index("inet") + 1].split("/")[0])
            if "brd" in parts:
                broadcasts.append(parts[parts.index("brd") + 1])
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))            # no packet is sent; this just picks the outgoing interface
                ips.append(s.getsockname()[0])
        except OSError:
            pass
    return ips, list(dict.fromkeys(broadcasts))


# ------------------------------------------------------------------ offers

def parse_offer(msg):
    """[(path parts, size)] from an offer, or raises ValueError with the reason shown to the PC."""
    files = msg.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("no files")
    if len(files) > MAX_FILES:
        raise ValueError("too many files (the limit is %d)" % MAX_FILES)
    out, seen = [], set()
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("bad file list")
        path, size = f.get("path"), f.get("size")
        if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("bad file list")
        parts = path.replace("\\", "/").split("/")
        if len(parts) > 64 or any(p in ("", ".", "..") or "\0" in p or len(p.encode("utf-8")) > 255 for p in parts):
            raise ValueError("bad file name: %s" % path[:200])
        key = "/".join(parts)
        if key in seen:
            raise ValueError("the same file is listed twice: %s" % key[:200])
        seen.add(key)
        out.append((parts, size))
    return out


def free_name(folder, name, is_dir, taken):
    """name, or "name (1).ext", "name (2).ext"… so nothing already in the folder is overwritten."""
    base, ext = (name, "") if is_dir else os.path.splitext(name)
    if base == "":                       # ".bashrc" style names
        base, ext = name, ""
    candidate, n = name, 1
    while candidate in taken or os.path.lexists(os.path.join(folder, candidate)) or \
            os.path.lexists(os.path.join(folder, candidate + PART)):
        candidate = "%s (%d)%s" % (base, n, ext)
        n += 1
    taken.add(candidate)
    return candidate


def plan_names(folder, files):
    """Maps each offered top-level name to the name it gets in the folder (renamed if it already exists)."""
    dirs = {parts[0] for parts, _ in files if len(parts) > 1}
    taken, names = set(), {}
    for parts, _ in files:
        top = parts[0]
        if top not in names:
            names[top] = free_name(folder, top, top in dirs, taken)
    return names


# ------------------------------------------------------------------ Plugin

class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, plugin):
        self.plugin = plugin

    def datagram_received(self, data, addr):
        self.plugin._on_datagram(data, addr)

    def error_received(self, exc):
        pass


class Plugin:
    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        self.settings = dict(DEFAULTS)
        try:
            with open(self.settings_path) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not self.settings["id"]:
            self.settings["id"] = "%012x" % random.getrandbits(48)
            self._save_settings()
        self.name = device_name()
        self.user, self.user_checked = None, 0.0
        self.peers = {}
        self.offers = {}             # waiting for an answer: id -> offer
        self.transfers = {}          # accepted and arriving: id -> transfer
        self.stream_slots = {}       # parallel transfer token -> queue of its extra connections
        self.history = collections.deque(maxlen=HISTORY)
        self.next_id = 1
        self.status, self.status_error = "", False
        self.ips, self.broadcasts = interface_addresses()
        self.udp, self.server, self.tasks = None, None, []
        if self.settings["enabled"]:
            await self._start()
        decky.logger.info("DeckDrop backend started as %s (%s), running as uid %s; Decky says the user is %s (home %s)%s",
                          self.name, ", ".join(self.ips) or "no network", os.geteuid() if hasattr(os, "geteuid") else "?",
                          getattr(decky, "DECKY_USER", "?"), getattr(decky, "DECKY_USER_HOME", "?"),
                          "" if self.settings["enabled"] else "; receiving is off")
        decky.logger.info("saving to %s", self.folder)

    async def _unload(self):
        if getattr(self, "loop", None) is not None:
            await self._stop()

    async def _uninstall(self):
        pass

    # ---------- on / off

    async def _start(self):
        """Listens on the network."""
        self._set_status("")
        try:
            self.server = await asyncio.start_server(self._handle_tcp, BIND, TCP_PORT, limit=MAX_OFFER)
        except OSError as e:
            self._set_status("Can't listen on port %d: %s" % (TCP_PORT, e.strerror or e), True)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((BIND, UDP_PORT))
            self.udp, _ = await self.loop.create_datagram_endpoint(lambda: _Discovery(self), sock=sock)
        except OSError as e:
            decky.logger.warning("discovery unavailable: %s", e)
        self.tasks = [self.loop.create_task(self._discovery_loop())]

    async def _stop(self):
        """Says goodbye, declines waiting offers, stops transfers and closes the ports."""
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        for offer in list(self.offers.values()):
            if not offer["future"].done():
                offer["future"].set_result(False)
        for t in self.transfers.values():
            t["cancel"] = True
        if self.udp is not None:
            self._hello("bye")
            self.udp.close()
            self.udp = None
        if self.server is not None:
            self.server.close()
            self.server = None
        self.peers.clear()
        self._set_status("")

    # ---------- state

    def _save_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, "w") as f:
                json.dump(self.settings, f)
        except OSError as e:
            decky.logger.warning("couldn't save settings: %s", e)

    def _user(self):
        """(home, uid, gid, how) of the person files are for; re-checked every few seconds until it's certain."""
        if self.user is None or (self.user[3] == "fallback" and time.monotonic() - self.user_checked > 5):
            found = find_user()
            if found != self.user:
                decky.logger.info("files are for uid %s, home %s (from %s)", found[1], found[0], found[3])
            self.user, self.user_checked = found, time.monotonic()
        return self.user

    def _default_folder(self):
        """DeckDrop inside the user's Documents folder."""
        if os.environ.get("DECKDROP_FOLDER"):
            return os.environ["DECKDROP_FOLDER"]
        return os.path.join(documents_dir(self._user()[0]), FOLDER_NAME)

    @property
    def folder(self):
        """Where accepted files are saved: the folder chosen in the panel, or Documents/DeckDrop."""
        return self.settings["folder"] or self._default_folder()

    def _set_status(self, text, error=False):
        self.status, self.status_error = text, error
        if error:
            decky.logger.warning(text)

    def _new_id(self):
        self.next_id += 1
        return self.next_id - 1

    def _expire(self):
        now = time.monotonic()
        for pid in [p for p, v in self.peers.items() if now - v["seen"] > PEER_TIMEOUT]:
            del self.peers[pid]

    def _learn(self, msg, ip):
        pid = str(msg.get("id") or ip)[:64]
        known = pid in self.peers
        self.peers[pid] = {"id": pid, "name": str(msg.get("name") or ip)[:64], "kind": str(msg.get("kind") or "")[:16],
                           "ip": ip, "seen": time.monotonic()}
        return self.peers[pid], known

    def _chown(self, path):
        """Gives what a root backend creates to the user, so they can move and delete it."""
        _, uid, gid, _ = self._user()
        if uid is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass

    def _makedirs(self, path):
        """Creates path and any missing parents, owned by the Deck's user."""
        missing = []
        p = path
        while p and not os.path.isdir(p):
            missing.append(p)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        os.makedirs(path, exist_ok=True)
        for d in missing:
            self._chown(d)

    async def _emit(self, event, data):
        try:
            await decky.emit(event, data)
        except Exception as e:
            decky.logger.info("emit %s failed: %s", event, e)

    def _record(self, offer, status, message, saved=0, received=0):
        self.history.append({"id": offer["id"], "from": offer["from"], "count": offer["count"], "size": offer["size"],
                             "saved": saved, "received": received, "status": status, "message": message,
                             "names": offer["names"], "time": time.time()})

    # ---------- discovery

    def _hello_message(self, kind, reply):
        return json.dumps({"app": APP, "v": PROTOCOL, "type": kind, "id": self.settings["id"], "name": self.name,
                           "kind": "deck", "port": TCP_PORT, "reply": reply}).encode("utf-8")

    def _hello(self, kind="hello", reply=False, to=None):
        if self.udp is None:
            return
        data = self._hello_message(kind, reply)
        targets = [to] if to is not None else [(b, DISCOVERY_PORT) for b in self.broadcasts]
        for t in targets:
            try:
                self.udp.sendto(data, t)
            except (OSError, ValueError) as e:
                decky.logger.debug("hello to %s: %s", t, e)

    def _on_datagram(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict) or msg.get("app") != APP or msg.get("id") == self.settings["id"]:
            return
        if msg.get("type") == "bye":
            self.peers.pop(str(msg.get("id")), None)
            return
        if msg.get("type") != "hello":
            return
        _, known = self._learn(msg, addr[0])
        if msg.get("reply") or not known:
            self._hello(reply=False, to=addr)

    async def _discovery_loop(self):
        first = True
        while True:
            self.ips, self.broadcasts = await self.loop.run_in_executor(None, interface_addresses)
            self._hello(reply=first)
            first = False
            self._expire()
            await asyncio.sleep(HELLO_EVERY)

    # ---------- receiving

    async def _handle_tcp(self, reader, writer):
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 15)
                msg = json.loads(line.decode("utf-8"))
            except asyncio.TimeoutError:
                return
            except (ValueError, UnicodeDecodeError):       # also a line over MAX_OFFER
                await self._reply(writer, {"ok": False, "error": "bad request"})
                return
            if not isinstance(msg, dict) or msg.get("app") != APP:
                await self._reply(writer, {"ok": False, "error": "not a DeckDrop request"})
            elif msg.get("type") == "ping":
                self._learn(msg, ip)
                await self._reply(writer, {"ok": True, "id": self.settings["id"], "name": self.name, "kind": "deck"})
            elif msg.get("type") == "offer":
                await self._receive(msg, ip, reader, writer)
            elif msg.get("type") == "data":
                # An extra connection of a parallel transfer: hand it over, keep it open until the transfer is over.
                queue = self.stream_slots.get(str(msg.get("token") or ""))
                if queue is not None:
                    finished = self.loop.create_future()
                    await queue.put((reader, writer, finished))
                    await finished
            else:
                await self._reply(writer, {"ok": False, "error": "unknown request"})
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _reply(self, writer, obj):
        writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _receive(self, msg, ip, reader, writer):
        try:
            files = parse_offer(msg)
        except ValueError as e:
            await self._reply(writer, {"ok": False, "error": str(e)})
            return
        peer, _ = self._learn(msg, ip)
        total = sum(size for _, size in files)
        offer = {"id": self._new_id(), "from": peer["name"], "ip": ip, "count": len(files), "size": total,
                 "names": list(dict.fromkeys(parts[0] for parts, _ in files))[:5],
                 "folders": len({parts[0] for parts, _ in files if len(parts) > 1}),
                 "time": time.time(), "future": self.loop.create_future(),
                 "folder": self.folder}         # fixed for this transfer even if the setting changes meanwhile
        folder = offer["folder"]

        # Refuse right away what can't be saved anyway, instead of asking first.
        try:
            await self.loop.run_in_executor(None, self._makedirs, folder)
            free = (await self.loop.run_in_executor(None, shutil.disk_usage, folder)).free
        except OSError as e:
            await self._reply(writer, {"ok": False, "error": "the Deck can't write to %s (%s)" % (folder, e.strerror or e)})
            self._record(offer, "failed", "Can't write to %s" % folder)
            return
        if total + SPARE_SPACE > free:
            await self._reply(writer, {"ok": False, "error": "not enough free space on the Deck (%s needed, %s free)"
                                       % (format_size(total), format_size(max(0, free - SPARE_SPACE)))})
            self._record(offer, "failed", "Not enough free space")
            return

        decky.logger.info("offer %d from %s (%s): %d files, %d bytes", offer["id"], peer["name"], ip, len(files), total)
        self.offers[offer["id"]] = offer
        await self._emit("deckdrop_offer", dict(self._offer_view(offer), notify=self.settings["notify"]))
        # The PC sends nothing until it's accepted, so reading anything here means it hung up.
        hangup = self.loop.create_task(reader.read(1))
        try:
            done, _ = await asyncio.wait({offer["future"], hangup}, timeout=APPROVAL_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.offers.pop(offer["id"], None)
            hangup.cancel()
        accepted = offer["future"].done() and offer["future"].result()
        if not offer["future"].done():
            offer["future"].cancel()
            await self._emit("deckdrop_closed", {"id": offer["id"]})
            if hangup in done:
                decky.logger.info("offer %d withdrawn by the PC", offer["id"])
                self._record(offer, "cancelled", "Cancelled on the PC")
                return
            decky.logger.info("offer %d expired", offer["id"])
            self._record(offer, "expired", "Nobody answered")
            await self._reply(writer, {"ok": False, "error": "nobody answered on the Deck"})
            return
        if not accepted:
            decky.logger.info("offer %d declined", offer["id"])
            self._record(offer, "declined", "Declined")
            await self._reply(writer, {"ok": False, "error": "declined"})
            return

        names = plan_names(folder, files)
        t = {"id": offer["id"], "from": offer["from"], "count": offer["count"], "size": offer["size"],
             "received": 0, "index": 0, "current": "", "cancel": False, "started": time.monotonic(),
             "saved": 0, "paths": [], "streams": 1}
        requested = msg.get("streams")
        if isinstance(requested, int) and not isinstance(requested, bool) and requested > 1:
            t["streams"] = min(requested, MAX_STREAMS)
            token = secrets.token_hex(16)
            self.stream_slots[token] = asyncio.Queue()
            await self._reply(writer, {"ok": True, "accepted": True, "folder": folder, "streams": t["streams"], "token": token})
            work = self._save_parallel(t, offer, files, names, reader, token)
        else:                                    # an older PC app: one connection, files back to back
            await self._reply(writer, {"ok": True, "accepted": True, "folder": folder})
            work = self._save_sequential(t, offer, files, names, reader)
        await self._run_transfer(t, offer, files, work, reader, writer)

    # ---------- saving

    def _incoming(self, offer, names, files, incoming, idx):
        """State of one file being received: where it goes, how much has arrived."""
        parts, size = files[idx]
        rel = [names[parts[0]]] + parts[1:]
        target = os.path.join(offer["folder"], *rel)
        f = {"rel": "/".join(rel), "target": target, "part": target + PART, "size": size, "received": 0,
             "fh": None, "ready": None, "closing": False, "saved": False, "lock": threading.Lock()}
        incoming[idx] = f
        return f

    def _open_part(self, f):
        self._makedirs(os.path.dirname(f["target"]))
        f["fh"] = open(f["part"], "wb", buffering=0)

    def _rename_part(self, f):
        """name.deckdrop-part -> name, without overwriting anything that appeared meanwhile."""
        target = f["target"]
        if os.path.lexists(target):
            d, base = os.path.split(target)
            target = os.path.join(d, free_name(d, base, False, set()))
        os.rename(f["part"], target)
        self._chown(target)
        f["target"], f["saved"] = target, True

    def _write_whole(self, f, data):
        """A file that arrived in one piece (small files): created, written and renamed in one go."""
        self._makedirs(os.path.dirname(f["target"]))
        with open(f["part"], "wb") as fh:
            fh.write(data)
        self._rename_part(f)

    def _close_and_rename(self, f):
        fh, f["fh"] = f["fh"], None
        fh.close()
        self._rename_part(f)

    def _discard(self, f):
        """Removes what arrived of a file that won't be completed."""
        fh, f["fh"] = f["fh"], None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        if not f["saved"]:
            try:
                os.remove(f["part"])
            except OSError:
                pass

    @staticmethod
    def _file_saved(t, f):
        t["saved"] += 1
        if len(t["paths"]) < 5:
            t["paths"].append(f["target"])

    async def _save_sequential(self, t, offer, files, names, reader):
        incoming = {}
        run_io = lambda fn, *a: self.loop.run_in_executor(None, fn, *a)
        for i in range(len(files)):
            f = self._incoming(offer, names, files, incoming, i)
            t["index"], t["current"] = i, f["rel"]
            await run_io(self._open_part, f)
            try:
                remaining = f["size"]
                while remaining:
                    buf = bytearray()
                    while remaining and len(buf) < CHUNK:
                        if t["cancel"]:
                            raise Cancelled()
                        data = await asyncio.wait_for(reader.read(min(remaining, CHUNK - len(buf))), IDLE_TIMEOUT)
                        if not data:
                            raise EOFError()          # the PC closed the connection: cancelled there
                        buf += data
                        remaining -= len(data)
                        t["received"] += len(data)
                    await run_io(f["fh"].write, buf)
                await run_io(self._close_and_rename, f)
            except BaseException:
                await run_io(self._discard, f)
                raise
            self._file_saved(t, f)

    async def _save_parallel(self, t, offer, files, names, reader, token):
        queue = self.stream_slots[token]
        incoming, extra, tasks = {}, [], []
        run_io = lambda fn, *a: self.loop.run_in_executor(None, fn, *a)
        try:
            for i, (_, size) in enumerate(files):          # empty files aren't sent
                if size == 0:
                    f = self._incoming(offer, names, files, incoming, i)
                    f["closing"] = True
                    await run_io(self._write_whole, f, b"")
                    self._file_saved(t, f)
            for _ in range(t["streams"] - 1):
                extra.append(await asyncio.wait_for(queue.get(), 15))
            readers = [reader] + [r for r, _, _ in extra]
            tasks = [self.loop.create_task(self._receive_blocks(t, offer, files, names, incoming, r)) for r in readers]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception() is not None:
                    raise task.exception()
            missing = sum(1 for f in incoming.values() if not f["saved"]) + len(files) - len(incoming)
            if missing:
                raise BadData("%d files didn't arrive completely" % missing)
        finally:
            self.stream_slots.pop(token, None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while not queue.empty():                       # connections that came too late
                extra.append(queue.get_nowait())
            for _, _, finished in extra:                   # lets their handlers close them
                if not finished.done():
                    finished.set_result(None)
            pending_io = [f["ready"] for f in incoming.values() if f["ready"] is not None]
            await asyncio.gather(*pending_io, return_exceptions=True)
            for f in incoming.values():
                if not f["saved"]:
                    await run_io(self._discard, f)

    async def _receive_blocks(self, t, offer, files, names, incoming, reader):
        """Reads blocks from one connection of a parallel transfer until its end marker."""
        run_io = lambda fn, *a: self.loop.run_in_executor(None, fn, *a)
        while True:
            if t["cancel"]:
                raise Cancelled()
            head = await asyncio.wait_for(reader.readexactly(BLOCK.size), IDLE_TIMEOUT)
            idx, offset, length = BLOCK.unpack(head)
            if idx == END_OF_STREAM:
                return
            if idx >= len(files) or length == 0 or length > MAX_BLOCK or offset + length > files[idx][1]:
                raise BadData("a block that doesn't fit the files offered")
            data = await asyncio.wait_for(reader.readexactly(length), IDLE_TIMEOUT)
            if t["cancel"]:
                raise Cancelled()
            t["received"] += length
            f = incoming.get(idx)
            if f is None:
                f = self._incoming(offer, names, files, incoming, idx)
                t["index"], t["current"] = max(t["index"], idx), f["rel"]
                if length == f["size"]:                    # the whole file in one block: one trip to the thread pool
                    f["closing"] = True
                    f["ready"] = run_io(self._write_whole, f, data)
                    await f["ready"]
                    self._file_saved(t, f)
                    continue
                f["ready"] = run_io(self._open_part, f)      # a future other blocks of this file wait on too
            if f["closing"]:
                raise BadData("more data for a file that was already complete")
            await f["ready"]
            await run_io(write_at, f, data, offset)
            f["received"] += length
            if f["received"] > f["size"]:
                raise BadData("more data than the file's size")
            if f["received"] == f["size"]:
                f["closing"] = True
                await run_io(self._close_and_rename, f)
                self._file_saved(t, f)

    async def _run_transfer(self, t, offer, files, work, reader, writer):
        """Runs an accepted transfer and reports how it ended (to the PC, the log, the list and a notification)."""
        self.transfers[t["id"]] = t
        status, message = "done", ""
        try:
            await work
            await self._reply(writer, {"ok": True, "done": True, "saved": t["saved"]})
            message = "Saved to " + offer["folder"]
        except Cancelled:
            status, message = "cancelled", "Cancelled on the Deck"
            try:
                await self._reply(writer, {"ok": False, "error": "cancelled on the Deck"})
                writer.write_eof()
                # Keep reading for a moment so the PC sees the message instead of a reset connection.
                end = time.monotonic() + 3
                while time.monotonic() < end and await asyncio.wait_for(reader.read(CHUNK), 3):
                    pass
            except (OSError, asyncio.TimeoutError):
                pass
        except EOFError:
            status, message = "cancelled", "Cancelled on the PC"
        except asyncio.TimeoutError:
            status, message = "failed", "The PC stopped sending"
        except BadData as e:
            status, message = "failed", "The PC sent something unexpected (%s)" % e
            try:
                await self._reply(writer, {"ok": False, "error": "the Deck stopped the transfer: %s" % e})
            except OSError:
                pass
        except ConnectionError:
            status, message = "failed", "The connection to the PC was lost"
        except OSError as e:
            status, message = "failed", "Couldn't save: %s" % (e.strerror or e)
            try:
                await self._reply(writer, {"ok": False, "error": "the Deck couldn't save the files (%s)" % (e.strerror or e)})
            except OSError:
                pass
        except Exception as e:                   # a bug: still report it instead of leaving the PC hanging
            decky.logger.exception("transfer %d failed", t["id"])
            status, message = "failed", "Something went wrong on the Deck (%s)" % e
            try:
                await self._reply(writer, {"ok": False, "error": "something went wrong on the Deck (%s)" % e})
            except OSError:
                pass
        finally:
            self.transfers.pop(t["id"], None)
        saved, paths = t["saved"], t["paths"]
        secs = time.monotonic() - t["started"]
        decky.logger.info("transfer %d: %s, %d of %d files saved, %s in %.1f s over %d connection(s) (%s)%s", t["id"], status,
                          saved, len(files), format_size(t["received"]), secs, t["streams"], message,
                          "".join("\n    " + p for p in paths) + ("\n    ..." if saved > len(paths) else ""))
        self._record(offer, status, message, saved, t["received"])
        await self._emit("deckdrop_done", {"id": t["id"], "from": t["from"], "status": status, "message": message,
                                           "saved": saved, "count": t["count"], "size": t["size"],
                                           "folder": offer["folder"], "notify": self.settings["notify"]})

    def _offer_view(self, offer):
        return {"id": offer["id"], "from": offer["from"], "count": offer["count"], "size": offer["size"],
                "names": offer["names"], "folders": offer["folders"], "time": offer["time"],
                "expires": offer["time"] + APPROVAL_TIMEOUT, "folder": offer["folder"]}

    # ---------- frontend API

    async def get_state(self):
        self._expire()
        now = time.monotonic()
        peers = sorted(self.peers.values(), key=lambda p: p["name"].lower())
        return {
            "me": {"name": self.name, "ips": self.ips, "port": TCP_PORT},
            "folder": self.folder,
            "default_folder": self._default_folder(),
            "peers": [{"id": p["id"], "name": p["name"], "kind": p["kind"], "ip": p["ip"]} for p in peers],
            "offers": [self._offer_view(o) for o in sorted(self.offers.values(), key=lambda o: o["id"])],
            "transfers": [{"id": t["id"], "from": t["from"], "count": t["count"], "size": t["size"],
                           "received": t["received"], "index": t["index"], "current": t["current"],
                           "elapsed": now - t["started"], "cancelling": t["cancel"]} for t in self.transfers.values()],
            "history": list(reversed(self.history)),
            "settings": {k: self.settings[k] for k in DEFAULTS if k != "id"},
            "listening": self.server is not None,
            "status": self.status, "error": self.status_error,
        }

    async def respond_offer(self, offer_id, accept):
        offer = self.offers.get(offer_id)
        if offer is None or offer["future"].done():
            return False
        offer["future"].set_result(bool(accept))
        return True

    async def cancel_transfer(self, transfer_id):
        t = self.transfers.get(transfer_id)
        if t is None:
            return False
        t["cancel"] = True
        return True

    async def clear_history(self):
        self.history.clear()

    async def log_info(self, text):
        decky.logger.info("frontend: %s", str(text)[:4000])

    async def set_folder(self, path):
        """Chooses where accepted files are saved; "" goes back to ~/Documents/DeckDrop."""
        path = str(path or "").strip()
        if path:
            path = os.path.normpath(os.path.expanduser(path))
            if not os.path.isabs(path):
                return {"ok": False, "message": "Choose a full folder path."}
            try:
                await self.loop.run_in_executor(None, self._makedirs, path)
            except OSError as e:
                return {"ok": False, "message": "Can't use %s: %s" % (path, e.strerror or e)}
            if not os.access(path, os.W_OK):
                return {"ok": False, "message": "DeckDrop isn't allowed to write to %s." % path}
            if path == self._default_folder():
                path = ""
        self.settings["folder"] = path
        self._save_settings()
        decky.logger.info("saving files to %s", self.folder)
        return {"ok": True, "message": "Files will be saved to %s." % self.folder}

    async def set_setting(self, key, value):
        if key not in DEFAULTS or key in ("id", "folder"):
            return
        value = bool(value)
        old = self.settings[key]
        self.settings[key] = value
        self._save_settings()
        if key == "enabled" and value != old:
            if value:
                await self._start()
            else:
                await self._stop()
            decky.logger.info("receiving turned %s", "on" if value else "off")

    async def refresh(self):
        if not self.settings["enabled"]:
            return
        self.ips, self.broadcasts = await self.loop.run_in_executor(None, interface_addresses)
        self._hello(reply=True)
