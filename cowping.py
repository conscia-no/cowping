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

# --------------------------------------------------------------------------- #
# How the program works, in short
# --------------------------------------------------------------------------- #
# 1. parse_args() reads the command line (and the hosts file, if one is given)
#    and turns it into a list of hosts to ping.
# 2. main() creates one Target object per host. A Target remembers everything
#    about that host: how many pings were lost, the history of results, etc.
# 3. main() starts one "thread" per host. A thread is a piece of the program
#    that runs at the same time as the rest, so all hosts are pinged in
#    parallel instead of one after the other. Each thread runs worker(),
#    which pings its host once per interval and stores the result in its Target.
# 4. Meanwhile the main thread calls render() four times a second. render()
#    reads every Target and draws the whole screen: banner, table and frame.
# 5. When you press Ctrl+C (or every host has been pinged --count times), the
#    threads are told to stop and a final summary is drawn.
#
# Reading tips for Python newcomers:
# - Lines starting with '#' are comments; Python ignores them.
# - Text in triple quotes right under a 'def' is a "docstring": a description
#   of what that function does.
# - Indentation (the spaces at the start of a line) is part of the language.
#   It shows which lines belong inside a function, loop or 'if'.
# - Names in CAPITALS (like MAX_TARGETS) are settings that never change while
#   the program runs. By convention they are called "constants".

# Makes print() behave the same in very old Python versions. Harmless in Python 3.
from __future__ import print_function

# "import" loads modules from Python's standard library (they come with every
# Python install, so nothing needs to be downloaded).
import argparse    # reading command-line options such as -t and -f
import math        # math.ceil() for rounding up
import os          # environment variables
import platform    # finding out which operating system we run on
import random      # shuffling the cow's sayings
import re          # "regular expressions": searching text for patterns
import subprocess  # running other programs (here: the system's ping)
import sys         # access to stdout (the screen) and the exit code
import threading   # running several things at the same time
import time        # clocks, sleeping, formatting the date and time

MAX_TARGETS = 20
HISTORY_MAX = 1000  # results kept per host; the screen shows as many as fit
REFRESH = 0.25  # seconds between screen redraws

# Work out the operating system once, at start-up. platform.system() returns
# e.g. "Linux", "Darwin" (macOS) or "Windows"; .lower() makes it lowercase.
SYSTEM = platform.system().lower()
IS_WINDOWS = SYSTEM.startswith("win")
IS_BSD_STYLE = SYSTEM == "darwin" or "bsd" in SYSTEM  # -W is in milliseconds

# now() returns the current time in seconds, used to measure how long things
# take. time.monotonic is best (it never jumps if the system clock is changed)
# but older Pythons don't have it, so getattr() falls back to time.time.
now = getattr(time, "monotonic", time.time)

# A regular expression that finds the round-trip time in ping's output.
# Matches "time=12.3 ms", "time<1ms", "tid=12 ms" (localised Windows) etc.
# In plain words: an '=' or '<', optional spaces, a number (with '.' or ','
# as decimal mark), optional spaces, then "ms". The number is "group 1".
RTT_RE = re.compile(r"[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms", re.IGNORECASE)
# The error raised when a program runs for too long. It doesn't exist before
# Python 3.3, in which case this is None.
TimeoutExpired = getattr(subprocess, "TimeoutExpired", None)


