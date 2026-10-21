#!/usr/bin/env python3
# <xbar.title>FreeRAM</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.desc>RAM gauge for Claude Code users: see which background Claude sessions hold your memory and stop them in one click.</xbar.desc>
# <xbar.dependencies>python3, Claude Code CLI</xbar.dependencies>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
"""FreeRAM: a SwiftBar plugin. The menu bar shows RAM in use; the menu lists background
Claude Code sessions, largest first, and stops one after a one-click confirm.

  freeram.1m.py               menu output (SwiftBar runs this every minute and on open)
  freeram.1m.py stop <id>     confirm, then `claude stop <id>` (the conversation is kept)
  freeram.1m.py unload-mlx    confirm, then stop a local mlx_lm.server (only listed while one runs)
  freeram.1m.py --selftest
FREERAM_DRY_RUN=1 prints the stop/unload command instead of running it.
FREERAM_DEMO=1 shows sample sessions (for screenshots).
"""
import base64, json, math, os, re, struct, subprocess, sys, zlib

HOME = os.path.expanduser("~")
# absolute paths: SwiftBar runs plugins with a bare PATH
CLAUDE = next((p for p in (f"{HOME}/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
               if os.access(p, os.X_OK)), None)
SELF = os.path.abspath(__file__)
PRESSURE = {1: ("Normal", None), 2: ("Warning", (255, 149, 0)), 4: ("Critical", (255, 59, 48))}
STATE_ICON = {"working": "bolt", "blocked": "exclamationmark.triangle", "done": "checkmark.circle", "idle": "moon.zzz"}
DEMO = [(2410, {"id": "a1b2c3d4", "name": "refactor auth module", "state": "working"}, "2:14:03"),
        (1630, {"id": "b2c3d4e5", "name": "fix flaky integration tests", "state": "done"}, "1-03:20:11"),
        (1180, {"id": "c3d4e5f6", "name": "research: vector databases", "state": "blocked"}, "45:12"),
        (870, {"id": "d4e5f6a7", "name": "write release notes", "state": "idle"}, "5:02:40")]


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def mb(s):
    s = s.rstrip("+-")  # top may suffix a delta marker
    n = float(re.sub(r"[^\d.]", "", s) or 0)
    return n * 1024 if s.endswith("G") else n / 1024 if s.endswith("K") else n


def parse_top(out):
    # top's MEM = physical footprint incl. GPU memory; ps RSS undercounts local models by GBs
    return {int(p): mb(m) for p, m in re.findall(r"^(\d+)\s+(\S+)\s*$", out, re.M)}


def ram_used(vm):
    # Activity Monitor's "Memory Used" = app (anonymous - purgeable) + wired + compressed
    page = int(re.search(r"page size of (\d+)", vm).group(1))
    n = lambda k: int(re.search(rf"^{k}:\s+(\d+)", vm, re.M).group(1))
    return (n("Anonymous pages") - n("Pages purgeable") + n("Pages wired down") + n("Pages occupied by compressor")) * page


def size(m):
    return f"{m / 1024:.1f} GB" if m >= 1000 else f"{m:.0f} MB"


def age(etime):
    # ps etime is [[dd-]hh:]mm:ss
    d, _, rest = etime.strip().rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    h, m = (parts[-3] if len(parts) == 3 else 0), parts[-2]
    d = int(d or 0)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def capsule(px, py, x0, y0, x1, y1):
    # signed distance to a fully rounded rectangle (negative inside)
    r = min(x1 - x0, y1 - y0) / 2
    qx, qy = max(x0 + r - px, px - (x1 - r), 0), max(y0 + r - py, py - (y1 - r), 0)
    return math.hypot(qx, qy) - r


def bar_png(frac, rgb=None, segs=8, w=62, h=28):
    # level meter of pill segments (a plain bar reads as a battery), 31x14pt at 2x (pHYs = 144 dpi).
    # rgb=None -> black template image that macOS tints for light/dark menu bars
    lit = math.ceil(min(max(frac, 0), 1) * segs)
    color = rgb or (0, 0, 0)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            seg, off = divmod(x, 8)  # 6px segment + 2px gap
            hit = 0
            if off < 6 and seg < segs:
                for sy in (0.125, 0.375, 0.625, 0.875):  # 4x4 supersampling for smooth edges
                    for sx in (0.125, 0.375, 0.625, 0.875):
                        hit += capsule(off + sx, y + sy, 0, 3, 6, h - 3) <= 0
            alpha = 255 if seg < lit else 80  # unlit segments stay faintly visible
            raw += bytes(color) + bytes([round(alpha * hit / 16)])
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1)) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def background(sessions):
    return [s for s in sessions if s.get("kind") != "interactive"]


def sessions():
    return json.loads(run(CLAUDE, "agents", "--json") or "[]") if CLAUDE else []


def confirm(msg, button):
    r = subprocess.run(["/usr/bin/osascript", "-e", "on run argv",
                        "-e", f'display dialog (item 1 of argv) with title "FreeRAM" buttons {{"Cancel", "{button}"}} default button "{button}" cancel button "Cancel"',
                        "-e", "end run", msg], capture_output=True)
    return r.returncode == 0


def notify(msg):
    subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "FreeRAM"',
                    "-e", "end run", msg], capture_output=True)


