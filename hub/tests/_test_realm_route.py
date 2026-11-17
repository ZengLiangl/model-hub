# -*- coding: utf-8 -*-
"""_test_realm_route.py —— 合并层「没号的区不进候选链 / 清单外模型按账号挑区」。

由来（线上实测，2026-11-17 20:27 的 route.jsonl）：
    model=deepseek-v4.1-flash status=503
    chain=[wb:cn 502, trae:cn 502, wb:intl 503]
客户端看到的却是最后那一跳的话：
    "no usable account for realm 'intl'；该区域没有任何账号"
—— 于是人去面板加国际区的号，而真实原因是国内区那两跳的上游 502。
更常见的一种：客户端把模型名拼成 deepseek-v4.1-flashdeepseek-v4.1-flash（清单外的
名字），hub 退回 (默认引擎, None)，wb 用它自己的默认区（面板开关停在 intl，号却在
cn）-> 又是那句 503。

本套件用一个 stub 引擎复刻这台机器的真实形状（默认区 intl、只有 cn 号、两区目录里
都有模型），钉住三件事：
  G01 有号的区先试，**链里不出现没号的 (wb,intl) 那一跳**；全失败时报的是国内区
      的真实错误（502），不再是「该区域没有任何账号」；
  G02 清单外的模型名：hub 给引擎的 realm 是 **cn**（有号的那个），于是引擎能回
      真实原因（404 model not found），不再被区的账号问题掩盖；
  G03 阳性对照：账号表未知时老行为不变（(engine, None)，交给引擎自己定）。

口径与 _test_merged_fallback.py 一致：harness 的 check()，自报 SUMMARY 行；
stub 与 portal 都跑在本进程的守护线程上，结束 shutdown + server_close。
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

_PORTAL = os.path.join(P.HUB, "core", "portal.py")
if not os.path.exists(_PORTAL):
    H.skip("G01 hub/core/portal.py 尚未落地 —— 区级路由判据整体跳过", _PORTAL)
    H.finish()

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import portal, settings, router

MODEL = "glm-5.3-flash"
UNKNOWN = "glm-5.3-flashglm-5.3-flash"     # 实测那种被客户端拼重了的模型名
MERGED_KEY = "mrg-realm-route-test-001"


class _Stub(BaseHTTPRequestHandler):
    """复刻 wb 的真实形状：默认区 intl、唯一的号在 cn、两区目录都有模型。

    chat 的返回按 (realm, model) 决定：
      · realm=intl          -> 503 no usable account（这台机器上 intl 真的没号）
      · realm=cn + 已知模型 -> 502 upstream gateway error（线上那两跳的真实回包）
      · realm=cn + 别的名字 -> 404 model not found（模型名校验的真实回包）
      · 没有 realm          -> 503 且照样说 intl（= 老实现走的那条路）
    """
    server_version = "stub-realm"

    def log_message(self, *a):
        pass

    def _send(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True, "version": "stub", "realm": "intl",
                                    "accounts": 1, "accounts_ready": 1,
                                    "accounts_by_realm": {"cn": 1},
                                    "accounts_ready_by_realm": {"cn": 1},
                                    "accounts_ready_current_realm": 0})
        if path in ("/v1/models", "/models"):
            # 目录：两区都列出同一个模型（和真引擎一样，与有没有号无关）
            return self._send(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "stub"}]})
        return self._send(404, {"error": "stub: no such path " + path})

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not self.path.split("?")[0].endswith("/chat/completions"):
            return self._send(404, {"error": "stub: no such path"})
        realm = (parse_qs(urlparse(self.path).query).get("realm") or [None])[0]
        try:
            model = str((json.loads(raw.decode("utf-8", "replace")) or {}).get("model") or "")
        except Exception:
            model = ""
        seen = [realm] if realm else []
        if realm == "intl" or realm is None:
            return self._send(503, {"error": {
                "message": "no usable account for realm 'intl': every candidate is "
                           "disabled, cooling down, expired, out of upstream quota or "
                           "parked by a local limit；该区域没有任何账号",
                "type": "server_error", "code": 503}, "realm_seen": seen,
                "realm_header": self.headers.get("X-Realm")},
                extra=[("X-WB-Realm", "intl")])
        if model == MODEL:
            return self._send(502, {"error": {
                "message": "upstream gateway error (stub)",
                "type": "server_error", "code": 502}, "realm_seen": seen,
                "realm_header": self.headers.get("X-Realm")},
                extra=[("X-WB-Realm", "cn")])
        return self._send(404, {"error": {
            "message": "model %r not found (stub)" % model,
            "type": "invalid_request_error", "code": "model_not_found"},
            "realm_seen": seen,
            "realm_header": self.headers.get("X-Realm")}, extra=[("X-WB-Realm", "cn")])


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if data:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _chain(headers):
    raw = headers.get("X-Hub-Route-Attempts") or headers.get("x-hub-route-attempts")
    return json.loads(raw) if raw else []


_tmp = tempfile.mkdtemp(prefix="hub-realm-route-")
_saved_env = os.environ.get("HUB_SETTINGS")
os.environ["HUB_SETTINGS"] = os.path.join(_tmp, "settings.json")
servers = []
try:
    settings.save({
        "panel_password_default": True,
        "merged_keys": [{"id": "kmr", "name": "realm-route", "key": MERGED_KEY,
                         "enabled": True}],
        "routing": {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"},
    })
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    servers.append(stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    prtl = portal.PortalServer(
        ("127.0.0.1", 0), portal.PortalHandler,
        ports={"wb": stub.server_address[1]},
        admin_dir=_tmp, default_engine="wb", proxy_timeout=15, quiet=True)
    servers.append(prtl)
    threading.Thread(target=prtl.serve_forever, daemon=True).start()
    port = prtl.server_address[1]
    time.sleep(0.25)

    # ---------- G01 已知模型：没号的区不进链，报真实错误 ----------
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    chain = _chain(headers)
    H.eq("G01 已知模型：全链失败时报国内区的真实回包（502），不是 503", status, 502)
    H.check("G01b 链里没有 (wb,intl) 那一跳（那里一个号都没有）",
            not any(a.get("engine") == "wb" and a.get("realm") == "intl" for a in chain), chain)
    H.eq("G01c 唯一那一跳就是 wb:cn", [(a.get("engine"), a.get("realm")) for a in chain],
         [("wb", "cn")])
    H.check("G01d 客户端看到的消息不再是「该区域没有任何账号」",
            "no usable account" not in body and "upstream gateway error" in body, body[:200])

    # ---------- G02 清单外的模型名：按账号挑区（cn），交给引擎说真实原因 ----------
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions",
        {"model": UNKNOWN, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    doc = json.loads(body) if body else {}
    H.eq("G02 清单外模型 -> 引擎收到的 realm 是 cn（不是 None/默认区 intl）",
         doc.get("realm_seen"), ["cn"])
    H.eq("G02b 于是引擎能回真实原因（404 model not found），不再被区的账号问题掩盖",
         status, 404)
    H.check("G02c 404 里带着模型名（客户端能自查是拼重了）",
            UNKNOWN in body, body[:200])

    # ---------- G04 单引擎路径（/api/wb/...）：客户端没钉区时不许撞空出口 ----------
    # 这一条是「4ms 就回 503」的正身：wb 的区开关停在 intl、号全在 cn。
    status, headers, body = _request(
        port, "POST", "/api/wb/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
    H.eq("G04 单引擎路径不带 realm -> 引擎收到的是 cn（有号的那个区）",
         json.loads(body).get("realm_seen") if body.startswith("{") else None, ["cn"])
    H.check("G04b 于是不是那句「该区域没有任何账号」（拿到的是国内区的真实回包）",
            status == 502 and "no usable account" not in body, "%s %s" % (status, body[:160]))
    # 显式钉区仍然照办：不覆盖客户端的选择（钉了 intl 就还是 intl）
    status, headers, body = _request(
        port, "POST", "/api/wb/v1/chat/completions?realm=intl",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
    H.eq("G04c 客户端显式钉 intl -> 仍然按 intl 发（不擅改）",
         json.loads(body).get("realm_seen") if body.startswith("{") else None, ["intl"])
    H.eq("G04d 钉 intl 且 intl 真没号 -> 如实 503（这条错误在这里是对的）", status, 503)
    # X-Realm 头同样算「钉了」
    status, headers, body = _request(
        port, "POST", "/api/wb/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"X-Realm": "intl"})
    _d = json.loads(body) if body.startswith("{") else {}
    H.eq("G04e X-Realm 头也算钉区 -> 不额外钉 query、头原样到达引擎",
         (_d.get("realm_seen"), _d.get("realm_header")), ([], "intl"))

    # ---------- G05 判定本身（纯单元）----------
    class _SnapR(object):
        def __init__(self, cur, by_realm):
            self.models = {}
            self.ready = {}
            self.accounts = {"wb": 1}
            self.accounts_by_realm = {"wb": by_realm}
            self.current_realm = {"wb": cur}

    H.eq("G05 当前出口没号、另一区有号 -> 改道到有号的那个区",
         router.realm_for_unpinned(_SnapR("intl", {"cn": 1}), "wb", {"prefer_realm": "cn"}), "cn")
    H.eq("G05b 当前出口有号 -> 不动（尊重运营者选的出口）",
         router.realm_for_unpinned(_SnapR("intl", {"intl": 2, "cn": 1}), "wb",
                                   {"prefer_realm": "cn"}), None)
    H.eq("G05c 引擎没报按区表（qd/trae）-> 不动",
         router.realm_for_unpinned(_SnapR("intl", None), "wb", {"prefer_realm": "cn"}), None)
    H.eq("G05d 引擎没报当前出口 -> 不动",
         router.realm_for_unpinned(_SnapR(None, {"cn": 1}), "wb", {"prefer_realm": "cn"}), None)
    H.eq("G05e 两个区都没号 -> 不动（没有更好的去处）",
         router.realm_for_unpinned(_SnapR("intl", {"intl": 0, "cn": 0}), "wb",
                                   {"prefer_realm": "cn"}), None)

    # ---------- G03 阳性对照：账号表未知时，老行为不变 ----------
    class _Snap(object):
        models = {}
        ready = {}
        accounts = {}
        accounts_by_realm = {}

    H.eq("G03 阳性对照：账号表未知 -> 兜底链仍是 (engine, None)（交给引擎自己定）",
         router.fallback_candidates(_Snap(), {"prefer_realm": "cn"}, "wb"), [("wb", None)])
    # 同一条兜底链（不给 realm）在 stub 上就是那句 503 —— 说明 G02 的绿确实是
    # 「按账号挑区」带来的，不是 stub 本来就很客气。
    _s, _h, _b = _request(
        stub.server_address[1], "POST", "/v1/chat/completions",
        {"model": UNKNOWN, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
    H.check("G03b 对照：直连引擎不给 realm（老 hub 的 (engine,None)）-> 仍是那句 503",
            _s == 503 and "no usable account" in _b, _b[:160])
    # 显式钉 intl 但 intl 没号：尊重首选、不把路走死 -> 退到 cn，拿到真实的 404
    status, headers, body = _request(
        port, "POST", "/api/v1/chat/completions?realm=intl",
        {"model": UNKNOWN, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
        {"Authorization": "Bearer " + MERGED_KEY})
    H.check("G03c 钉 intl 但 intl 没号 -> 退到 cn 拿到真实 404（不是 503 加号提示）",
            status == 404 and "no usable account" not in body, body[:160])
finally:
    for srv in servers:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    if _saved_env is None:
        os.environ.pop("HUB_SETTINGS", None)
    else:
        os.environ["HUB_SETTINGS"] = _saved_env

H.finish()