# --------------------------------------------------------------------------- #
# Pinging
# --------------------------------------------------------------------------- #
def build_command(host, timeout):
    """Return the ping command for one echo request on this platform."""
    # The command is built as a list: the program name followed by each
    # argument, e.g. ["ping", "-n", "-c", "1", "10.0.0.1"]. Using a list (not
    # one long string) means host names can't be misread as extra options.
    ipv6 = ":" in host  # IPv6 addresses contain colons, e.g. 2001:db8::1
    ms = str(max(1, int(round(timeout * 1000))))  # timeout in milliseconds, at least 1

    if IS_WINDOWS:
        # -n 1 = send one request, -w = wait this many milliseconds for the reply
        cmd = ["ping", "-n", "1", "-w", ms]
        if ipv6:
            cmd.append("-6")
        return cmd + [host]

    if IS_BSD_STYLE:
        # -n = don't look up names (faster), -c 1 = one request,
        # -W = wait this many milliseconds (on macOS/BSD)
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
    # Copy the environment variables and force the "C" locale, so ping prints
    # English text and '.' as decimal mark, whatever the user's language is.
    env = dict(os.environ)
    env["LC_ALL"] = "C"  # predictable output on Unix
    # try/except: run the code under "try"; if it fails with the named error,
    # run the "except" block instead of crashing the program.
    try:
        # Start ping. stdout=PIPE captures what it prints so we can read it,
        # and stderr=STDOUT mixes its error messages into the same text.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        # Typically: the ping program could not be found.
        return None, "cannot run {0}: {1}".format(cmd[0], exc)

    try:
        # Wait for ping to finish and collect its output, giving up after
        # `limit` seconds. The "_" is a name for a value we don't need.
        out, _ = proc.communicate(timeout=limit)
    except TypeError:  # Python < 3.3: no timeout argument
        out, _ = proc.communicate()
    except Exception as exc:
        if TimeoutExpired is not None and isinstance(exc, TimeoutExpired):
            # ping hung: stop it and report it as "no reply" (exit code 1).
            proc.kill()
            proc.communicate()
            return 1, ""
        raise  # any other error: let it through unchanged
    # The output arrives as raw bytes; decode() turns it into text. "replace"
    # puts a placeholder in for any bytes that aren't valid UTF-8.
    return proc.returncode, out.decode("utf-8", "replace")


def ping_once(host, timeout):
    """
    Send one echo request.
    Returns ("ok", rtt_ms) | ("timeout", None) | ("error", message).
    """
    # A function can return several values at once as a "tuple", written
    # (a, b). The caller unpacks them again: status, value = ping_once(...)
    start = now()
    # Allow ping a few extra seconds beyond the timeout before we kill it.
    code, output = run_command(build_command(host, timeout), timeout + 3)
    elapsed_ms = (now() - start) * 1000.0

    if code is None:  # ping could not even be started
        return "error", output

    # Decide whether we got a reply. A program's "exit code" (returncode) is a
    # number it hands back when it finishes; 0 conventionally means success.
    if IS_WINDOWS:
        # Windows can return 0 for "destination unreachable"; TTL= only appears on a real reply.
        success = code == 0 and "ttl=" in output.lower()
    else:
        success = code == 0

    if not success:
        # On Unix, exit code 1 = no reply, anything else = error (bad host etc.)
        if not IS_WINDOWS and code not in (0, 1):
            # Use the last line of ping's output as the error message.
            line = output.strip().splitlines()[-1] if output.strip() else "ping failed"
            return "error", line.strip()
        lowered = output.lower()
        # Windows uses exit code 1 for everything, so look for the text instead.
        # "fant ikke" is the Norwegian version of "could not find".
        if IS_WINDOWS and ("could not find host" in lowered or "fant ikke" in lowered):
            return "error", "unknown host"
        return "timeout", None

    # We got a reply. Find the round-trip time in the output. If it can't be
    # found, use the time we measured ourselves around the whole ping command.
    match = RTT_RE.search(output)
    rtt = float(match.group(1).replace(",", ".")) if match else elapsed_ms
    # A reply that came back later than the timeout still counts as lost
    # (needed because Linux ping can only wait in whole seconds, see above).
    if rtt > timeout * 1000.0:
        return "timeout", None
    return "ok", rtt


