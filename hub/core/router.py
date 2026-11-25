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

  * **模型冷却**（取自 workbuddy2api-hub 的 model_cooldowns 语义）：引擎自己早就把
    「某个模型撞了 429 / 6004，停到某个时刻」记在账号档里
    （wb_accounts.Account.model_cooldowns / model_caps，面板上就是
    modelCooldowns[].expiresAt），但融合层以前完全不看 —— 于是候选链会把请求送进
    一个**必然 429** 的跳，白等一次往返，还把上游的 429 原样透给客户端。
    现在快照里带上 cooldowns[(engine, realm)][model] = 恢复时刻，候选链按它排序
    （见 order_by_cooldown）。**只降权、不摘除**：同一引擎的另一个账号可能还能服务
    这个模型，摘掉会把路走死。

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
        #: 模型冷却：{(engine, realm): {model: 恢复时刻 epoch}}。
        #: 空 = **不知道**（引擎没答 /accounts），与「知道且没在冷却」不同 ——
        #: 后者是 {(engine, realm): {}}。判据见 model_cooldown_until。
        self.cooldowns = {}
        #: 整个账号级冷却的 (engine, realm)：该区**所有**账号都在 cooldown_until
        #: 之内（引擎的 inCooldown）。它比模型级更狠，但同样只降权不摘除。
        self.parked = set()
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


def _realm_counts_from_accounts(ports):
    """按区数账号：引擎没在 /health 报 accounts_by_realm 时的兜底（trae / qd）。

    由来（用户报的）：下拉里出现了 claude-opus-4.7 / claude-sonnet-4.6，可这台机器
    **一个国际账号都没有**。那两个模型只挂在 trae 的 intl 目录里；trae 的 /health
    只报总数（accounts=1）、不报按区数，于是 publishing_realms 按「没报 = 两区照发」
    处理 —— 清单里就多了两条点不动的模型。

    这里换一条不会骗人的路：直接问引擎的账号池（/accounts?realm=all，每条自带
    realm），数出来就是 {engine: {realm: n}}。取不到就不给这个键，调用方仍按
    「不知道 = 不猜」处理（老行为不变）。
    """
    try:
        from hub.core import aggregator        # 延迟导入：core 内部避免循环引用
        doc = aggregator.hub_accounts(ports)
    except Exception:
        return {}
    out = {}
    for item in (doc.get("items") or []):
        engine = str(item.get("engine") or "").strip().lower()
        if engine not in ENGINES:
            continue
        realm = str(item.get("realm") or "").strip().lower()
        bucket = out.setdefault(engine, {})
        bucket[realm] = bucket.get(realm, 0) + 1
    return out


def _int_or_none(value):
    """能转成 int 就转，转不了（缺字段 / None / 非数）就给 None。

    「不知道」与「0」必须分开：0 个账号是**确定的事实**（这台引擎没接任何号，
    模型不该对外发布），不知道只能按老行为放过（见 publishing_realms）。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_or_none(value):
    """冷却时刻：能转成正数 epoch 就给 float，否则 None（不编造）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (number > 0):
        return None
    return number


