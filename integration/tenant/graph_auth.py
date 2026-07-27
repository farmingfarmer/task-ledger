"""Microsoft Graph auth for the home-tenant integration proof.

Two flows, stdlib only (no MSAL dependency to install on locked-down
machines):

  app_token(cfg)      client-credentials: the app acting as itself.
                      Used by the webhook host and mail subscriptions.
  user_token(cfg)     device-code: you sign in once in a browser, the
                      refresh token persists locally. Used by pull_chats
                      to read YOUR chats with delegated permissions --
                      the path that avoids Teams' protected-API gate.

Configuration comes from ~/.task-ledger/tenant.json:

    {
      "tenant_id":  "<directory (tenant) id>",
      "client_id":  "<application (client) id>",
      "client_secret": "<secret value -- app flows only>"
    }

Tokens cache to ~/.task-ledger/tokens.json (chmod 600). Nothing here ever
prints a token. Python 3.8 compatible.
"""

import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONF_DIR = os.path.expanduser("~/.task-ledger")
CONF_PATH = os.path.join(CONF_DIR, "tenant.json")
TOKEN_PATH = os.path.join(CONF_DIR, "tokens.json")
GRAPH = "https://graph.microsoft.com/v1.0"


def load_config():
    if not os.path.exists(CONF_PATH):
        sys.stderr.write(
            "missing {0} -- create it with tenant_id, client_id, "
            "client_secret (see TENANT_SETUP.md)\n".format(CONF_PATH))
        sys.exit(2)
    with open(CONF_PATH) as f:
        return json.load(f)


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError("token endpoint {0}: {1}".format(e.code,
                                                            detail[:300]))


def _cache_read():
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            return json.load(f)
    return {}


def _cache_write(cache):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump(cache, f)
    os.chmod(TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)


def app_token(cfg=None):
    """Client-credentials access token, cached until near expiry."""
    cfg = cfg or load_config()
    cache = _cache_read()
    ent = cache.get("app")
    if ent and ent["expires_at"] - 120 > time.time():
        return ent["access_token"]
    url = ("https://login.microsoftonline.com/{0}/oauth2/v2.0/token"
           .format(cfg["tenant_id"]))
    tok = _post_form(url, {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    })
    cache["app"] = {"access_token": tok["access_token"],
                    "expires_at": time.time() + int(tok["expires_in"])}
    _cache_write(cache)
    return cache["app"]["access_token"]


def user_token(cfg=None, scopes="Chat.Read User.Read offline_access"):
    """Delegated access token via device code; refresh token persisted so
    the browser dance happens once."""
    cfg = cfg or load_config()
    cache = _cache_read()
    ent = cache.get("user")
    base = ("https://login.microsoftonline.com/{0}/oauth2/v2.0"
            .format(cfg["tenant_id"]))
    if ent:
        if ent["expires_at"] - 120 > time.time():
            return ent["access_token"]
        if ent.get("refresh_token"):
            try:
                tok = _post_form(base + "/token", {
                    "client_id": cfg["client_id"],
                    "grant_type": "refresh_token",
                    "refresh_token": ent["refresh_token"],
                    "scope": scopes,
                })
                return _store_user(cache, tok)
            except RuntimeError:
                pass  # fall through to a fresh device-code dance
    dc = _post_form(base + "/devicecode", {
        "client_id": cfg["client_id"], "scope": scopes})
    print(dc["message"])   # "go to https://microsoft.com/devicelogin ..."
    deadline = time.time() + int(dc["expires_in"])
    while time.time() < deadline:
        time.sleep(int(dc["interval"]))
        try:
            tok = _post_form(base + "/token", {
                "client_id": cfg["client_id"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dc["device_code"],
            })
            return _store_user(cache, tok)
        except RuntimeError as e:
            if "authorization_pending" in str(e):
                continue
            raise
    raise RuntimeError("device code expired before sign-in completed")


def _store_user(cache, tok):
    cache["user"] = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token",
                                 cache.get("user", {}).get("refresh_token")),
        "expires_at": time.time() + int(tok["expires_in"]),
    }
    _cache_write(cache)
    return cache["user"]["access_token"]


def graph_get(path, token, params=None):
    """GET with paging support: returns the combined value list for
    collections, or the raw object for single resources."""
    url = path if path.startswith("http") else GRAPH + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    out = []
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "value" not in data:
            return data
        out.extend(data["value"])
        url = data.get("@odata.nextLink")
    return out


def graph_post(path, token, body):
    req = urllib.request.Request(
        GRAPH + path, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("graph {0} {1}: {2}".format(
            e.code, path, e.read().decode("utf-8", "replace")[:400]))


def graph_delete(path, token):
    req = urllib.request.Request(GRAPH + path, method="DELETE", headers={
        "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status
