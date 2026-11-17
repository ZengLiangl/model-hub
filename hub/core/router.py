#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub/core/router.py —— 合并模式智能路由。

一个合并 key 同时打国内/国际 × wb/qd 两引擎。路由矩阵由 portal 持有：

  * 快照（TTL 8s）：两引擎 × 两区域的模型清单（/v1/models?realm=X）+ 账号总数
    与总就绪账号数（/health）—— 本地回环探测，失败不致命（该引擎/区域不参与候选）。
  * 候选序列 candidates_for(model)：
      区域优先：prefer_realm（默认 cn）先，另一区兜底；
      区域内引擎顺序：engine_prefer=auto 时按就绪账号多者先（并列 wb 先），
      显式 wb/qd 时按指定顺序。
  * 兜底语义：portal 逐个尝试候选，503/429/502/504（未流式输出前）换下一个。
  * 合并模型清单（/v1/models）：同名模型合一条，带 engines 与 realm；
    **只有「有账号的 引擎 × 区域」才发布**（见 publishing_realms）。

只用标准库。
"""
import http.client
import json
import threading
import time

# 引擎名单**只从注册表来**（第 100 轮）。
from hub import ENGINES  # noqa: E402
REALMS = ("cn", "intl")

#: 合并路由只作用于这些聊天端点（+ /v1/models 合并清单）。
#: 存**去首斜杠**的形式 —— portal 传来的 rest 是 /api/ 之后剥掉前缀的路径。
CHAT_PATHS = frozenset(p.strip("/") for p in (
    "/v1/chat/completions", "/chat/completions",
    "/v1/completions", "/completions",
    "/v1/messages", "/messages",
    "/v1/responses", "/responses",
))
#: 未开始流式输出前的可重试状态（含 401：key 可能没注册到某台引擎，换一台）。
RETRYABLE_STATUS = frozenset((401, 429, 502, 503, 504))

SNAPSHOT_TTL = 30.0


class RouteSnapshot(object):
    """路由快照：models[engine][realm]=set(ids)；ready[engine]=int|None。

    accounts[engine] = 该引擎的账号**总数**（/health 的 accounts），
    None = 不知道（引擎没报这个字段 / /health 没答上来）—— 与 0 是两回事。

    accounts_by_realm[engine] = {realm: 该区账号数} 或 None（引擎没报这个字段）。
    目前只有 wb 的 /health 报 accounts_by_realm（qd/trae 只有总数）—— 于是
    「wb 有号但没有国际区号」能精确判，"qd 的国际区有没有号"只能按总数放过。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.at = 0.0
        self.models = {}
        self.ready = {}
        self.accounts = {}
        self.accounts_by_realm = {}
        #: 引擎当前的出口（/health 的 realm）—— 「客户端没钉区时它会用哪个区」。
        #: 面板上的区开关与号的实际归属经常对不上（实测 wb 开关 intl、号在 cn），
        #: 于是任何不带 realm 的请求都会撞上「该区域没有任何账号」。
        self.current_realm = {}
        self.errors = {}

    def is_fresh(self):
        with self.lock:
            return time.time() - self.at < SNAPSHOT_TTL


