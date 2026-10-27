#!/usr/bin/env python3
"""
cowping.py - ping up to 20 hosts in parallel with a live terminal view.

Standard library only. Uses the operating system's own `ping` command, so no
root/admin rights are needed. Works on Linux, macOS/BSD and Windows.

Examples:
    python3 cowping.py 8.8.8.8 1.1.1.1 router.local
    python3 cowping.py -f hosts.csv -t 0.5
    python3 cowping.py "Core,core-sw01,10.0.0.1" "Core,core-sw02,10.0.0.2"

Input entries (arguments or file lines, '#' starts a comment):
    10.0.0.1                    plain host or IP
    core-sw01,10.0.0.1          name,ip
    Core,core-sw01,10.0.0.1     group,name,ip  -> shown under a "Core" header

Columns: Host = name if given, otherwise the IP. Lost = total lost pings,
Last = consecutive lost pings (resets on reply). Rows are green while the host answers and red when it does not.
History: '!' = reply, '.' = timeout, '?' = error (e.g. unknown host).
Press Ctrl+C to stop; a final summary is left on screen.

Copyright (C) 2026 Conscia
This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It comes WITHOUT ANY WARRANTY. See the LICENSE file or
<https://www.gnu.org/licenses/gpl-3.0.html>.
"""

from __future__ import print_function

import argparse
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time

MAX_TARGETS = 20
HISTORY_MAX = 1000  # results kept per host; the screen shows as many as fit
REFRESH = 0.25  # seconds between screen redraws

SYSTEM = platform.system().lower()
IS_WINDOWS = SYSTEM.startswith("win")
IS_BSD_STYLE = SYSTEM == "darwin" or "bsd" in SYSTEM  # -W is in milliseconds

now = getattr(time, "monotonic", time.time)

# Matches "time=12.3 ms", "time<1ms", "tid=12 ms" (localised Windows) etc.
RTT_RE = re.compile(r"[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms", re.IGNORECASE)
TimeoutExpired = getattr(subprocess, "TimeoutExpired", None)


# --------------------------------------------------------------------------- #
# Pinging
# --------------------------------------------------------------------------- #
def build_command(host, timeout):
    """Return the ping command for one echo request on this platform."""
    ipv6 = ":" in host
    ms = str(max(1, int(round(timeout * 1000))))

    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", ms]
        if ipv6:
            cmd.append("-6")
        return cmd + [host]

    if IS_BSD_STYLE:
        if ipv6:
            # macOS/BSD ping6 has no per-reply timeout flag; we enforce it ourselves.
            return ["ping6", "-n", "-c", "1", host]
        return ["ping", "-n", "-c", "1", "-W", ms, host]

    # Linux and others: older iputils only accept whole seconds for -W,
    # so round up and enforce the exact timeout on the measured RTT instead.
    cmd = ["ping", "-n", "-c", "1", "-W", str(max(1, int(math.ceil(timeout))))]
    if ipv6:
        cmd.append("-6")
    return cmd + [host]


