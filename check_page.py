#!/usr/bin/env python3
"""Check that a SilverBullet page is readable with the settings sb_eink.py would use.

Usage: check_page.py http://192.168.1.50:3000/Tasks [-c config.toml]
Accepts a UI URL (.../Tasks) or a file URL (.../.fs/Tasks.md). Exit code 0 = OK.
"""
import argparse
import sys
import urllib.parse

import sb_eink as sb

HINTS = {
    sb.NetworkError: "server unreachable: check the URL/port, Wi-Fi, firewall, or https vs http",
    sb.AuthError: "auth rejected or redirected to login: set token (or username/password) in the config or SB_EINK_* env vars",
    sb.PageNotFound: "no such page: check the page name/case, or fs_prefix for multi-space setups",
    sb.ConflictError: "precondition failed (412)",
}


def split_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise sb.ConfigError("URL must start with http:// or https://")
    path = urllib.parse.unquote(u.path)
    if "/.fs/" in path:
        prefix, page = path.split("/.fs/", 1)
        return "%s://%s%s" % (u.scheme, u.netloc, prefix), page
    page = path.strip("/")
    if not page:
        raise sb.ConfigError("URL has no page name (expected e.g. http://host:3000/Tasks)")
    return "%s://%s" % (u.scheme, u.netloc), page


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="SilverBullet page URL")
    ap.add_argument("-c", "--config", help="TOML config for auth/TLS/tag settings")
    args = ap.parse_args(argv)

    try:
        cfg = sb.load_config(args.config)
        cfg["base_url"], cfg["page"] = split_url(args.url)
        client = sb.SilverBulletClient(cfg)
    except sb.SBError as e:
        print("FAIL: %s" % e)
        return 1

    print("Server: %s  Page: %s" % (cfg["base_url"], cfg["page"]))
    try:
        body, etag = client.get_page()
    except sb.SBError as e:
        reason = HINTS.get(type(e), "")
        cause = e.__cause__
        print("FAIL: %s" % e)
        if cause is not None:
            print("  detail: %s" % (getattr(cause, "reason", None) or cause))
        if reason:
            print("  reason: %s" % reason)
        return 1

    text = sb.decode(body)
    tasks = sb.parse_tasks(text)
    tagged = sb.parse_tasks(text, cfg["tag"]) if cfg["tag"] else tasks
    print("OK: read %d bytes, %d unchecked tasks, %d matching tag %r"
          % (len(body), len(tasks), len(tagged), cfg["tag"]))
    print("  ETag: %s" % (etag or "none returned (script will fall back to re-read before PUT)"))
    if not tasks:
        print("  note: page is readable but has no unchecked '- [ ]' tasks; check the page name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
