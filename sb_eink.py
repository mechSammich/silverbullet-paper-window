#!/usr/bin/env python3
"""Show SilverBullet tasks one at a time on a Waveshare 2.13" e-paper HAT.

Buttons: next / previous / done. "done" re-fetches the page, flips one `[ ]`
to `[x]` and PUTs the page back. See README.md for setup.
"""
import argparse
import difflib
import http.client
import http.cookiejar
import json
import logging
import os
import queue
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

log = logging.getLogger("sb_eink")

W, H = 250, 122  # landscape canvas
FOOTER_H = 16

# ---------------------------------------------------------------- errors


class SBError(Exception):
    short = "Error"  # short text suitable for the display

    def __init__(self, short=None):
        super().__init__(short or self.short)
        if short:
            self.short = short


class ConfigError(SBError):
    short = "Config error"


class NetworkError(SBError):
    short = "Network down"


class AuthError(SBError):
    short = "Auth failed"


class PageNotFound(SBError):
    short = "Page not found"


class ConflictError(SBError):
    short = "Page changed"


class TaskNotFound(SBError):
    short = "Task not found"


class TaskAmbiguous(SBError):
    short = "Duplicate task"


# ---------------------------------------------------------------- parse / toggle

TASK_RE = re.compile(r"^\s*[-*] \[( |x)\] (.*)$")
LEADING_REF_RE = re.compile(r"^\s*\[\[[^\]]*\]\]:?\s*")
CHARS_PER_LINE = 30


@dataclass(frozen=True)
class Task:
    text: str  # raw text after the checkbox (used to locate the line)
    display: str  # text shown on screen (tag removed)


def split_lines(page):
    """Split on \\n keeping each line's ending (\\n or \\r\\n) attached."""
    return re.findall(r"[^\n]*\n|[^\n]+", page)


def _tag_re(tag):
    return re.compile(r"(?<!\S)" + re.escape(tag) + r"(?!\w)") if tag else None


def _match(line):
    return TASK_RE.match(line.rstrip("\r\n"))


def parse_tasks(page, tag=""):
    tre = _tag_re(tag)
    tasks = []
    for line in split_lines(page):
        m = _match(line)
        if not m or m.group(1) != " ":
            continue
        text = m.group(2)
        if tre and not tre.search(text):
            continue
        shown = " ".join((tre.sub("", text) if tre else text).split())
        shown = LEADING_REF_RE.sub("", shown)
        tasks.append(Task(text, shown or text))
    return tasks


def toggle_task(page, text, tag=""):
    """Return `page` with the single unchecked task `text` checked off.

    Raises TaskNotFound / TaskAmbiguous; every other byte is left untouched.
    """
    tre = _tag_re(tag)
    lines = split_lines(page)
    hits = []
    for i, line in enumerate(lines):
        m = _match(line)
        if m and m.group(1) == " " and m.group(2) == text and (not tre or tre.search(text)):
            hits.append((i, m.start(1)))
    if not hits:
        raise TaskNotFound()
    if len(hits) > 1:
        raise TaskAmbiguous()
    i, pos = hits[0]
    lines[i] = lines[i][:pos] + "x" + lines[i][pos + 1:]
    return "".join(lines)


# ---------------------------------------------------------------- config

DEFAULTS = {
    "base_url": "",
    "token": "",  # bearer token (SB_AUTH_TOKEN on the server)
    "username": "",  # cookie login via POST /.auth (used when no token)
    "password": "",
    "page": "Tasks.md",
    "tag": "#eink",  # "" = show all unchecked tasks
    "poll_interval": 300,
    "retry_interval": 60,  # poll interval while offline
    "timeout": 15,
    "verify_tls": True,
    "ca_file": "",
    # Unverified against a live server: make adjustable.
    "fs_prefix": "/.fs",
    "send_sync_mode": True,  # X-Sync-Mode: true on every request
    "use_if_match": True,  # If-Match with the ETag from GET
    "epd_revision": None,  # V2 | V3 | V4 | b_V4 (no built-in default on purpose)
    "full_refresh_every": 20,
    "rotate180": False,
    "font_path": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "pin_next": 5,
    "pin_prev": 6,
    "pin_done": 13,
    "debounce": 0.05,
    "cache_file": os.path.join(os.path.expanduser("~"), ".cache", "sb-eink", "cache.json"),
}