def collect_cooldowns(ports):
    """从各引擎的 /accounts?realm=all 里收「模型冷却」与「账号级冷却」。

    返回 (cooldowns, parked, errors)：
      cooldowns = {(engine, realm): {model: epoch}}   —— 该区**所有**账号里，
                  某模型最晚的恢复时刻（最保守：最晚恢复的那个账号决定这个跳能不能
                  省掉往返；早恢复的账号不会让这个跳更早可用）。
      parked    = {(engine, realm)}                  —— 该区**每一个**账号都在
                  inCooldown 里（引擎自己的账号级冷却）。只要有一个账号可用，
                  这一跳就还有希望，不进 parked。
      errors    = {engine: str|None}

    为什么走 /accounts 而不是读引擎的账号档：融合层不该知道各引擎的存储格式
    （wb/qd/trae 三家的档结构不同，而且随时会变）；/accounts 是引擎自己对外
    承诺的面板接口，modelCooldowns / inCooldown 是它的既有字段。
    取不到 → 该引擎不进 cooldowns（= 不知道），路由按老行为放过。

    只用标准库 + aggregator 的既有权杖（_request 会自己处理 /panel/login）。
    """
    from hub.core import aggregator        # 延迟导入：core 内部避免循环引用
    cooldowns, parked, errors = {}, set(), {}
    seen = {}
    for engine in ENGINES:
        port = (ports or {}).get(engine)
        if not port:
            continue
        doc = aggregator._json_or_none(engine, ports, "/accounts?realm=all")
        if not isinstance(doc, dict):
            errors[engine] = "accounts unavailable"
            continue
        errors[engine] = None
        # 三种形状都认：
        #   · 引擎 /accounts 的**原始**回包 —— 键是 `accounts`（实测 wb/qd/trae 三家
        #     都是这个键；曾按 `items` 读，于是冷却表永远是空的，白改一场）；
        #   · aggregator.hub_accounts 归一后的形状 —— 键是 `items`；
        #   · 直接给一个数组。
        rows = doc.get("accounts")
        if not isinstance(rows, list):
            rows = doc.get("items")
        if not isinstance(rows, list):
            rows = []
        #: 每个 (engine, realm) 的账号数与「在冷却中的账号数」，用来判 parked。
        tally = seen.setdefault(engine, {})
        for row in rows:
            if not isinstance(row, dict):
                continue
            realm = str(row.get("realm") or "").strip().lower()
            if realm not in REALMS:
                continue
            bucket = tally.setdefault(realm, {"total": 0, "cooling": 0})
            bucket["total"] += 1
            if row.get("inCooldown") is True:
                bucket["cooling"] += 1
            models = row.get("modelCooldowns")
            if not isinstance(models, list):
                continue
            target = cooldowns.setdefault((engine, realm), {})
            for item in models:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("model") or "").strip().lower()
                until = _epoch_or_none(item.get("expiresAt"))
                if not mid or until is None:
                    continue
                # 取**最晚**：同区多个账号都在冷却时，这一跳要等最晚的那个。
                if until > target.get(mid, 0.0):
                    target[mid] = until
    now = time.time()
    for engine, realms in seen.items():
        for realm, bucket in realms.items():
            # 只有「确实有账号、且每一个都在冷却」才算整区 parked。
            # 账号数为 0 时不算：那是没配好，不该被当成冷却。
            if bucket["total"] and bucket["cooling"] >= bucket["total"]:
                parked.add((engine, realm))
    # 过期条目在收集时清掉，快照里只留**还有效**的冷却。
    for key in list(cooldowns):
        live = {m: t for m, t in cooldowns[key].items() if t > now}
        cooldowns[key] = live
    return cooldowns, parked, errors


def model_cooldown_until(snapshot, engine, realm, model):
    """该 (engine, realm) 上这个模型的恢复时刻；不知道 / 没在冷却 → None。

    None 与「已过期」在这里是同一个结果（都返回 None）—— 调用方只需要知道
    「现在能不能试」，不需要区分「从没撞过」和「撞过但好了」。
    """
    model = str(model or "").strip().lower()
    if not model:
        return None
    table = (getattr(snapshot, "cooldowns", None) or {}).get((engine, realm))
    if not isinstance(table, dict):
        return None
    until = table.get(model)
    if until is None:
        return None
    return until if until > time.time() else None


def order_by_cooldown(candidates, snapshot, model):
    """按「这一跳现在能不能服务这个模型」把候选链重排（**稳定**，不改长度）。

    三档，档内保持原顺序（原顺序已经编码了区域优先与引擎就绪排序）：
      0. 该模型在这个跳上没在冷却 —— 立刻能试；
      1. 模型在冷却，但该跳还有别的账号（不是整区 parked）—— 也许能成；
      2. 整区 parked（所有账号都在账号级冷却）—— 几乎必然失败，放最后。

    为什么不直接摘掉：摘掉之后「全部候选都在冷却」就退化成空链，portal 会掉进
    fallback_candidates 去打一个默认引擎 —— 那反而绕过了这里的信息，还可能
    打到一个更差的跳。降权保留了「全都在冷却时仍然试一次」的语义（上游的
    429 比我们自己猜的更准），同时把最好的那一跳排到最前。
    """
    if not candidates:
        return list(candidates)
    parked = getattr(snapshot, "parked", None) or set()
    ranked = []
    for index, (engine, realm) in enumerate(candidates):
        if (engine, realm) in parked:
            rank = 2
        elif model_cooldown_until(snapshot, engine, realm, model) is not None:
            rank = 1
        else:
            rank = 0
        ranked.append((rank, index, (engine, realm)))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def cooldown_summary(snapshot, model=None):
    """把快照里的冷却收成可读形状（面板 / 排障头用）。

    model 给了就只报这个模型的；没给就报全部仍在冷却的。
    返回 [{engine, realm, model, until, seconds_left}]，按剩余时间升序。
    """
    now = time.time()
    out = []
    wanted = str(model or "").strip().lower()
    for (engine, realm), table in (getattr(snapshot, "cooldowns", None) or {}).items():
        for mid, until in (table or {}).items():
            if wanted and mid != wanted:
                continue
            left = until - now
            if left <= 0:
                continue
            out.append({"engine": engine, "realm": realm, "model": mid,
                        "until": int(until), "seconds_left": int(left)})
    out.sort(key=lambda item: item["seconds_left"])
    return out


