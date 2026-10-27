# CLAUDE.md

Guidance for Claude when working in this repository.

## What this is

`cowping.py` is a single-file terminal tool that pings up to 20 hosts in parallel and shows a live, framed, colour-coded view with a cowsay-style banner and a seven-segment ASCII clock. `hosts.csv` is an example input file.

## Hard constraints

- **Standard library only.** No third-party packages, ever. No `requirements.txt`.
- **Single file.** Keep everything in `cowping.py` so it can be copied to any box and run.
- **Python 3 compatibility.** Target Python 3.3+, and don't break 3.2 needlessly:
  - no f-strings, no type hints, no walrus, no `subprocess.run`, no `pathlib`, no dataclasses
  - use `"{0}".format(...)`, `subprocess.Popen` + `communicate()`
  - `shutil.get_terminal_size` and `time.monotonic` are guarded with fallbacks; keep it that way
- **Cross-platform.** Linux (iputils and busybox), macOS/BSD and Windows. Ping flags differ per OS (see `build_command`).
- **No root required.** Use the system `ping` command; never raw ICMP sockets.

## Architecture

- `build_command` / `run_command` / `ping_once`: one echo request through the OS `ping`. Returns `("ok", rtt)`, `("timeout", None)` or `("error", msg)`.
  - Linux `-W` rounds up to whole seconds on older iputils, so the exact timeout is enforced by comparing the measured RTT.
  - On Windows, success requires `TTL=` in the output (exit code 0 alone is not enough). RTT parsing handles localised output such as Norwegian `tid=`.
- `Target`: per-host state (`lost`, `streak`, `status`, `history`), guarded by a lock.
- `worker`: one thread per target. Loops ping, record, sleep for the rest of the interval.
- `render`: builds the whole frame each refresh (0.25 s). Redraws in place with ANSI (`\033[H` + `\033[K` per line + `\033[J`). Rows are built as plain text, padded, and only then coloured, so widths are measured without escape codes.
- `banner_lines`: cow plus a big clock side by side, stacked when the window is narrow.
- `parse_entry` / `read_hosts_file`: input formats `host`, `name,ip`, `group,name,ip`. Comments use `#`, and a header line is skipped.

## Display rules (user decisions, keep them)

- Columns: **Host** (name if given, else IP), **Lost** (total lost), **Last** (consecutive lost, reset on reply), **History**.
- History takes all remaining terminal width. No error text at the end of rows.
- The whole row is green on the last reply, red on timeout or error, and uncoloured before the first result.
- Groups are shown with a labelled divider in order of first appearance. Ungrouped hosts go first.
- Banner: the cow says "Ping is life" for the first minute, then a new random line from `SAYINGS` every minute (all 20 before any repeats). Date format is `dd.mm.yyyy`. `--no-banner` hides it.
- Box-drawing frame, with an automatic ASCII fallback when the terminal encoding can't show it.

## Testing

There is no test suite. Verify manually:

```bash
python3 -m py_compile cowping.py
COLUMNS=90 python3 cowping.py --no-color -c 3 -t 0.5 -f hosts.csv
python3 cowping.py -c 3 127.0.0.1 10.255.255.1 no.such.host.invalid
```

These cover a reply (127.0.0.1), a timeout (10.255.255.1) and an error (the invalid hostname). Check both colour mode (a real terminal) and `--no-color`, and narrow and wide `COLUMNS`.