ENV_OVERRIDES = {
    "SB_EINK_URL": "base_url",
    "SB_EINK_TOKEN": "token",
    "SB_EINK_USERNAME": "username",
    "SB_EINK_PASSWORD": "password",
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        if tomllib is None:
            raise ConfigError("Need Python 3.11+ or `pip install tomli`")
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except OSError as e:
            raise ConfigError("Cannot read config: %s" % e)
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise ConfigError("Unknown config keys: %s" % ", ".join(sorted(unknown)))
        cfg.update(data)
        if (cfg["token"] or cfg["password"]) and os.stat(path).st_mode & 0o077:
            log.warning("%s holds secrets but is group/world accessible; chmod 600 it", path)
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


SUPPORTED_REVISIONS = ("V2", "V3", "V4", "b_V4")


def validate_config(cfg, need_panel, need_server=True):
    if need_server and not cfg["base_url"]:
        raise ConfigError("base_url is not set")
    if need_panel and cfg["epd_revision"] not in SUPPORTED_REVISIONS:
        raise ConfigError(
            "epd_revision is unset or unsupported (%r). Set it to one of %s "
            "(revision is printed on the back of the HAT)"
            % (cfg["epd_revision"], ", ".join(SUPPORTED_REVISIONS))
        )


# ---------------------------------------------------------------- SilverBullet client


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect to the login page must not be mistaken for page content.
    def redirect_request(self, *args, **kwargs):
        return None


class SilverBulletClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        handlers = [_NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if self.base.startswith("https://"):
            ctx = ssl.create_default_context(cafile=cfg["ca_file"] or None)
            if not cfg["verify_tls"]:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.logged_in = False

    def _request(self, method, path, data=None, headers=None):
        h = {"User-Agent": "sb-eink/1"}
        if self.cfg["token"]:
            h["Authorization"] = "Bearer " + self.cfg["token"]
        if self.cfg["send_sync_mode"]:
            h["X-Sync-Mode"] = "true"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=self.cfg["timeout"]) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400 or e.code in (401, 403):
                raise AuthError()
            if e.code == 404:
                raise PageNotFound()
            if e.code == 412:
                raise ConflictError()
            raise SBError("HTTP %d" % e.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise NetworkError() from e

    def _login(self):
        form = urllib.parse.urlencode(
            {"username": self.cfg["username"], "password": self.cfg["password"]}
        ).encode()
        body, _ = self._request(
            "POST", "/.auth", form, {"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            ok = json.loads(body).get("status") == "ok"
        except ValueError:
            ok = False
        if not ok:
            raise AuthError()
        self.logged_in = True

    def _fs(self, method, data=None, headers=None):
        c = self.cfg
        page = c["page"] if c["page"].endswith(".md") else c["page"] + ".md"
        path = c["fs_prefix"] + "/" + urllib.parse.quote(page)
        cookie_mode = not c["token"] and c["username"]
        if cookie_mode and not self.logged_in:
            self._login()
        try:
            return self._request(method, path, data, headers)
        except AuthError:
            if not cookie_mode:
                raise
            self.logged_in = False
            self._login()  # session may have expired: retry once
            return self._request(method, path, data, headers)

    def get_page(self):
        body, hdrs = self._fs("GET")
        etag = hdrs.get("ETag") if self.cfg["use_if_match"] else None
        return body, etag

    def put_page(self, data, etag):
        headers = {"Content-Type": "text/markdown"}
        if etag:
            headers["If-Match"] = etag
        self._fs("PUT", data, headers)


class LocalPage:
    """Same interface as the client, backed by a file (for --page-file)."""

    def __init__(self, path):
        self.path = path

    def get_page(self):
        with open(self.path, "rb") as f:
            return f.read(), None

    def put_page(self, data, etag):
        with open(self.path, "wb") as f:
            f.write(data)


def decode(b):
    return b.decode("utf-8", "surrogateescape")


def check_off(source, tag, text, dry_run=False):
    """Fetch fresh, check off one task, write back. Returns the new page text."""
    body, etag = source.get_page()
    old = decode(body)
    new = toggle_task(old, text, tag)
    if dry_run:
        for line in difflib.unified_diff(split_lines(old), split_lines(new), "a/page", "b/page"):
            print(line.rstrip("\n"))
        return new
    if etag is None:
        # No conditional write: re-read right before the PUT (small race remains).
        again, _ = source.get_page()
        if again != body:
            raise ConflictError()
    source.put_page(new.encode("utf-8", "surrogateescape"), etag)
    return new


# ---------------------------------------------------------------- cache


def save_cache(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched_at": time.time(), "text": text}, f)
    os.replace(tmp, path)


def load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)["text"]
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- rendering


@lru_cache(maxsize=None)
def load_font(path, size):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


def wrap(text, font, width):
    lines, cur = [], ""
    for word in text.split():
        while font.getlength(word) > width:  # break over-long words
            n = len(word)
            while n > 1 and font.getlength(word[:n]) > width:
                n -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:n])
            word = word[n:]
        trial = (cur + " " + word).strip()
        if cur and font.getlength(trial) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def fit(text, font_path, width, height):
    top = 9
    while top < 40 and load_font(font_path, top + 1).getlength("n" * CHARS_PER_LINE) <= width:
        top += 1
    for size in range(top, 8, -1):
        font = load_font(font_path, size)
        lines = wrap(text, font, width)
        lh = int(size * 1.2)
        if len(lines) * lh <= height:
            return font, lines, lh
    max_lines = max(1, height // lh)
    lines = lines[:max_lines]
    lines[-1] = lines[-1][:-1] + "\u2026"
    return font, lines, lh


def render(cfg, task, idx, total, offline, notice):
    from PIL import Image, ImageDraw

    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    fp = cfg["font_path"]
    small = load_font(fp, 12)
    d.line([(0, H - FOOTER_H), (W, H - FOOTER_H)], fill=0)
    left = " ".join(s for s in ("OFFLINE" if offline else "", notice) if s)
    right = "%d/%d" % (idx + 1, total) if total else ""
    d.text((W - 4 - d.textlength(right, font=small), H - FOOTER_H + 1), right, font=small, fill=0)
    d.text((4, H - FOOTER_H + 1), left, font=small, fill=0)  # may overlap the right text if long

    body = task.display if task else (notice or "No tasks")
    font, lines, lh = fit(body, fp, W - 8, H - FOOTER_H - 6)
    y = (H - FOOTER_H - len(lines) * lh) // 2
    for line in lines:
        d.text((4, y), line, font=font, fill=0)
        y += lh
    if cfg["rotate180"]:
        img = img.rotate(180)
    return img


# ---------------------------------------------------------------- panels


class PngPanel:
    def __init__(self, path):
        self.path = path

    def show(self, img, full):
        img.save(self.path)
        log.info("wrote %s (%s refresh)", self.path, "full" if full else "partial")


class EpdPanel:
    """Waveshare 2.13" panel. Sequences taken from waveshare_epd/epd2in13_*.py."""

    MODULES = {"V2": "epd2in13_V2", "V3": "epd2in13_V3", "V4": "epd2in13_V4", "b_V4": "epd2in13b_V4"}
    PARTIAL = ("V2", "V3", "V4")  # tri-color b_V4 has full refresh only

    def __init__(self, rev):
        import importlib

        self.rev = rev
        try:
            self.epd = importlib.import_module("waveshare_epd." + self.MODULES[rev]).EPD()
            self.epdconfig = importlib.import_module("waveshare_epd.epdconfig")
        except ImportError as e:
            raise ConfigError("Waveshare driver missing (%s); see README (PYTHONPATH)" % e)
        self.have_base = False

    def show(self, img, full):
        epd, rev = self.epd, self.rev
        buf = epd.getbuffer(img)  # rotates 250x122 -> 122x250 for the native portrait panel
        if rev == "b_V4":
            epd.init()
            epd.display(buf, [0xFF] * len(buf))  # blank red/yellow plane
        elif full or not self.have_base:
            epd.init(epd.FULL_UPDATE) if rev == "V2" else epd.init()
            epd.displayPartBaseImage(buf)  # full refresh; also loads the base for partials
            self.have_base = True
        elif rev == "V2":
            epd.init(epd.PART_UPDATE)
            epd.displayPartial(buf)
        else:
            # displayPartial() pulses RST itself; it assumes RAM survived deep sleep.
            self.epdconfig.module_init()
            epd.displayPartial(buf)
        epd.sleep()  # deep sleep + PWR pin off


# ---------------------------------------------------------------- app


class App:
    def __init__(self, cfg, source, panel, dry_run=False, use_cache=True):
        self.cfg, self.source, self.panel = cfg, source, panel
        self.dry_run, self.use_cache = dry_run, use_cache
        self.page = None
        self.tasks = []
        self.idx = 0
        self.offline = False
        self.notice = ""
        self.sig = None
        self.updates = 0
        self.rendered_at = 0.0

    def set_page(self, text, keep=True):
        cur = self.current()
        self.page = text
        self.tasks = parse_tasks(text, self.cfg["tag"])
        if keep and cur in self.tasks:
            self.idx = self.tasks.index(cur)
        self.idx = min(self.idx, max(0, len(self.tasks) - 1))

    def current(self):
        return self.tasks[self.idx] if self.idx < len(self.tasks) else None

    def refresh(self):
        try:
            body, _ = self.source.get_page()
        except SBError as e:
            log.warning("refresh failed: %s", e)
            self.offline = True
            self.notice = "" if isinstance(e, NetworkError) else e.short
            if self.page is None and self.use_cache:
                cached = load_cache(self.cfg["cache_file"])
                if cached is not None:
                    self.set_page(cached)
            return
        text = decode(body)
        self.offline, self.notice = False, ""
        self.set_page(text)
        if self.use_cache:
            try:
                save_cache(self.cfg["cache_file"], text)
            except OSError as e:
                log.warning("cache write failed: %s", e)

    def render(self):
        task = self.current()
        sig = (task, self.idx, len(self.tasks), self.offline, self.notice)
        if sig == self.sig:
            return
        every = self.cfg["full_refresh_every"]
        full = every <= 1 or self.updates % every == 0
        img = render(self.cfg, task, self.idx, len(self.tasks), self.offline, self.notice)
        try:
            self.panel.show(img, full)
        except Exception:
            log.exception("display update failed")
            return
        self.updates += 1
        self.sig = sig
        self.rendered_at = time.monotonic()

    def done(self):
        task = self.current()
        if not task:
            return
        if self.offline:
            self.notice = "offline: no done"
            return
        old_idx = self.idx
        try:
            new = check_off(self.source, self.cfg["tag"], task.text, self.dry_run)
        except SBError as e:
            log.warning("done failed: %s", e)
            if isinstance(e, NetworkError):
                self.offline = True
            elif isinstance(e, (TaskNotFound, ConflictError)):
                self.refresh()
            self.notice = e.short
            return
        self.notice = ""
        self.idx = old_idx
        self.set_page(new, keep=False)
        if self.use_cache and not self.dry_run:
            try:
                save_cache(self.cfg["cache_file"], new)
            except OSError:
                pass

    def handle(self, name, ts):
        n = len(self.tasks)
        self.notice = ""
        if name == "next" and n:
            self.idx = (self.idx + 1) % n
        elif name == "prev" and n:
            self.idx = (self.idx - 1) % n
        elif name == "done":
            if ts < self.rendered_at and not self.dry_run:  # pressed while the screen was stale/busy
                log.info("ignoring stale done press")
                return
            if self.dry_run and self.current():
                print("DONE:", self.current().display, flush=True)
            self.done()

    def run(self, events):
        self.refresh()
        self.render()
        next_poll = time.monotonic() + self.cfg["poll_interval"]
        while True:
            try:
                name, ts = events.get(timeout=max(0.0, next_poll - time.monotonic()))
            except queue.Empty:
                self.refresh()
                self.render()
                gap = self.cfg["retry_interval"] if self.offline else self.cfg["poll_interval"]
                next_poll = time.monotonic() + gap
                continue
            if name == "quit":
                return
            try:
                self.handle(name, ts)
                while True:  # coalesce presses that queued up during a refresh
                    try:
                        name, ts = events.get_nowait()
                    except queue.Empty:
                        break
                    if name == "quit":
                        return
                    self.handle(name, ts)
                self.render()
            except Exception:
                log.exception("event handling failed")


# ---------------------------------------------------------------- inputs


def start_buttons(cfg, events):
    from gpiozero import Button

    buttons = []
    for name, key in (("next", "pin_next"), ("prev", "pin_prev"), ("done", "pin_done")):
        b = Button(cfg[key], pull_up=True, bounce_time=cfg["debounce"])
        b.when_pressed = lambda n=name: events.put((n, time.monotonic()))
        buttons.append(b)
    return buttons  # keep references alive


def start_stdin_keys(events):
    keys = {"n": "next", "p": "prev", "d": "done", "q": "quit"}

    def loop():
        for line in sys.stdin:
            name = keys.get(line.strip()[:1])
            if name:
                events.put((name, time.monotonic()))
        events.put(("quit", 0))

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=None, help="TOML config (default: ./config.toml)")
    ap.add_argument("--dry-run", "--simulate", dest="dry_run", action="store_true",
                    help="no hardware, no writes: render to PNG, print the PUT diff, keys n/p/d/q on stdin")
    ap.add_argument("--png", default="eink.png", help="PNG output path for --dry-run")
    ap.add_argument("--page-file", help="read the page from a local file instead of the server")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        path = args.config or ("config.toml" if os.path.exists("config.toml") else None)
        cfg = load_config(path)
        validate_config(cfg, need_panel=not args.dry_run, need_server=not args.page_file)
        source = LocalPage(args.page_file) if args.page_file else SilverBulletClient(cfg)
        panel = PngPanel(args.png) if args.dry_run else EpdPanel(cfg["epd_revision"])
    except SBError as e:
        log.error("%s", e)
        return 2

    events = queue.Queue()
    signal.signal(signal.SIGTERM, lambda *_: events.put(("quit", 0)))
    if args.dry_run:
        start_stdin_keys(events)
    else:
        buttons = start_buttons(cfg, events)  # noqa: F841
    App(cfg, source, panel, dry_run=args.dry_run, use_cache=not args.page_file).run(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