def _http(port, path, key=None, timeout=8.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + str(key)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, payload
    except OSError as exc:
        return 0, ("connection failed: %s" % exc).encode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _key_for(key, engine):
    """key 可以是**字符串**（两侧同一把）或**{engine: key} 字典**。

    字典是为了「合并 key 没在引擎里登记」的场景：hub 用各引擎自己的一把启用
    key 去抓清单，否则 /v1/models 会 401、快照空掉、候选退化成默认引擎。
    """
    if isinstance(key, dict):
        return key.get(engine)
    return key


def _int_or_none(value):
    """能转成 int 就转，转不了（缺字段 / None / 非数）就给 None。

    「不知道」与「0」必须分开：0 个账号是**确定的事实**（这台引擎没接任何号，
    模型不该对外发布），不知道只能按老行为放过（见 publishing_realms）。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def refresh_snapshot(ports, key, snapshot, force=False):
    """按 TTL 重建快照；并发调用只让一个线程真正探测。"""
    if not force and snapshot.is_fresh():
        return snapshot
    models, ready, accounts, by_realm, errors = {}, {}, {}, {}, {}
    current = {}
    for engine in ENGINES:
        port = (ports or {}).get(engine)
        if not port:
            continue
        models[engine], ready[engine], accounts[engine] = {}, None, None
        by_realm[engine], errors[engine] = None, None
        current[engine] = None
        status, body = _http(port, "/health")
        if status == 200:
            try:
                doc = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                doc = None
            if isinstance(doc, dict):
                # ready 保留老口径：缺字段就是 0（老代码就是这么算排序权重的）。
                ready[engine] = _int_or_none(doc.get("accounts_ready")) or 0
                # ★ 账号**总数**（第 245 轮）：0 个号 = 这台引擎其实什么都没接，
                #   但它的 /v1/models 照样倒出静态目录 —— 发布清单时要整段摘掉
                #   （见 publishing_realms / merged_model_list）。
                accounts[engine] = _int_or_none(doc.get("accounts"))
                # ★ 按区账号数（第 246 轮）：只有 wb 报这个字段。有了它才能判
                #   「有号，但**没有国际区**的号」—— 于是 wb intl 独有模型
                #   （实测 kimi-k3.1-pro-highspeed）不该出现在清单里。
                by_realm[engine] = _realm_counts(doc.get("accounts_by_realm"))
                _cur = str(doc.get("realm") or "").strip().lower()
                current[engine] = _cur if _cur in REALMS else None
            else:
                errors[engine] = "health parse error"
        else:
            errors[engine] = "health %s" % status
        for realm in REALMS:
            status, body = _http(port, "/v1/models?realm=" + realm,
                                 key=_key_for(key, engine))
            ids = set()
            if status == 200:
                try:
                    doc = json.loads(body.decode("utf-8", "replace"))
                    for item in doc.get("data") or []:
                        mid = str(item.get("id") or "").strip().lower()
                        if mid:
                            ids.add(mid)
                except Exception:
                    errors[engine] = "models parse error"
            else:
                errors[engine] = "models %s %s" % (realm, status)
            models[engine][realm] = ids
    with snapshot.lock:
        snapshot.models = models
        snapshot.ready = ready
        snapshot.accounts = accounts
        snapshot.accounts_by_realm = by_realm
        snapshot.current_realm = current
        snapshot.errors = errors
        snapshot.at = time.time()
    return snapshot


def _realm_counts(value):
    """把 /health 的 accounts_by_realm 收成 {realm: int}；形状不对就给 None。

    None 与 {} 是两回事：None = 这家引擎**没报**按区计数（qd/trae 就是这样），
    {} = 报了，但一个区都没有号。
    """
    if not isinstance(value, dict):
        return None
    out = {}
    for key, raw in value.items():
        n = _int_or_none(raw)
        out[str(key).strip().lower()] = 0 if n is None else n
    return out


def engine_holds(snapshot, engine, realm, model):
    return model in ((snapshot.models.get(engine) or {}).get(realm) or set())


def engine_accounts(snapshot, engine):
    """该引擎的账号总数；None = 不知道（引擎没报这个字段 / 没探测到）。"""
    return (getattr(snapshot, "accounts", None) or {}).get(engine)


def engine_has_accounts(snapshot, engine):
    """**确知**这家有号？None/未知一律按「有」处理（口径见 publishing_realms）。"""
    n = engine_accounts(snapshot, engine)
    return n is None or n > 0


def engine_accounts_by_realm(snapshot, engine):
    """该引擎按区的账号数 {realm: n}；None = 这家没报这个字段（只有 wb 报）。"""
    return (getattr(snapshot, "accounts_by_realm", None) or {}).get(engine)


def realm_has_accounts(snapshot, engine, realm):
    """(引擎, 区) 这一对**确知**有号？不知道就按有处理。

    与 publishing_realms 同一套取舍：只摘「确知没号」的，不猜。

    路由为什么要用它：wb 只有国内区的号，但它的 intl 目录里照样有这份模型清单，
    于是 candidates_for 会把 (wb, intl) 排进链尾。那一跳必然 503「该区域没有
    任何账号」，而且**最后一跳的报错会顶掉前面真实的上游错误** —— 实测
    deepseek-v4.1-flash 的链是 wb cn 502 → trae cn 502 → wb intl 503，
    客户端看到的却是「去面板加个国际区的号」。
    """
    if not engine_has_accounts(snapshot, engine):
        return False
    by_realm = engine_accounts_by_realm(snapshot, engine)
    if isinstance(by_realm, dict):
        return (by_realm.get(realm) or 0) > 0
    return True


def publishing_realms(snapshot):
    """这一轮对外发布「哪几家引擎 × 哪个区」—— 清单口径的**唯一入口**。

    为什么要它：引擎的 /v1/models 是「目录」，不是「我现在能服务什么」——
    · trae 一个号都没登录时，照样把整份静态目录（实测 cn 20 条聊天模型）倒出来，
      于是合并清单里出现一批点不动的模型（实测 DeepSeek-V4-Flash 只挂在 trae 上）；
    · 更细的一层是**区域**：wb 有号但没有**国际区**的号时，wb 的 intl 目录照样会被
      合并进来（实测 kimi-k3.1-pro-highspeed 只在 wb intl 目录里）—— 一样点不动。

    规则（按优先级）：
      1. 引擎账号总数为 0 -> 这家整段不发（它的所有区都不发）；
      2. 引擎报了按区账号数（wb 的 accounts_by_realm）-> 只发**有号的那些区**；
         没报的区（含键都不存在）= 0 个号；
      3. 没报按区数（qd / trae）-> 两个区都发 —— 只摘「确知没号」的，不猜；
      4. 一家号都没有 / 一个区都没号 -> 退回目录视图（全部照发）：那是「还没配好」
         的首次运行状态，发一张空清单连「这个网关支持哪些模型」都看不到，比多列
         几条点不动的更糟（首次运行自检 F07 就是靠这条不红）。加了号之后下一次
         快照就按 1/2 收窄。

    返回 {engine: [realm, ...]}，键覆盖 ENGINES（没发的那家给空列表），
    所以调用方不必再判「这家是不是压根没出现」。
    """
    out = {}
    for engine in ENGINES:
        if not engine_has_accounts(snapshot, engine):
            out[engine] = []
            continue
        by_realm = engine_accounts_by_realm(snapshot, engine)
        if isinstance(by_realm, dict):
            hit = [r for r in REALMS if (by_realm.get(r) or 0) > 0]
            # 有号、但按区表里一个 REALMS 的区都对不上（例如账号的 realm 没填，
            # 落成 ""）—— 这时**不能**把它摘光：那会把「有号」误判成「没号」。
            out[engine] = hit or list(REALMS)
        else:
            out[engine] = list(REALMS)
    if not any(out.values()):
        return {e: list(REALMS) for e in ENGINES}
    return out


def candidates_for(model, snapshot, config, requested=None):
    """(engine, realm) 候选序列。config 是 hub/core/settings.py routing() 的形状。

    requested：客户端**显式钉**的区域（?realm= / X-Realm）。钉了就把它当本次首选区，
    另一区仍然留在链尾兜底 —— 「显式优先，但不把路走死」。没钉就用 config 的
    prefer_realm。

    ★ 清单口径（publishing_realms）与路由口径在这一层对齐：**确知没号的
      (引擎, 区)** 不进候选（见 realm_has_accounts）。清单发出去的每一条，
      候选链里至少有一跳真的能服务；反过来，摘掉的组合也不再被路由拿去试。
    """
    model = str(model or "").strip().lower()
    if not model:
        return []
    engine_prefer = str((config or {}).get("engine_prefer") or "auto").strip().lower()
    if engine_prefer not in ("wb", "qd", "auto"):
        engine_prefer = "auto"
    requested = str(requested or "").strip().lower()
    if requested in REALMS:
        first = requested
    else:
        first = str((config or {}).get("prefer_realm") or "cn").strip().lower()
        if first not in REALMS:
            first = "cn"
    realms = [first] + [r for r in REALMS if r != first]
    out, all_hops = [], []
    for realm in realms:
        holders = [e for e in ENGINES if engine_holds(snapshot, e, realm, model)]
        if engine_prefer == "auto":
            holders.sort(key=lambda e: (-(snapshot.ready.get(e) or 0),
                                        0 if e == "wb" else 1))
        else:
            holders.sort(key=lambda e: 0 if e == engine_prefer else 1)
        for engine in holders:
            all_hops.append((engine, realm))
            # ★ 只留「这一对确知有号」的跳（第 247 轮）：没号的那一跳必然 503，
            #   白等一次往返，还会把它的"该区域没有任何账号"顶成客户端看到的错误。
            if realm_has_accounts(snapshot, engine, realm):
                out.append((engine, realm))
    # 护栏：全被摘光时（三家都没号 / 按区表整个对不上）退回不筛的链 ——
    # 与 publishing_realms 第 4 条同口径：不知道、或确实一处都没有时别删路，
    # 让引擎自己给真实回包（首次运行的 F07 就靠这条不红）。
    return out or all_hops


def engine_current_realm(snapshot, engine):
    """引擎当前的出口（/health.realm）；没报 / 形状不对 → None（不知道）。"""
    return (getattr(snapshot, "current_realm", None) or {}).get(engine)


def realm_for_unpinned(snapshot, engine, config=None):
    """客户端**没钉区**时，要不要替它钉一个区？返回区名或 None（不干预）。

    由来（线上实测）：wb 的区开关停在 intl，而唯一的号在 cn —— 于是任何不带
    realm 的请求都由引擎按自己的默认区（intl）处理，**4ms 就回 503**
    「no usable account for realm 'intl'；该区域没有任何账号」。用户看到的是让
    他去面板加号，而真正要做的只是把请求发到 cn。

    只在「确知当前出口没号、且另一个区有号」时改道：
      · 引擎没报按区账号数（qd/trae）或没报 realm → None，什么都不动；
      · 当前出口有号 → None（尊重运营者选的出口）；
      · 当前出口 0 个号、另一个区有号 → 返回那个区（prefer_realm 优先）。
    """
    current = engine_current_realm(snapshot, engine)
    if not current:
        return None
    by_realm = engine_accounts_by_realm(snapshot, engine)
    if not isinstance(by_realm, dict):
        return None
    if (by_realm.get(current) or 0) > 0:
        return None
    first = str((config or {}).get("prefer_realm") or "cn").strip().lower()
    if first not in REALMS:
        first = "cn"
    for realm in [first] + [r for r in REALMS if r != first]:
        if (by_realm.get(realm) or 0) > 0:
            return realm
    return None


def fallback_candidates(snapshot, config, engine):
    """快照里没有这个模型时的兜底链：默认引擎 × **有号的那些区**。

    老实现给的是 (engine, None)：让引擎用它自己的默认区。而引擎的默认区是面板上
    那个开关 —— wb 有国内区的号、开关却停在 intl，于是**清单外的任何模型名**
    都回一句 "no usable account for realm 'intl'；该区域没有任何账号"。真实原因
    往往只是模型名写错（实测客户端拼出 deepseek-v4.1-flashdeepseek-v4.1-flash），
    用户却被引去面板加号。

    这里仍然只用默认引擎（不拿别的引擎去猜模型名），但区按 /health 的**按区
    账号表**挑：表里没有号的区不送（实测 wb 报 accounts_by_realm={'cn':1}，
    于是 intl 不再被送到一个注定 503 的出口上）。引擎没报按区表（qd/trae）、
    或表里哪个区都没号时，退回 (engine, None) —— 让引擎自己定，老行为不变。
    """
    first = str((config or {}).get("prefer_realm") or "cn").strip().lower()
    if first not in REALMS:
        first = "cn"
    by_realm = engine_accounts_by_realm(snapshot, engine)
    if not isinstance(by_realm, dict):
        return [(engine, None)]
    realms = [first] + [r for r in REALMS if r != first]
    out = [(engine, r) for r in realms if (by_realm.get(r) or 0) > 0]
    return out or [(engine, None)]


def merged_model_list(snapshot):
    """合并 /v1/models：同名模型合一条，带 engines（含区域）与 realm 并集。

    ★ 只发布**有账号的（引擎 × 区域）**（见 publishing_realms）：没号的那家整段
      跳过、有号但**该区没号**的那个区也跳过 —— 只挂在它们上面的模型因此整条
      不返回。engines 里仍保留全部引擎键，没发/没这个模型的给空列表 —— 键集合
      稳定，前端不必猜这一家是不是压根没出现。

    它只管**清单**，不管路由：candidates_for 仍按快照全量给候选，客户端手写
    一个只挂在没号组合上的模型名时，失败码是引擎自己的真实回包。
    """
    publish = publishing_realms(snapshot)
    by_id = {}
    for engine in ENGINES:
        for realm in publish.get(engine) or []:
            for mid in sorted((snapshot.models.get(engine) or {}).get(realm) or set()):
                entry = by_id.setdefault(mid, {
                    "id": mid, "object": "model", "owned_by": "hub",
                    "engines": {}, "realm": [],
                })
                entry["engines"].setdefault(engine, [])
                if realm not in entry["engines"][engine]:
                    entry["engines"][engine].append(realm)
                if realm not in entry["realm"]:
                    entry["realm"].append(realm)
    out = []
    for mid in sorted(by_id):
        entry = by_id[mid]
        for engine in ENGINES:
            entry["engines"].setdefault(engine, [])
        realms = sorted(entry["realm"])
        entry["realm"] = realms
        entry["realm_exclusive"] = len(realms) == 1
        out.append(entry)
    return out


def extract_model(body):
    """从请求体 JSON 里拿模型名（chat/completions/responses/messages 都是 model 键）。"""
    if not body:
        return ""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return ""
    return str((doc.get("model") if isinstance(doc, dict) else "") or "").strip()