def act(cmd):
    if os.environ.get("FREERAM_DRY_RUN"):
        print("DRY RUN:", " ".join(cmd))
        return True
    return subprocess.run(cmd).returncode == 0


def menu():
    total = int(run("/usr/sbin/sysctl", "-n", "hw.memsize"))
    used = ram_used(run("/usr/bin/vm_stat"))
    level = int(run("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level") or 1)
    label, rgb = PRESSURE.get(level, PRESSURE[1])
    kind = "image" if rgb else "templateImage"
    tint = " color=#%02X%02X%02X" % rgb if rgb else ""
    print(f"{used / total:.0%} | {kind}={base64.b64encode(bar_png(used / total, rgb)).decode()}{tint}")
    print("---")
    print(f"Memory  {used / 2**30:.1f} of {total / 2**30:.0f} GB used | sfimage=memorychip")
    swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", run("/usr/sbin/sysctl", "-n", "vm.swapusage"))
    if swap:
        print(f"Swap  {float(swap.group(2)) / 1024:.1f} of {float(swap.group(1)) / 1024:.1f} GB | sfimage=arrow.triangle.2.circlepath")
    print(f"Pressure  {label} | sfimage=gauge.with.dots.needle.33percent")
    print("---")

    if os.environ.get("FREERAM_DEMO"):
        rows = DEMO
    else:
        mem = parse_top(run("/usr/bin/top", "-l", "1", "-o", "mem", "-n", "300", "-stats", "pid,mem"))

        def tree(pid):  # session + its MCP server children
            return mem.get(pid, 0) + sum(tree(int(k)) for k in run("/usr/bin/pgrep", "-P", str(pid)).split())

        ages = dict(l.split(None, 1) for l in run("/bin/ps", "-axo", "pid=,etime=").strip().splitlines())
        rows = sorted(((tree(int(s["pid"])), s, ages.get(str(s["pid"]), "0:00")) for s in background(sessions())),
                      key=lambda r: -r[0])

    if not CLAUDE:
        print("Claude Code CLI not found | color=#8E8E93")
    print(f"Background Claude sessions · {len(rows)} · {size(sum(r[0] for r in rows))} | size=11 color=#8E8E93")
    if not rows:
        print("None running | color=#8E8E93")
    for m, s, et in rows:
        name = (s.get("name") or s["id"]).replace("|", "/")
        state = s.get("state", "?")
        icon = STATE_ICON.get(state, "circle")
        print(f'{name}   {size(m)} · {age(et)} · {state} | sfimage={icon} tooltip="State: {state}. Click to stop (the conversation is kept)." '
              f'bash="{SELF}" param1=stop param2={s["id"]} terminal=false refresh=true')

    mlx = mlx_pids()
    if mlx and not os.environ.get("FREERAM_DEMO"):
        mem_mlx = sum(mem.get(int(p), 0) for p in mlx)
        print("---")
        print(f'Stop local MLX server   {size(mem_mlx)} | sfimage=cpu bash="{SELF}" param1=unload-mlx terminal=false refresh=true')
    print("---")
    print("Stopping keeps the conversation: resume with claude attach <id> | size=11 color=#8E8E93")


def stop(sid):
    s = next((s for s in sessions() if s["id"] == sid), None)
    if not s or s.get("kind") == "interactive":  # never stop an interactive session, even if asked
        return
    name = s.get("name") or sid
    if confirm(f"Stop \"{name}\" ({s.get('state', '?')})?\n\nThe conversation is kept. Resume it any time with:\nclaude attach {sid}", "Stop"):
        if act([CLAUDE, "stop", sid]):
            notify(f"Stopped \"{name}\". Resume with: claude attach {sid}")


def mlx_pids():
    # a local model served by mlx_lm.server holds GPU memory that Activity Monitor hides
    return run("/usr/bin/pgrep", "-f", "python.*mlx_lm.server").split()


def unload_mlx():
    pids = mlx_pids()
    if pids and confirm("Stop the local MLX server (mlx_lm.server)?\n\nThis frees the model's memory. Start it again when you need it.", "Stop"):
        act(["/bin/kill", *pids])


def selftest():
    assert parse_top("PID  MEM\n101  1234M\n102  2.5G\n103  512K\n104  80M+\n") == {101: 1234, 102: 2560, 103: 0.5, 104: 80}
    assert [s["id"] for s in background([{"id": "a", "kind": "background"}, {"id": "b", "kind": "interactive"}, {"id": "c"}])] == ["a", "c"]
    vm = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages wired down:  10.\nPages purgeable:  2.\nAnonymous pages:  20.\nPages occupied by compressor:  5.\n"
    assert ram_used(vm) == 33 * 16384
    assert [age(e) for e in ("05:09", "2:14:03", "1-03:20:11")] == ["5m", "2h 14m", "1d 3h"]
    assert size(870) == "870 MB" and size(2410) == "2.4 GB"
    png = bar_png(0.5)
    assert png.startswith(b"\x89PNG") and struct.unpack(">II", png[16:24]) == (62, 28)
    print("selftest ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["stop"] and len(a) > 1:
        stop(a[1])
    elif a[:1] == ["unload-mlx"]:
        unload_mlx()
    elif a[:1] == ["--selftest"]:
        selftest()
    else:
        menu()