def hold_until(candidates, snapshot, model):
    """整条候选链**全部**都在这个模型的冷却里时，返回最早的恢复时刻；否则 None。

    这是「冷却期内不调度，到点后再请求」的判据（对齐 workbuddy2api-hub 的
    Account.ready(model=...) 语义：冷却中的账号不会被 pick）。

    只在**确知**全链冷却时才拦：
      · 候选为空 → None（交给 fallback_candidates 的老路）；
      · 任意一跳没有这个模型的冷却记录 → None（那一跳还能试）；
      · 引擎没答 /accounts（快照里没有该键）→ None（不知道就不拦）。
    返回 None 时调用方照常逐跳尝试 —— 「上游的 429 比我们猜的更准」。
    """
    if not candidates:
        return None
    table = getattr(snapshot, "cooldowns", None) or {}
    if not table:
        return None
    earliest = None
    for engine, realm in candidates:
        until = model_cooldown_until(snapshot, engine, realm, model)
        if until is None:
            return None                      # 这一跳还能试，别拦
        earliest = until if earliest is None else min(earliest, until)
    return earliest


def cooldown_429_body(model, until, candidates, snapshot):
    """全链冷却时回的 429 正文：说清「哪个模型、等多久、几点恢复、哪些跳在等」。

    措辞对齐 OpenAI 的 429 形状（error.message/type/code），客户端现有的
    Retry-After 处理不用改。
    """
    now = time.time()
    left = max(0, int(until - now)) if until else 0
    rows = []
    for engine, realm in candidates:
        stop = model_cooldown_until(snapshot, engine, realm, model)
        rows.append({"engine": engine, "realm": realm,
                     "until": int(stop) if stop else None,
                     "seconds_left": max(0, int(stop - now)) if stop else None})
    return {
        "error": {
            "message": ("model %r is in upstream cooldown on every candidate hop; "
                        "retry after %d seconds (resumes at %s)"
                        % (model, left,
                           time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(until)) if until else "?")),
            "type": "rate_limit_error",
            "code": "model_cooldown",
            "model": model,
            "retry_after": left,
            "resumes_at": int(until) if until else None,
            "hops": rows,
            "hint": ("这是引擎侧记录的上游限流窗口（429 / code 6004 的恢复时刻），"
                     "不是网关自己编的。窗口到期后同一个请求会自动重新调度。"),
        }
    }


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
    # ★ 引擎没报 accounts_by_realm 的（trae / qd）：去它的账号池数一遍。
    #   否则「没报 = 两区照发」会把没号的区也发布出去（实测 trae 只有 cn 号，
    #   却把 intl 目录里的 claude-* 发进了合并清单）。
    missing = [e for e in ENGINES if e in models and by_realm.get(e) is None]
    if missing:
        for engine, counts in _realm_counts_from_accounts(ports).items():
            if engine in missing and counts:
                by_realm[engine] = counts
    # ★ 模型冷却（第 N 轮）：引擎自己的 /accounts 已经记着「哪个模型撞线停到几点」，
    #   这里收进快照，候选链据此降权。取不到就**空表**（= 不知道），路由行为不变。
    cooldowns, parked, cd_errors = {}, set(), {}
    try:
        cooldowns, parked, cd_errors = collect_cooldowns(ports)
    except Exception as exc:                       # noqa: BLE001 —— 冷却探测不许打断路由
        for engine in ENGINES:
            cd_errors[engine] = "cooldown probe failed: %s" % (exc,)
    with snapshot.lock:
        snapshot.models = models
        snapshot.ready = ready
        snapshot.accounts = accounts
        snapshot.accounts_by_realm = by_realm
        snapshot.current_realm = current
        snapshot.cooldowns = cooldowns
        snapshot.parked = parked
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