# --------------------------------------------------------------------------- #
# Per-target state
# --------------------------------------------------------------------------- #
# A "class" is a blueprint for objects. Each host gets its own Target object
# (created with Target(host, name, group)) holding that host's data. Inside the
# class, "self" means "this particular object", so self.lost is the lost-ping
# count of this one host.
class Target(object):
    # __init__ runs automatically when a new Target is created.
    def __init__(self, host, name="", group=""):
        self.host = host            # what we ping (IP or hostname)
        self.name = name or host    # label shown on screen
        self.group = group
        # A lock makes sure only one thread at a time touches this Target's
        # data. Without it, the screen could read a half-updated Target while
        # its worker thread is in the middle of recording a result.
        self.lock = threading.Lock()
        self.sent = 0
        self.lost = 0
        self.streak = 0             # consecutive lost pings, reset on a reply
        self.status = "waiting"     # "waiting" until the first result arrives
        self.history = []           # one character per ping: '!', '.' or '?'

    def record(self, status, value):
        """Store the result of one ping. Called by this host's worker thread."""
        # "with self.lock:" takes the lock, runs the indented block, and always
        # releases the lock again afterwards, even if something goes wrong.
        with self.lock:
            self.sent += 1  # += 1 means "add one to it"
            self.status = status
            if status == "ok":
                self.streak = 0
                mark = "!"
            else:
                self.lost += 1
                self.streak += 1
                mark = "?" if status == "error" else "."
            self.history.append(mark)
            # Keep memory use bounded: drop the oldest result when full.
            if len(self.history) > HISTORY_MAX:
                del self.history[0]

    def snapshot(self):
        """Return a consistent copy of the data, for the screen to draw."""
        with self.lock:
            # A dict ("dictionary") stores values under names (keys), so the
            # caller can write s["lost"] to get the lost count.
            # "".join(list) glues the history characters into one string.
            return dict(lost=self.lost, streak=self.streak, status=self.status,
                        history="".join(self.history))


def worker(target, timeout, interval, count, stop):
    """Ping one target again and again. Each host's thread runs this."""
    # `stop` is a threading.Event: a flag shared by all threads. main() sets it
    # when the user presses Ctrl+C, and every worker then finishes its loop.
    while not stop.is_set():
        started = now()
        status, value = ping_once(target.host, timeout)
        target.record(status, value)
        # With --count, stop after that many pings. A count of 0 counts as
        # "false" in Python, so 0 means "run forever".
        if count and target.sent >= count:
            break
        # Sleep for whatever is left of the interval, so pings start at a
        # steady rhythm no matter how long each one took. stop.wait() is used
        # instead of time.sleep() because it wakes up at once when stop is set.
        remaining = interval - (now() - started)
        if remaining > 0:
            stop.wait(remaining)


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #
# Characters for drawing the frame: corners (tl = top left, br = bottom right),
# horizontal and vertical lines, and the T-pieces (lt, rt) where a divider line
# meets the sides. ASCII_BOX is the fallback for terminals that can't show
# the nicer box-drawing characters.
UNICODE_BOX = {"tl": u"┌", "tr": u"┐", "bl": u"└", "br": u"┘",
               "h": u"─", "v": u"│", "lt": u"├", "rt": u"┤"}
ASCII_BOX = {"tl": "+", "tr": "+", "bl": "+", "br": "+",
             "h": "-", "v": "|", "lt": "+", "rt": "+"}
# ANSI colour codes. Terminals change colour when they receive the special
# text "\033[" + code + "m" ("\033" is the invisible Escape character).
# "\033[0m" switches back to normal. 1 = bold, 31 = red, 32 = green, etc.
# The line below assigns five names at once, in order.
GREEN, RED, BOLD, CYAN, YELLOW = "32", "31", "1", "1;36", "33"
MIN_HISTORY = 10  # the History column is never narrower than this


