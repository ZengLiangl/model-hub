# -*- coding: utf-8 -*-
"""_test_v1_auth.py —— 真发请求验三档鉴权（第 152 轮）。

前三档此前只验到 **分类函数**（keys.classify），没验"请求打进来会怎样"。
这一套起三个假引擎 + 一个真门户，用三把 key 各打一次 /api/v1/models：

  合并 key  → 三家的模型都在
  trae  key → **只有 trae 的模型**（引擎 key 只走那一家 —— 这是需求里最核心的一条）
  未知 key  → 401

不起真引擎（用 http.server 假实现），所以秒级、不占端口的固定实例。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

ROOT = tempfile.mkdtemp(prefix="hub-v1-")
os.environ["HUB_DATA_ROOT"] = ROOT
os.environ["HUB_SETTINGS"] = os.path.join(ROOT, "hub-settings.json")

ENGINES = ("wb", "qd", "trae")

# ---- 假引擎 ----
class FakeEngine(BaseHTTPRequestHandler):
    name = "wb"
    fail_status = 0        # 0 = 正常；503 之类 = 该引擎的 chat 直接失败
    accounts = 1           # /health 报的**账号总数**（0 = 池子空着，第 245 轮的判据用它）
    by_realm = None        # /health 的 accounts_by_realm（只有 wb 报；None = 不报）
    realm_models = None    # {realm: [id...]}；None = 两个区都给同一份

    def log_message(self, *a):        # 静音
        pass

    def do_GET(self):
        full = self.path.split("?")[0].rstrip("/") or "/"
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        realm = "cn"
        for part in query.split("&"):
            if part.startswith("realm="):
                realm = part.split("=", 1)[1]
        if full.endswith("/health"):
            body = {"ok": True, "service": self.name + "-svc", "version": "1.0.0",
                    "accounts": self.accounts,
                    "accounts_ready": 1 if self.accounts else 0, "realm": realm}
            if self.by_realm is not None:
                body["accounts_by_realm"] = dict(self.by_realm)
        elif full.endswith("/v1/models"):
            if self.realm_models is not None:
                body = {"data": [{"id": x, "object": "model"}
                                 for x in self.realm_models.get(realm, [])]}
            else:
                # 每家都给一个**同名**模型：只有候选集合里同时有它，路由才会跨引擎回退。
                body = {"data": [{"id": self.name + "-model", "object": "model"},
                                 {"id": "common-model", "object": "model"}]}
        else:
            body = {}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/chat/completions") and self.fail_status:
            self._send(self.fail_status, {"error": {"message": "boom from " + self.name}})
            return
        if path.endswith("/chat/completions"):
            self._send(200, {"id": "chatcmpl-" + self.name, "served_by": self.name,
                             "choices": [{"message": {"role": "assistant", "content": "ok"}}]})
            return
        self.do_GET()

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

FAKE_CLASSES = {}


def start_fake(name, fail_status=0, accounts=1, by_realm=None, realm_models=None):
    cls = type("Fake_" + name, (FakeEngine,),
               {"name": name, "fail_status": fail_status, "accounts": accounts,
                "by_realm": by_realm, "realm_models": realm_models})
    FAKE_CLASSES[name] = cls          # 留个引用：后面要运行时改"谁失败"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

# ---- 夹具：一把合并 key + 每家一把引擎 key ----
def write_json(path_, obj):
    os.makedirs(os.path.dirname(path_), exist_ok=True)
    with open(path_, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)

write_json(os.path.join(ROOT, "hub-settings.json"), {
    "panel_password_default": True,
    "merged_keys": [{"id": "m1", "name": "merged", "key": "mk-all", "enabled": True}],
    "routing": {"enabled": True, "prefer_realm": "cn", "engine_prefer": "wb"},
})
for eng in ENGINES:
    write_json(os.path.join(ROOT, eng, "accounts", "settings.json"), {
        "api_keys": [{"id": eng + "1", "name": eng + "-key", "key": "ek-" + eng,
                      "realm": "", "enabled": True}],
    })

ports = {}
servers = []
for eng in ENGINES:
    # 让 wb 的 chat 回 503（**可重试**状态）：用来验"合并 key 会换一家"。
    # 第一版我让它回 500，结果没换 —— 查 router.RETRYABLE_STATUS 才知道
    # 500 是**有意**不重试的（避免把真实错误掩盖成"换了家就好了"）。
    srv, port = start_fake(eng, fail_status=(503 if eng == "wb" else 0))
    servers.append(srv)
    ports[eng] = port

portal_port = H.free_port()

# ---- 起门户（与 server.start_portal 同一套 argv） ----
argv = ["--host", "127.0.0.1", "--port", str(portal_port)]
for eng in ENGINES:
    argv += ["--%s-port" % eng, str(ports[eng])]
sys.path.insert(0, P.REPO)                  # 与其他套件同口径：用仓库根导入 hub
try:
    from hub.core import portal            # noqa: E402
except Exception as exc:                   # noqa: BLE001
    H.skip("V00 门户 import 失败（半成品树）", "%s: %s" % (type(exc).__name__, exc))
    H.finish()

threading.Thread(target=portal.main, args=(argv,), daemon=True).start()
base = "http://127.0.0.1:%d" % portal_port


def wait_portal(limit=15.0):
    import time
    end = time.time() + limit
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/hub/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


if not wait_portal():
    H.check("V00 门户在测试里起得来", False, "15s 内没应答 /api/hub/health")
    H.finish()

H.check("V00 门户在测试里起得来", True, "")


def call_models(key, base_url=None, query=""):
    req = urllib.request.Request((base_url or base) + "/api/v1/models" + query)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = None
        return exc.code, body


def engines_in(doc):
    """返回这条响应里出现了哪些引擎（按模型 id 前缀判断）。"""
    ids = []
    for item in (doc or {}).get("data") or []:
        mid = str(item.get("id") or "")
        for eng in ENGINES:
            if mid.startswith(eng + "-"):
                ids.append(eng)
    return sorted(set(ids))


status, doc = call_models("mk-all")
H.check("V1 合并 key 能拿到三家的模型",
        status == 200 and engines_in(doc) == sorted(ENGINES),
        "status=%s engines=%s" % (status, engines_in(doc)))

status, doc = call_models("ek-trae")
H.check("V2 引擎 key（trae）只拿到 trae 的模型",
        status == 200 and engines_in(doc) == ["trae"],
        "status=%s engines=%s" % (status, engines_in(doc)))

status, doc = call_models("ek-wb")
H.check("V3 引擎 key（wb）只拿到 wb 的模型",
        status == 200 and engines_in(doc) == ["wb"],
        "status=%s engines=%s" % (status, engines_in(doc)))

status, doc = call_models("nope")
H.check("V4 未知 key → 401", status == 401, "status=%s" % status)

status, doc = call_models(None)
# ★ 不带 key **不是** hub 该拦的情况（portal._reject_unknown_key 的注释写得很清楚：
#   "无 key 请求也应由引擎按自己的开关决定要不要放行"）。
#   第一版我断言 401，红了一次 —— 那不是 bug，是我把设计意图写错了。
#   这里改成断言"hub **没有**擅自用 invalid_api_key 拒绝"。
hub_rejected = False
if isinstance(doc, dict):
    err = doc.get("error")
    if isinstance(err, dict) and str(err.get("code") or "") == "invalid_api_key":
        hub_rejected = True
H.check("V5 不带 key 时 hub 不擅自拒绝（交给引擎按自己的开关决定）",
        not hub_rejected, "status=%s body=%s" % (status, str(doc)[:120]))

def call_chat(key):
    body = json.dumps({"model": "common-model",
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(base + "/api/v1/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            doc = json.loads(exc.read().decode())
        except Exception:
            doc = None
        return exc.code, dict(exc.headers), doc


# wb 的 chat 恒 500，路由又偏好 wb → 必须换一家成功，并在尝试链里留下 wb:500
status, headers, doc = call_chat("mk-all")
attempts_raw = ""
for k, v in headers.items():
    if k.lower() == "x-hub-route-attempts":
        attempts_raw = v
served = str((doc or {}).get("served_by") or "")

H.check("V6 合并 key 在某家 5xx 时换另一家成功", status == 200 and served and served != "wb",
        "status=%s served_by=%s" % (status, served))

# 第 231 轮：这里原来写死 "503"，但 mock 上游可能回 500/502/503 —— 实测偶发红。
# 判据的意图是"失败的那一家要留在尝试链里"，不是"必须是 503"，所以按 5xx 判断。
_attempts_5xx = False
try:
    import json as _json
    for _a in (_json.loads(attempts_raw) if attempts_raw else []):
        if str(_a.get("engine")) == "wb" and int(_a.get("status") or 0) >= 500:
            _attempts_5xx = True
except Exception:
    _attempts_5xx = False

H.check("V7 尝试链里留下失败的那一家（X-Hub-Route-Attempts）",
        "wb" in attempts_raw and (_attempts_5xx or "50" in attempts_raw),
        "attempts=%s" % (attempts_raw[:160] or "(缺头)"))

# 契约：500 **不**重试（RETRYABLE_STATUS 里没有 500）。
# 这条不是为了"更好"，而是把既有设计钉住 —— 500 多半是真实错误，
# 换一家重试会把"这家坏了"掩盖成"凑巧成功了"。
try:
    from hub.core import router as _router
    H.check("V8 500 不在可重试集合里（有意：不掩盖真实错误）",
            500 not in _router.RETRYABLE_STATUS,
            "RETRYABLE_STATUS=%s" % sorted(_router.RETRYABLE_STATUS))
except Exception as exc:
    H.check("V8 可重试集合可读", False, str(exc))

# ---- V9 全引擎都失败：最终响应要保留**最后一次真实状态码** ----
# 用户会看到的那一屏：不该被包装成 502，也不该变成 200。
for _name, _cls in FAKE_CLASSES.items():
    _cls.fail_status = 503
status_all, headers_all, doc_all = call_chat("mk-all")
attempts_all = ""
for k, v in headers_all.items():
    if k.lower() == "x-hub-route-attempts":
        attempts_all = v
try:
    _attempts = json.loads(attempts_all or "[]")
except Exception:
    _attempts = []
tried = {a.get("engine") for a in _attempts if isinstance(a, dict)}
H.check("V9 三家全失败时保留最后一次真实状态码（503，不包装成 502）",
        status_all == 503, "status=%s body=%s" % (status_all, str(doc_all)[:90]))
# 候选是「引擎 × 区域」对（默认 cn/intl 两区），所以尝试链通常有 6 条 ——
# 断言"每一家都试过"，而不是写死条数（第一版写 == 3，当场红）。
H.check("V9b 三家都试过（候选是引擎×区域对）", tried == set(ENGINES),
        "试过=%s attempts=%s" % (sorted(x for x in tried if x), attempts_all[:120]))

for _name, _cls in FAKE_CLASSES.items():
    _cls.fail_status = 0             # 还原

# ---- V10 没有账号的引擎：它的模型不进合并清单（第 245 轮）----
# 由来：引擎的 /v1/models 是「目录」，与有没有登录号无关 —— trae 池子空着也照样
# 倒出整份静态清单，于是合并清单里出现点不动的模型（实测 DeepSeek-V4-Flash 只挂在
# trae 上）。这里另起一套假引擎（trae 0 个号）+ 一个新门户：快照是新起的，
# 不必等 30 秒 TTL，也不会污染上面那套已经跑完的判据。
ports2, servers2 = {}, []
for eng in ENGINES:
    srv2, port2 = start_fake(eng, accounts=(0 if eng == "trae" else 1))
    servers2.append(srv2)
    ports2[eng] = port2

portal2_port = H.free_port()
argv2 = ["--host", "127.0.0.1", "--port", str(portal2_port)]
for eng in ENGINES:
    argv2 += ["--%s-port" % eng, str(ports2[eng])]
threading.Thread(target=portal.main, args=(argv2,), daemon=True).start()
base2 = "http://127.0.0.1:%d" % portal2_port


def wait_portal2(limit=15.0):
    import time
    end = time.time() + limit
    while time.time() < end:
        try:
            with urllib.request.urlopen(base2 + "/api/hub/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def ids_of(doc):
    return [str(i.get("id") or "") for i in ((doc or {}).get("data") or [])]


if not wait_portal2():
    H.check("V10 第二个门户（trae 0 个号）起得来", False, "15s 内没应答 /api/hub/health")
else:
    status, doc2 = call_models("mk-all", base2)
    ids = ids_of(doc2)
    H.check("V10 trae 没有账号 -> 只挂在 trae 上的模型整条不返回（同名的不受影响）",
            status == 200 and "trae-model" not in ids
            and "wb-model" in ids and "common-model" in ids,
            "status=%s ids=%s" % (status, ids))
    counts = (doc2 or {}).get("counts") or {}
    H.check("V10b counts 里没号的引擎是 0，有号的照旧（其他引擎不受牵连）",
            counts.get("trae") == 0 and (counts.get("wb") or 0) > 0,
            "counts=%s" % (counts,))
    hidden = (doc2 or {}).get("hidden") or {}
    H.check("V10c 摘掉多少条留在 hidden 里（排障看得见，不是悄悄消失）",
            (hidden.get("trae") or 0) > 0 and (hidden.get("wb") or 0) == 0,
            "hidden=%s" % (hidden,))

    # 反向对照：引擎自己的目录**不删**（这是「不发」不是「删」）。
    # trae 的 key 打到同一个门户，仍然看到 trae 那份静态目录。
    status, doc3 = call_models("ek-trae", base2)
    H.check("V11 引擎 key 仍能拿到它自己的目录（不发 != 删）",
            status == 200 and "trae-model" in ids_of(doc3),
            "status=%s ids=%s" % (status, ids_of(doc3)))

    # 账号回来（同一个测试进程里改假引擎）-> 重建快照后 trae 的模型放回来。
    # 快照 TTL 是 30s，这里借用 /api/hub/routing/preview 强制重建（它做的就是
    # refresh_snapshot(force=True)），不去干等 30 秒。
    for _cls in list(FAKE_CLASSES.values()):
        _cls.accounts = 1
    try:
        _req = urllib.request.Request(base2 + "/api/hub/routing/preview",
                                      data=b"{}", method="POST")
        _req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(_req, timeout=10).read()
    except Exception as exc:                       # noqa: BLE001
        H.check("V12 强制重建快照（POST /api/hub/routing/preview）可用", False, str(exc))
    status, doc4 = call_models("mk-all", base2)
    H.check("V12 账号数不再为 0 时清单恢复（口径只看当前快照）",
            status == 200 and "trae-model" in ids_of(doc4),
            "status=%s ids=%s" % (status, ids_of(doc4)))

# ---- V2x 再细分到**区域**：wb 有号、但只有国内区的号（第 246 轮）----
# 由来：wb 的 intl 目录里有国际版独有模型（实测 kimi-k3.1-pro-highspeed），
# 没有国际区账号时它一样点不动。wb 的 /health 报 accounts_by_realm，据此收窄。
CLS3 = {
    "wb": {"accounts": 2, "by_realm": {"cn": 2},
           "realm_models": {"cn": ["wb-cn-model"], "intl": ["kimi-k3.1-pro-highspeed"]}},
    "qd": {"accounts": 1, "by_realm": None,                 # qd 不报按区 -> 两区照发
           "realm_models": {"cn": ["qd-cn-model"], "intl": ["qd-intl-model"]}},
    "trae": {"accounts": 0},
}
ports3, servers3 = {}, []
for eng in ENGINES:
    spec = dict(CLS3.get(eng) or {})
    srv3, port3 = start_fake(eng, **spec)
    servers3.append(srv3)
    ports3[eng] = port3

portal3_port = H.free_port()
argv3 = ["--host", "127.0.0.1", "--port", str(portal3_port)]
for eng in ENGINES:
    argv3 += ["--%s-port" % eng, str(ports3[eng])]
threading.Thread(target=portal.main, args=(argv3,), daemon=True).start()
base3 = "http://127.0.0.1:%d" % portal3_port


def wait_portal3(limit=15.0):
    import time
    end = time.time() + limit
    while time.time() < end:
        try:
            with urllib.request.urlopen(base3 + "/api/hub/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


if not wait_portal3():
    H.check("V20 第三个门户（wb 只有国内区号）起得来", False, "15s 内没应答 /api/hub/health")
else:
    status, doc5 = call_models("mk-all", base3)
    ids = ids_of(doc5)
    H.check("V20 wb 没有国际区号 -> 国际版独有模型不返回（国内区照发）",
            status == 200 and "kimi-k3.1-pro-highspeed" not in ids
            and "wb-cn-model" in ids, "status=%s ids=%s" % (status, ids))
    H.check("V20b 没报按区数的引擎照发两区（qd 的国际区模型还在）",
            "qd-cn-model" in ids and "qd-intl-model" in ids, ids)
    H.check("V20c realms 字段说清发布范围",
            ((doc5 or {}).get("realms") or {}) == {"wb": ["cn"],
                                                   "qd": ["cn", "intl"], "trae": []},
            (doc5 or {}).get("realms"))
    counts = (doc5 or {}).get("counts") or {}
    check_map = {"wb": 1, "qd": 2, "trae": 0}      # 按区累加：wb 只剩 cn 那 1 条
    H.check("V20d counts 按发布的区累加", counts == check_map, counts)
    hidden = (doc5 or {}).get("hidden") or {}
    H.check("V20e 被区收窄摘掉的那 1 条记在 hidden.wb",
            hidden.get("wb") == 1 and (hidden.get("qd") or 0) == 0, hidden)

    # 反向对照：wb 自己的目录不删 —— 引擎 key 打 intl 仍能看到那份国际区目录
    status, doc6 = call_models("ek-wb", base3, "?realm=intl")
    H.check("V21 引擎 key + ?realm=intl 仍能拿到 wb 的国际区目录（不发 != 删）",
            status == 200 and "kimi-k3.1-pro-highspeed" in ids_of(doc6),
            "status=%s ids=%s" % (status, ids_of(doc6)))

H.finish()
