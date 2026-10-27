# cowping

## Why?

I had an old perl-script from around 1995 that I consider the OG multiping, used it for two decades, but Perl kinda faded away and Pyton became what the kool kidz use. So I made a python2 version of this at some stage, but did not have the time to keep it up to date while it worked fine for my typical lab-use and some maintenance windows for customers. 

Now Python 3 is nearly ubiquitus, so I thought it would be useful to update it for python3 and add some features.

With some AI coding assistance I improved on the original script, and here is the (much) improved version, with more cow-ness and available for all!

The script can ping up to 20 hosts in parallel and you watch the results live in your terminal, with a cow for moral support and live commentary.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  ______________                                                               │
│ < Ping is life >                              _   _     _   _     _   _       │
│  --------------                            |  _| o |_| |_| o  _| | |          │
│          \   ^__^                          | |_  o   |  _| o |_  |_|          │
│           \  (oo)\_______                                                     │
│              (__)\       )\/\             _   _     _   _     _   _   _       │
│                  ||----w |                _|  |    | | |   _| | | _| |_       │
│                  ||     ||               |_   | o  | |_| o |_  |_| |_  |_|    │
├───────────────────────────────────────────────────────────────────────────────┤
│ cowping | 4 targets | timeout 1s | interval 1s | 00:01:02 | Ctrl+C to stop    │
├───────────────────────────────────────────────────────────────────────────────┤
│ Host            Lost    Last  History                                         │
├─ Core ────────────────────────────────────────────────────────────────────────┤
│ core-sw01          0       0  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! │
│ core-sw02          3       0  !!!!!!!!!!!!!!!!!!...!!!!!!!!!!!!!!!!!!!!!!!!!! │
├─ WAN ─────────────────────────────────────────────────────────────────────────┤
│ isp-gw            62      62  .............................................. │
│ google-dns         0       0  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! │
├───────────────────────────────────────────────────────────────────────────────┤
│ ! reply   . timeout   ? error   (newest right)                                │
└───────────────────────────────────────────────────────────────────────────────┘
```

Rows are **green** while a host answers and **red** when it doesn't.

## Features

- Up to 20 hosts pinged in parallel, one thread per host
- Live, in-place redraw with a framed view
- Hosts organised into groups, each under its own header
- **Lost** = total lost pings, **Last** = consecutive lost pings (resets on a reply)
- Cisco-style history (`!` reply, `.` timeout, `?` error) that fills the full terminal width and grows when you widen the window
- Live clock and date in seven-segment ASCII, plus the cow
- Configurable timeout (default 1 s), interval and count
- No dependencies: Python 3 standard library only
- Linux, macOS/BSD and Windows

## Requirements

- Python 3.3 or newer (3.2 works, without the hard kill of a hung `ping`)
- The operating system's `ping` command

cowping does **not** need root. It runs the system `ping`, which already has the privileges to send ICMP (via `cap_net_raw`, unprivileged ICMP sockets, or setuid). If `ping` works for your user, cowping does too. If every row shows `?`, check that with:

```bash
ping -c1 127.0.0.1
getcap /usr/bin/ping                  # expect: cap_net_raw=ep
sysctl net.ipv4.ping_group_range
```

## Usage

```bash
python3 cowping.py 8.8.8.8 1.1.1.1 router.local
python3 cowping.py -f hosts.csv
python3 cowping.py -f hosts.csv -t 0.5 -i 2
python3 cowping.py "Core,core-sw01,10.0.0.1" "Core,core-sw02,10.0.0.2"
```

| Option | Default | Description |
|---|---|---|
| `-f`, `--file FILE` | | Read entries from a file |
| `-t`, `--timeout SEC` | `1` | Reply timeout in seconds (fractions allowed) |
| `-i`, `--interval SEC` | `1` | Time between pings to each host |
| `-c`, `--count N` | `0` | Pings per host, `0` runs until Ctrl+C |
| `--no-banner` | | Hide the cow and clock to save screen rows |
| `--no-color` | | Plain output; also used automatically when piped |

Press **Ctrl+C** to stop. The final state stays on screen.

## Host list format

Entries can be given on the command line or in a file, one per line. `#` starts a comment.

```csv
# group,name,ip
group,name,ip                 # optional header line, ignored
Core,core-sw01,10.0.0.1       # group, name and IP
Core,core-sw02,10.0.0.2
WAN,google-dns,8.8.8.8
fw01,10.0.0.254               # name and IP, no group
192.168.1.1                   # just an IP or hostname
```

- The **Host** column shows the name when one is given, otherwise the IP.
- Groups are shown in the order they first appear. Hosts without a group are listed first, under `(no group)` when other groups exist.
- Duplicate entries are ignored. More than 20 hosts is rejected.

See [`hosts.csv`](hosts.csv) for an example.

## Customising

Near the top of the banner section in `cowping.py`:

- `DATE_FORMAT` / `CLOCK_FORMAT`: `strftime` formats for the big clock (default `%d.%m.%Y` and `%H:%M:%S`)
- `COW`: the ASCII art
- `MAX_TARGETS`, `HISTORY_MAX`, `REFRESH`: limits and redraw rate

## Licence

Copyright (C) 2026 Conscia

cowping is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [`LICENSE`](LICENSE) for the full text.