def run_command(cmd, limit):
    """Run cmd, return (returncode, output). Kills it if it exceeds `limit`."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"  # predictable output on Unix
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        return None, "cannot run {0}: {1}".format(cmd[0], exc)

    try:
        out, _ = proc.communicate(timeout=limit)
    except TypeError:  # Python < 3.3: no timeout argument
        out, _ = proc.communicate()
    except Exception as exc:
        if TimeoutExpired is not None and isinstance(exc, TimeoutExpired):
            proc.kill()
            proc.communicate()
            return 1, ""
        raise
    return proc.returncode, out.decode("utf-8", "replace")


def ping_once(host, timeout):
    """
    Send one echo request.
    Returns ("ok", rtt_ms) | ("timeout", None) | ("error", message).
    """
    start = now()
    code, output = run_command(build_command(host, timeout), timeout + 3)
    elapsed_ms = (now() - start) * 1000.0

    if code is None:
        return "error", output

    if IS_WINDOWS:
        # Windows can return 0 for "destination unreachable"; TTL= only appears on a real reply.
        success = code == 0 and "ttl=" in output.lower()
    else:
        success = code == 0

    if not success:
        # On Unix, exit code 1 = no reply, anything else = error (bad host etc.)
        if not IS_WINDOWS and code not in (0, 1):
            line = output.strip().splitlines()[-1] if output.strip() else "ping failed"
            return "error", line.strip()
        lowered = output.lower()
        if IS_WINDOWS and ("could not find host" in lowered or "fant ikke" in lowered):
            return "error", "unknown host"
        return "timeout", None

    match = RTT_RE.search(output)
    rtt = float(match.group(1).replace(",", ".")) if match else elapsed_ms
    if rtt > timeout * 1000.0:
        return "timeout", None
    return "ok", rtt


# --------------------------------------------------------------------------- #
# Per-target state
# --------------------------------------------------------------------------- #
class Target(object):
    def __init__(self, host, name="", group=""):
        self.host = host            # what we ping (IP or hostname)
        self.name = name or host    # label shown on screen
        self.group = group
        self.lock = threading.Lock()
        self.sent = 0
        self.lost = 0
        self.streak = 0             # consecutive lost pings, reset on a reply
        self.status = "waiting"
        self.history = []

    def record(self, status, value):
        with self.lock:
            self.sent += 1
            self.status = status
            if status == "ok":
                self.streak = 0
                mark = "!"
            else:
                self.lost += 1
                self.streak += 1
                mark = "?" if status == "error" else "."
            self.history.append(mark)
            if len(self.history) > HISTORY_MAX:
                del self.history[0]

    def snapshot(self):
        with self.lock:
            return dict(lost=self.lost, streak=self.streak, status=self.status,
                        history="".join(self.history))


def worker(target, timeout, interval, count, stop):
    while not stop.is_set():
        started = now()
        status, value = ping_once(target.host, timeout)
        target.record(status, value)
        if count and target.sent >= count:
            break
        remaining = interval - (now() - started)
        if remaining > 0:
            stop.wait(remaining)


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #
UNICODE_BOX = {"tl": u"┌", "tr": u"┐", "bl": u"└", "br": u"┘",
               "h": u"─", "v": u"│", "lt": u"├", "rt": u"┤"}
ASCII_BOX = {"tl": "+", "tr": "+", "bl": "+", "br": "+",
             "h": "-", "v": "|", "lt": "+", "rt": "+"}
GREEN, RED, BOLD, CYAN, YELLOW = "32", "31", "1", "1;36", "33"
MIN_HISTORY = 10


def pick_box_chars():
    """Use box-drawing characters if the terminal encoding can show them."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        u"".join(UNICODE_BOX.values()).encode(encoding)
        return UNICODE_BOX
    except (UnicodeError, LookupError):
        return ASCII_BOX


def terminal_width():
    try:
        import shutil
        return shutil.get_terminal_size((80, 24)).columns
    except (ImportError, AttributeError):  # Python < 3.3
        try:
            return int(os.environ.get("COLUMNS", 80))
        except ValueError:
            return 80


class Painter(object):
    def __init__(self, use_color, banner=True):
        self.use_color = use_color
        self.banner = banner
        self.box = pick_box_chars()

    def color(self, text, code):
        if not self.use_color or not code:
            return text
        return "\033[{0}m{1}\033[0m".format(code, text)


def fit(text, width):
    """Left-justify text in width, truncating with '~' if too long."""
    if len(text) > width:
        return text[:width - 1] + "~"
    return text.ljust(width)