def pick_box_chars():
    """Use box-drawing characters if the terminal encoding can show them."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    # Try to convert the box characters to the terminal's encoding. If that
    # fails, the terminal can't display them, so use plain ASCII instead.
    try:
        u"".join(UNICODE_BOX.values()).encode(encoding)
        return UNICODE_BOX
    except (UnicodeError, LookupError):
        return ASCII_BOX


def terminal_width():
    """Number of characters that fit on one line of the terminal window."""
    try:
        import shutil
        # (80, 24) is the fallback size if the real size can't be found,
        # e.g. when the output is sent to a file instead of a window.
        return shutil.get_terminal_size((80, 24)).columns
    except (ImportError, AttributeError):  # Python < 3.3
        try:
            return int(os.environ.get("COLUMNS", 80))
        except ValueError:
            return 80


class Painter(object):
    """Display settings shared by all the drawing code."""
    def __init__(self, use_color, banner=True):
        self.use_color = use_color
        self.banner = banner
        self.box = pick_box_chars()

    def color(self, text, code):
        """Wrap text in an ANSI colour code, unless colours are switched off."""
        if not self.use_color or not code:
            return text
        return "\033[{0}m{1}\033[0m".format(code, text)


def fit(text, width):
    """Left-justify text in width, truncating with '~' if too long."""
    # text[:n] means "the first n characters of text" (this is called slicing).
    # ljust() pads with spaces on the right up to the given width.
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
# Length of the longest saying. "max(len(s) for s in ...)" goes through every
# saying, measures it, and keeps the biggest number.
SAY_W = max(len(s) for s in [PING_IS_LIFE] + SAYINGS)

# The r in front of each string means "raw": backslashes are kept as they are
# instead of being treated as special characters.
COW = [
    r"         \   ^__^",
    r"          \  (oo)\_______",
    r"             (__)\       )\/\ ",
    r"                 ||----w |",
    r"                 ||     ||",
]
# Wide enough for the longest saying, so the clock never moves.
COW_W = max([SAY_W + 4] + [len(l) for l in COW])

# What the cow is saying right now. It lives outside any function so that it
# is remembered between screen redraws:
#   slot = which minute of the run the current saying belongs to
#   text = the current saying
#   deck = sayings not used yet in this round, in shuffled order
_saying = {"slot": 0, "text": PING_IS_LIFE, "deck": []}


def cow_saying(started):
    """What the cow says now: PING_IS_LIFE first, then a new saying every minute."""
    # // divides and rounds down, so this is "whole minutes since start"
    # (0 during the first minute, 1 during the second, and so on).
    slot = int(now() - started) // SAYING_EVERY
    if slot != _saying["slot"]:
        # A new minute has started: take the next saying from the deck,
        # like drawing cards. When the deck is empty, shuffle a new one.
        deck = _saying["deck"]
        if not deck:
            deck[:] = random.sample(SAYINGS, len(SAYINGS))  # shuffled copy
            # Sayings are taken from the end of the list. If the first one in
            # the new deck is the one just shown, move it to the front so the
            # cow never says the same thing twice in a row.
            if deck[-1] == _saying["text"]:
                deck.insert(0, deck.pop())
        _saying["slot"] = slot
        _saying["text"] = deck.pop()  # pop() removes and returns the last item
    return _saying["text"]


def cow_lines(text):
    """Speech bubble with text above the cow."""
    # "_" * 5 gives "_____": multiplying a string repeats it.
    # Adding two lists (+) joins them into one longer list.
    return [" " + "_" * (len(text) + 2),
            "< " + text + " >",
            " " + "-" * (len(text) + 2)] + COW

# The big clock digits, drawn like a digital alarm clock. Each character maps
# to three lines of text, e.g. "2" is:
#    _
#    _|
#   |_
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
# How the time and date are written, in strftime format: %H = hour,
# %M = minute, %S = second, %d = day, %m = month, %Y = four-digit year.
CLOCK_FORMAT = "%H:%M:%S"
DATE_FORMAT = "%d.%m.%Y"


def big_text(text):
    """Render text in the seven-segment font; returns three lines."""
    # Look up each character's glyph (unknown characters become a blank).
    # The [... for ... in ...] form is a "list comprehension": a compact way
    # to build a list by running an expression for every item.
    glyphs = [SEGMENT_FONT.get(ch, SEGMENT_FONT[" "]) for ch in text]
    # Build the top line from the top rows of all glyphs, then the middle
    # line, then the bottom line, with a space between glyphs.
    return [" ".join(g[row] for g in glyphs) for row in range(3)]


def banner_lines(inner, started):
    """
    Return a list of [(text, colour), ...] parts per line: the cow on the left,
    time and date on the right. Stacks them if the window is too narrow.
    """
    # `inner` is the width available inside the frame.
    cow = cow_lines(cow_saying(started))
    clock = big_text(time.strftime(CLOCK_FORMAT))
    date = big_text(time.strftime(DATE_FORMAT))
    # Stack the clock, an empty line and the date into one block, with every
    # line padded to the same width so the block lines up neatly.
    clock_w = max(len(l) for l in clock + date)
    clock_block = [l.ljust(clock_w) for l in clock] + [" " * clock_w] + \
                  [l.ljust(clock_w) for l in date]

    gap = 6  # minimum spaces between the cow and the clock
    if COW_W + gap + clock_w <= inner:
        # Side by side, clock centred in the space to the right of the cow.
        lpad = (inner - COW_W - clock_w) // 2 + COW_W // 4
        lpad = max(gap, min(lpad, inner - COW_W - clock_w))
        # Which cow line the clock starts on, so it sits vertically centred.
        top = (len(cow) - len(clock_block)) // 2 + 1
        lines = []
        # enumerate() gives both the position (i) and the item (cow_line).
        for i, cow_line in enumerate(cow):
            j = i - top  # which clock line belongs next to this cow line
            right = clock_block[j] if 0 <= j < len(clock_block) else ""
            lines.append([(cow_line.ljust(COW_W), YELLOW),
                          (" " * lpad, None),
                          (right, CYAN)])
        return lines

    # Narrow window: cow, then clock underneath.
    return [[(l, YELLOW)] for l in cow] + [[(l, CYAN)] for l in clock_block]


def render(targets, painter, timeout, interval, started, final=False):
    """Draw the whole screen: frame, banner, title, table and legend."""
    box = painter.box
    grouped = any(t.group for t in targets)  # True if at least one host has a group

    # Host column shows the name if one was given, otherwise the IP/host.
    # Its width fits the longest name, but stays between 4 and 30 characters.
    name_w = max(4, min(30, max(len(t.name) for t in targets)))

    # Fixed columns: host, lost, last. History gets all the rest.
    # (name + 2 spaces + 6 for Lost + 2 spaces + 6 for Last + 2 spaces)
    fixed = name_w + 2 + 6 + 2 + 6 + 2
    # 4 = "| " + " |", 1 spare so we never write into the last terminal column.
    hist_w = max(MIN_HISTORY, terminal_width() - 4 - 1 - fixed)
    inner = fixed + hist_w  # width of everything between the frame's sides

    # Functions can be defined inside other functions. These small helpers can
    # use render's variables (name_w, hist_w, inner, box, ...) directly.
    def row(name, lost, streak, hist):
        """One table row as plain text, every column at its fixed width."""
        # "{0:>6}".format(x) writes x right-aligned in 6 characters.
        return "  ".join([fit(name, name_w), "{0:>6}".format(lost),
                          "{0:>6}".format(streak), fit(hist, hist_w)])

    header = row("Host", "Lost", "Last", "History")

    # Running time as hh:mm:ss. {3:02d} means "argument 3, as a whole number,
    # at least 2 digits, padded with zeros" (so 5 becomes "05").
    elapsed = int(now() - started)
    title = "cowping | {0} targets | timeout {1:g}s | interval {2:g}s | {3:02d}:{4:02d}:{5:02d}".format(
        len(targets), timeout, interval, elapsed // 3600, (elapsed // 60) % 60, elapsed % 60)
    if not final:
        title += " | Ctrl+C to stop"
    legend = "! reply   . timeout   ? error   (newest right)"

    # Text is padded to its full width first and coloured afterwards. Colour
    # codes are invisible but still count as characters, so measuring after
    # colouring would get the widths wrong and break the frame.
    def framed(text, col=None):
        """A line of text between the frame's left and right sides."""
        return box["v"] + " " + painter.color(fit(text, inner), col) + " " + box["v"]

    def framed_parts(parts):
        """Like framed(), but for a line made of differently coloured parts."""
        out, used = [], 0
        for text, col in parts:
            text = text[:max(0, inner - used)]  # cut off anything that won't fit
            used += len(text)
            out.append(painter.color(text, col))
        return box["v"] + " " + "".join(out) + " " * (inner - used) + " " + box["v"]

    def rule(left, right, label=None):
        """A horizontal frame line, optionally with a group name in it."""
        if not label:
            return left + box["h"] * (inner + 2) + right
        label = " " + fit(label, min(len(label), inner - 4)).rstrip() + " "
        tail = box["h"] * max(0, inner + 2 - 1 - len(label))
        return left + box["h"] + painter.color(label, CYAN) + tail + right

    # Collect every screen line in a list, top to bottom.
    lines = [rule(box["tl"], box["tr"])]
    if painter.banner:
        lines.extend(framed_parts(parts) for parts in banner_lines(inner, started))
        lines.append(rule(box["lt"], box["rt"]))
    lines += [framed(title, BOLD),
             rule(box["lt"], box["rt"]),
             framed(header, BOLD)]

    # Groups in order of first appearance; ungrouped hosts first, unlabelled.
    # ("" is the group name of hosts that have no group.)
    order = []
    for t in targets:
        if t.group not in order:
            order.append(t.group)
    if "" in order:
        order.remove("")
        order.insert(0, "")

    for group in order:
        # Divider line with the group name. Ungrouped hosts get "(no group)",
        # but only if there are other groups; otherwise no label at all.
        label = group if group else ("(no group)" if grouped else None)
        lines.append(rule(box["lt"], box["rt"], label))
        for t in targets:
            if t.group != group:
                continue  # skip hosts that belong to another group
            s = t.snapshot()
            hist = s["history"][-hist_w:]  # [-n:] = the last n characters (newest)
            # Pick the row colour from the latest result. .get() returns None
            # (no colour) for "waiting", i.e. before the first result.
            col = {"ok": GREEN, "timeout": RED, "error": RED}.get(s["status"])
            lines.append(framed(row(t.name, s["lost"], s["streak"], hist), col))

    lines.append(rule(box["lt"], box["rt"]))
    lines.append(framed(legend))
    lines.append(rule(box["bl"], box["br"]))

    if painter.use_color:
        # Redraw in place instead of scrolling, using more ANSI codes:
        #   \033[H = move the cursor to the top-left corner
        #   \033[K = erase the rest of the line (removes leftovers from the
        #            previous frame if the new line is shorter)
        #   \033[J = erase everything below (if the window got smaller)
        out = "\033[H" + "\n".join(l + "\033[K" for l in lines) + "\n\033[J"
    else:
        out = "\n".join(lines) + "\n\n"
    # Write the whole frame in one go, and flush() so it appears immediately
    # instead of waiting in a buffer. This avoids flicker.
    sys.stdout.write(out)
    sys.stdout.flush()