# --------------------------------------------------------------------------- #
# Banner: cow + seven-segment clock
# --------------------------------------------------------------------------- #
# The cow opens with PING_IS_LIFE, then says something new every SAYING_EVERY
# seconds, going through all of SAYINGS in random order before repeating.
PING_IS_LIFE = "Ping is life"
SAYINGS = [
    "It's always DNS",
    "No pong? Big wrong.",
    "Moo-ve along, packets",
    "Udder-ly reachable",
    "Latency is a state of mind",
    "ICMP: I Can Moo, Please?",
    "Echo request, echo regret",
    "TTL: Time To Lie down",
    "Every dot is a sad cow",
    "Ping early, ping often",
    "Round trip, no souvenirs",
    "Cows don't drop packets",
    "Firewalls are just fences",
    "Packets lost, cow found",
    "I herd you like packets",
    "Pong is the best reward",
    "It worked in the lab",
    "Did you try rebooting it?",
    "Ping me maybe",
    "Grass is green, so is ping",
]
SAYING_EVERY = 60
SAY_W = max(len(s) for s in [PING_IS_LIFE] + SAYINGS)

COW = [
    r"         \   ^__^",
    r"          \  (oo)\_______",
    r"             (__)\       )\/\ ",
    r"                 ||----w |",
    r"                 ||     ||",
]
# Wide enough for the longest saying, so the clock never moves.
COW_W = max([SAY_W + 4] + [len(l) for l in COW])

_saying = {"slot": 0, "text": PING_IS_LIFE, "deck": []}


def cow_saying(started):
    """What the cow says now: PING_IS_LIFE first, then a new saying every minute."""
    slot = int(now() - started) // SAYING_EVERY
    if slot != _saying["slot"]:
        deck = _saying["deck"]
        if not deck:
            deck[:] = random.sample(SAYINGS, len(SAYINGS))
            if deck[-1] == _saying["text"]:
                deck.insert(0, deck.pop())
        _saying["slot"] = slot
        _saying["text"] = deck.pop()
    return _saying["text"]


def cow_lines(text):
    """Speech bubble with text above the cow."""
    return [" " + "_" * (len(text) + 2),
            "< " + text + " >",
            " " + "-" * (len(text) + 2)] + COW

# Each glyph is three rows of equal width.
SEGMENT_FONT = {
    "0": [" _ ", "| |", "|_|"],
    "1": ["   ", "  |", "  |"],
    "2": [" _ ", " _|", "|_ "],
    "3": [" _ ", " _|", " _|"],
    "4": ["   ", "|_|", "  |"],
    "5": [" _ ", "|_ ", " _|"],
    "6": [" _ ", "|_ ", "|_|"],
    "7": [" _ ", "  |", "  |"],
    "8": [" _ ", "|_|", "|_|"],
    "9": [" _ ", "|_|", " _|"],
    ":": [" ", "o", "o"],
    ".": [" ", " ", "o"],
    "-": ["   ", " _ ", "   "],
    " ": ["  ", "  ", "  "],
}
CLOCK_FORMAT = "%H:%M:%S"
DATE_FORMAT = "%d.%m.%Y"


def big_text(text):
    """Render text in the seven-segment font; returns three lines."""
    glyphs = [SEGMENT_FONT.get(ch, SEGMENT_FONT[" "]) for ch in text]
    return [" ".join(g[row] for g in glyphs) for row in range(3)]


def banner_lines(inner, started):
    """
    Return a list of [(text, colour), ...] parts per line: the cow on the left,
    time and date on the right. Stacks them if the window is too narrow.
    """
    cow = cow_lines(cow_saying(started))
    clock = big_text(time.strftime(CLOCK_FORMAT))
    date = big_text(time.strftime(DATE_FORMAT))
    clock_w = max(len(l) for l in clock + date)
    clock_block = [l.ljust(clock_w) for l in clock] + [" " * clock_w] + \
                  [l.ljust(clock_w) for l in date]

    gap = 6
    if COW_W + gap + clock_w <= inner:
        # Side by side, clock centred in the space to the right of the cow.
        lpad = (inner - COW_W - clock_w) // 2 + COW_W // 4
        lpad = max(gap, min(lpad, inner - COW_W - clock_w))
        top = (len(cow) - len(clock_block)) // 2 + 1
        lines = []
        for i, cow_line in enumerate(cow):
            j = i - top
            right = clock_block[j] if 0 <= j < len(clock_block) else ""
            lines.append([(cow_line.ljust(COW_W), YELLOW),
                          (" " * lpad, None),
                          (right, CYAN)])
        return lines

    # Narrow window: cow, then clock underneath.
    return [[(l, YELLOW)] for l in cow] + [[(l, CYAN)] for l in clock_block]


def render(targets, painter, timeout, interval, started, final=False):
    box = painter.box
    grouped = any(t.group for t in targets)

    # Host column shows the name if one was given, otherwise the IP/host.
    name_w = max(4, min(30, max(len(t.name) for t in targets)))

    # Fixed columns: host, lost, last. History gets all the rest.
    fixed = name_w + 2 + 6 + 2 + 6 + 2
    # 4 = "| " + " |", 1 spare so we never write into the last terminal column.
    hist_w = max(MIN_HISTORY, terminal_width() - 4 - 1 - fixed)
    inner = fixed + hist_w

    def row(name, lost, streak, hist):
        return "  ".join([fit(name, name_w), "{0:>6}".format(lost),
                          "{0:>6}".format(streak), fit(hist, hist_w)])

    header = row("Host", "Lost", "Last", "History")

    elapsed = int(now() - started)
    title = "cowping | {0} targets | timeout {1:g}s | interval {2:g}s | {3:02d}:{4:02d}:{5:02d}".format(
        len(targets), timeout, interval, elapsed // 3600, (elapsed // 60) % 60, elapsed % 60)
    if not final:
        title += " | Ctrl+C to stop"
    legend = "! reply   . timeout   ? error   (newest right)"

    def framed(text, col=None):
        return box["v"] + " " + painter.color(fit(text, inner), col) + " " + box["v"]

    def framed_parts(parts):
        out, used = [], 0
        for text, col in parts:
            text = text[:max(0, inner - used)]
            used += len(text)
            out.append(painter.color(text, col))
        return box["v"] + " " + "".join(out) + " " * (inner - used) + " " + box["v"]

    def rule(left, right, label=None):
        if not label:
            return left + box["h"] * (inner + 2) + right
        label = " " + fit(label, min(len(label), inner - 4)).rstrip() + " "
        tail = box["h"] * max(0, inner + 2 - 1 - len(label))
        return left + box["h"] + painter.color(label, CYAN) + tail + right

    lines = [rule(box["tl"], box["tr"])]
    if painter.banner:
        lines.extend(framed_parts(parts) for parts in banner_lines(inner, started))
        lines.append(rule(box["lt"], box["rt"]))
    lines += [framed(title, BOLD),
             rule(box["lt"], box["rt"]),
             framed(header, BOLD)]

    # Groups in order of first appearance; ungrouped hosts first, unlabelled.
    order = []
    for t in targets:
        if t.group not in order:
            order.append(t.group)
    if "" in order:
        order.remove("")
        order.insert(0, "")

    for group in order:
        label = group if group else ("(no group)" if grouped else None)
        lines.append(rule(box["lt"], box["rt"], label))
        for t in targets:
            if t.group != group:
                continue
            s = t.snapshot()
            hist = s["history"][-hist_w:]
            col = {"ok": GREEN, "timeout": RED, "error": RED}.get(s["status"])
            lines.append(framed(row(t.name, s["lost"], s["streak"], hist), col))

    lines.append(rule(box["lt"], box["rt"]))
    lines.append(framed(legend))
    lines.append(rule(box["bl"], box["br"]))

    if painter.use_color:
        # Home cursor, overwrite each line (clear to end of line), clear the rest.
        out = "\033[H" + "\n".join(l + "\033[K" for l in lines) + "\n\033[J"
    else:
        out = "\n".join(lines) + "\n\n"
    sys.stdout.write(out)
    sys.stdout.flush()


def enable_ansi_on_windows():
    """Turn on ANSI escape handling in the Windows 10+ console."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def parse_entry(text):
    """
    Turn one entry into a list of (group, name, host) tuples.
      host                      -> ("", "", host)
      name,host                 -> ("", name, host)
      group,name,host           -> (group, name, host)
    Lines without commas may hold several space-separated hosts.
    """
    text = text.split("#", 1)[0].strip()
    if not text:
        return []
    if "," not in text:
        return [("", "", h) for h in text.split()]
    fields = [f.strip() for f in text.split(",")]
    if len(fields) > 3:  # tolerate stray empty fields, e.g. 'group,,name,ip'
        fields = [f for f in fields if f]
    if len(fields) == 2:
        fields.insert(0, "")
    if len(fields) != 3 or not fields[2]:
        raise ValueError("expected group,name,ip or name,ip: {0!r}".format(text))
    group, name, host = fields
    if host.lower() in ("ip", "host", "address") and name.lower() in ("name", "hostname"):
        return []  # header line
    return [(group, name, host)]


def read_hosts_file(path):
    entries = []
    with open(path) as handle:
        for lineno, line in enumerate(handle, 1):
            try:
                entries.extend(parse_entry(line))
            except ValueError as exc:
                raise ValueError("{0}, line {1}: {2}".format(path, lineno, exc))
    return entries


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ping up to {0} hosts in parallel with a live view.".format(MAX_TARGETS),
        epilog="Hosts can be plain IPs/names, 'name,ip' or 'group,name,ip', "
               "on the command line or one per line in a file.")
    parser.add_argument("hosts", nargs="*", help="host, name,ip or group,name,ip")
    parser.add_argument("-f", "--file", help="file with entries (one per line, # comments)")
    parser.add_argument("-t", "--timeout", type=float, default=1.0,
                        help="reply timeout in seconds (default: 1)")
    parser.add_argument("-i", "--interval", type=float, default=1.0,
                        help="seconds between pings to each host (default: 1)")
    parser.add_argument("-c", "--count", type=int, default=0,
                        help="pings per host, 0 = until Ctrl+C (default: 0)")
    parser.add_argument("--no-banner", action="store_true",
                        help="hide the cow and the big clock (saves screen rows)")
    parser.add_argument("--no-color", action="store_true",
                        help="plain output without colors or screen redraw")
    args = parser.parse_args()

    entries = []
    try:
        for item in args.hosts:
            entries.extend(parse_entry(item))
        if args.file:
            entries.extend(read_hosts_file(args.file))
    except (IOError, OSError) as exc:
        parser.error("cannot read {0}: {1}".format(args.file, exc))
    except ValueError as exc:
        parser.error(str(exc))

    seen, unique = set(), []
    for e in entries:
        if e not in seen:
            seen.add(e)
            unique.append(e)

    if not unique:
        parser.error("no hosts given")
    if len(unique) > MAX_TARGETS:
        parser.error("{0} hosts given, maximum is {1}".format(len(unique), MAX_TARGETS))
    if args.timeout <= 0:
        parser.error("timeout must be greater than 0")
    if args.interval <= 0:
        parser.error("interval must be greater than 0")
    if args.count < 0:
        parser.error("count cannot be negative")

    args.entries = unique
    return args


def main():
    args = parse_args()
    use_color = (not args.no_color and sys.stdout.isatty() and enable_ansi_on_windows())
    painter = Painter(use_color, banner=not args.no_banner)

    targets = [Target(host, name, group) for group, name, host in args.entries]
    stop = threading.Event()
    threads = []
    for t in targets:
        th = threading.Thread(target=worker,
                              args=(t, args.timeout, args.interval, args.count, stop))
        th.daemon = True
        th.start()
        threads.append(th)

    started = now()
    if use_color:
        sys.stdout.write("\033[2J\033[?25l")  # clear screen, hide cursor

    try:
        if use_color:
            while any(th.is_alive() for th in threads):
                render(targets, painter, args.timeout, args.interval, started)
                time.sleep(REFRESH)
        else:
            # Plain mode: print a table every 5 seconds instead of redrawing.
            last_print = 0
            while any(th.is_alive() for th in threads):
                if now() - last_print >= 5:
                    render(targets, painter, args.timeout, args.interval, started)
                    last_print = now()
                time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for th in threads:
            th.join(args.timeout + 4)
        render(targets, painter, args.timeout, args.interval, started, final=True)
        if use_color:
            sys.stdout.write("\033[?25h")  # show cursor again
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