def enable_ansi_on_windows():
    """Turn on ANSI escape handling in the Windows 10+ console."""
    # Linux and macOS terminals understand colour codes out of the box.
    if not IS_WINDOWS:
        return True
    # On Windows the console must be told to understand them. ctypes lets
    # Python call functions in Windows' own system libraries (here kernel32).
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # -11 = the standard output window
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # Switch on flag 0x0004, "ENABLE_VIRTUAL_TERMINAL_PROCESSING", which
        # makes the console interpret ANSI codes. The | keeps existing flags.
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False  # anything went wrong: just run without colours


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
    # Drop everything from '#' onwards (a comment), then surrounding spaces.
    # split("#", 1) cuts the text in two at the first '#'; [0] is the part before.
    text = text.split("#", 1)[0].strip()
    if not text:
        return []  # empty or comment-only line: nothing to add
    if "," not in text:
        return [("", "", h) for h in text.split()]
    fields = [f.strip() for f in text.split(",")]
    if len(fields) > 3:  # tolerate stray empty fields, e.g. 'group,,name,ip'
        fields = [f for f in fields if f]
    if len(fields) == 2:
        fields.insert(0, "")  # name,ip: add an empty group in front
    if len(fields) != 3 or not fields[2]:
        # "raise" reports an error. parse_args() catches it and shows it to the user.
        raise ValueError("expected group,name,ip or name,ip: {0!r}".format(text))
    group, name, host = fields
    # Skip a header line such as "group,name,ip" at the top of a CSV file.
    if host.lower() in ("ip", "host", "address") and name.lower() in ("name", "hostname"):
        return []  # header line
    return [(group, name, host)]


def read_hosts_file(path):
    """Read every entry from a hosts file, one entry per line."""
    entries = []
    # "with open(...)" opens the file and closes it again automatically.
    with open(path) as handle:
        # enumerate(handle, 1) numbers the lines starting from 1, so error
        # messages can say which line was wrong.
        for lineno, line in enumerate(handle, 1):
            try:
                entries.extend(parse_entry(line))
            except ValueError as exc:
                raise ValueError("{0}, line {1}: {2}".format(path, lineno, exc))
    return entries


def parse_args():
    """Read and check the command-line options. Exits with a message on mistakes."""
    # argparse does the hard work: it understands options like -t 0.5,
    # converts them to the right type, and generates the --help text.
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
    # The results become attributes: args.timeout, args.no_banner, etc.
    # (dashes in option names turn into underscores).
    args = parser.parse_args()

    # Gather hosts from the command line first, then from the file.
    entries = []
    try:
        for item in args.hosts:
            entries.extend(parse_entry(item))
        if args.file:
            entries.extend(read_hosts_file(args.file))
    except (IOError, OSError) as exc:
        # parser.error() prints the message and exits the program.
        parser.error("cannot read {0}: {1}".format(args.file, exc))
    except ValueError as exc:
        parser.error(str(exc))

    # Remove duplicates but keep the original order. A set can quickly tell
    # whether we have seen an entry before.
    seen, unique = set(), []
    for e in entries:
        if e not in seen:
            seen.add(e)
            unique.append(e)

    # Sanity checks on the options.
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
    """Start everything, keep the screen updated, and clean up at the end."""
    args = parse_args()
    # Use colours and live redraw only when writing to a real terminal
    # (isatty), not when the output is sent to a file or another program.
    use_color = (not args.no_color and sys.stdout.isatty() and enable_ansi_on_windows())
    painter = Painter(use_color, banner=not args.no_banner)

    # One Target per host, then one thread per Target running worker().
    targets = [Target(host, name, group) for group, name, host in args.entries]
    stop = threading.Event()
    threads = []
    for t in targets:
        th = threading.Thread(target=worker,
                              args=(t, args.timeout, args.interval, args.count, stop))
        # A "daemon" thread doesn't keep the program alive on its own, so a
        # stuck ping can never stop cowping from exiting.
        th.daemon = True
        th.start()
        threads.append(th)

    started = now()
    if use_color:
        sys.stdout.write("\033[2J\033[?25l")  # clear screen, hide cursor

    try:
        if use_color:
            # Redraw until every worker has finished (only happens with --count;
            # otherwise this runs until Ctrl+C).
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
        # Ctrl+C raises KeyboardInterrupt. That's how the user stops cowping,
        # so it isn't an error: just carry on to the clean-up below.
        pass
    finally:
        # "finally" always runs, however the "try" block ended.
        stop.set()  # tell all workers to stop
        for th in threads:
            th.join(args.timeout + 4)  # wait for each one, but not forever
        render(targets, painter, args.timeout, args.interval, started, final=True)
        if use_color:
            sys.stdout.write("\033[?25h")  # show cursor again
            sys.stdout.flush()

    return 0  # exit code 0 = success


# This is true only when the file is run directly (python3 cowping.py), not
# when another script imports it. It's the program's starting point.
if __name__ == "__main__":
    sys.exit(main())
