# -*- coding: utf-8 -*-
"""_test_model_cooldown.py —— 模型冷却：候选降权 / 冷却期拦停 / 到点放行。

语义来源：workbuddy2api-hub 的 wb_accounts.Account.model_cooldowns /
model_caps（面板字段 modelCooldowns[].expiresAt）。引擎早就把「这个模型撞了
429 / code 6004，停到某个时刻」记下来了，融合层以前完全不看 —— 于是候选链会把
请求送进一个必然 429 的跳。这里把新行为钉住：

  C 系列 —— collect_cooldowns：从 /accounts 收表（多账号取最晚、过期条目清掉、
             账号级 parked 只在「每一个账号都在冷却」时成立）；
  D 系列 —— order_by_cooldown：稳定降权，三档（可用 / 模型冷却 / 整区 parked），
             不改链长、不摘跳；
  E 系列 —— hold_until：只有**确知全链冷却**才返回时刻（三种放过条件逐个钉）；
  F 系列 —— cooldown_429_body / cooldown_summary：对外形状与 Retry-After。

纯单元：不碰引擎端口、不起进程、不联网（collect_cooldowns 用桩替换 aggregator）。
口径与 _test_merged_router.py 一致：harness 的 check()，自报 SUMMARY 行。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

_ROUTER = os.path.join(P.HUB, "core", "router.py")
if not os.path.exists(_ROUTER):
    H.skip("C01 hub/core/router.py 尚未落地 —— 模型冷却判据整体跳过", _ROUTER)
    H.finish()

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import aggregator
from hub.core import router


# --------------------------------------------------------------------- 夹具
class Snap(object):
    """最小快照：只带冷却相关的两张表（其余字段按老默认）。"""

    def __init__(self, cooldowns=None, parked=None, models=None):
        self.cooldowns = cooldowns if cooldowns is not None else {}
        self.parked = parked if parked is not None else set()
        self.models = models or {}
        self.ready = {}
        self.accounts = {}
        self.accounts_by_realm = {}
        self.current_realm = {}
        self.errors = {}


def stub_accounts(payload):
    """把 aggregator._json_or_none 换成按引擎回固定 doc 的桩。

    payload = {engine: doc|None}；None 表示这个引擎取不到（该引擎不进 cooldowns）。
    """
    def fake(engine, ports, path, **kw):
        return payload.get(engine)
    return fake


def with_stub(payload, fn):
    """在桩生效期间跑 fn，结束后一定还原（套件自身不许污染别的判据）。"""
    original = aggregator._json_or_none
    aggregator._json_or_none = stub_accounts(payload)
    try:
        return fn()
    finally:
        aggregator._json_or_none = original


def row(realm="cn", cooling=False, models=None):
    """一条 /accounts 账号行。models = {模型: 剩余秒}。"""
    now = time.time()
    return {
        "realm": realm,
        "inCooldown": cooling,
        "modelCooldowns": [
            {"model": m, "expiresAt": int(now + left)}
            for m, left in (models or {}).items()
        ],
    }


PORTS = {"wb": 8811, "qd": 8812, "trae": 8813}


# ------------------------------------------------------- C: collect_cooldowns
def _c_basic():
    payload = {"wb": {"items": [row(models={"glm-5.3": 600})]},
               "qd": {"items": [row(models={"glm-5.3": 120})]},
               "trae": {"items": []}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd, parked, errors = _c_basic()
H.check("C01 两个引擎各自的模型冷却都被收进快照",
        set(cd) == {("wb", "cn"), ("qd", "cn")}, "got=%r" % (cd,))
H.check("C02 恢复时刻是未来（不是 0 也不是 None）",
        all(t > time.time() for t in cd[("wb", "cn")].values()), "wb=%r" % (cd.get(("wb", "cn")),))
H.eq("C03 空账号列表的引擎不进冷却表（不知道 ≠ 没在冷却）",
     cd.get(("trae", "cn")), None)
H.eq("C04 没有账号级冷却时不产生 parked", parked, set())
H.check("C05 三个引擎的 errors 都是 None（都答上来了）",
        all(errors.get(e) is None for e in ("wb", "qd", "trae")), "errors=%r" % (errors,))


def _c_multi_account():
    """同一 (engine, realm) 两个账号都撞了同一个模型：取**最晚**的恢复时刻。"""
    payload = {"wb": {"items": [row(models={"m": 100}), row(models={"m": 900})]}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd2, _, _ = _c_multi_account()
until = cd2[("wb", "cn")]["m"]
H.check("C06 同区多账号撞同一模型取最晚恢复时刻（最保守）",
        until > time.time() + 850, "left=%.0fs" % (until - time.time()))


def _c_expired():
    """已经过期的条目在收集时就被清掉（快照里只留还有效的）。"""
    payload = {"wb": {"items": [row(models={"gone": -60, "live": 300})]}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd3, _, _ = _c_expired()
H.eq("C07 过期冷却被清掉，只剩还有效的",
     sorted(cd3[("wb", "cn")]), ["live"])


def _c_parked():
    """账号级 parked：**每一个**账号都在 inCooldown 才成立。"""
    all_cool = {"wb": {"items": [row(cooling=True), row(cooling=True)]}}
    partial = {"wb": {"items": [row(cooling=True), row(cooling=False)]}}
    return (with_stub(all_cool, lambda: router.collect_cooldowns(PORTS)),
            with_stub(partial, lambda: router.collect_cooldowns(PORTS)))


(_, parked_all, _), (_, parked_partial, _) = _c_parked()
H.eq("C08 全部账号都在冷却 -> parked", parked_all, {("wb", "cn")})
H.eq("C09 只要有一个账号可用 -> 不进 parked", parked_partial, set())


def _c_engine_down():
    """引擎没答 /accounts（None）-> 该引擎不进表，errors 有原因，别的引擎照常。"""
    payload = {"wb": None, "qd": {"items": [row(models={"m": 60})]}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd4, _, err4 = _c_engine_down()
H.check("C10 引擎取不到时该引擎不进冷却表（不知道就不猜）",
        ("wb", "cn") not in cd4, "cd=%r" % (cd4,))
H.check("C11 取不到的引擎在 errors 里有可读原因",
        err4.get("wb"), "errors=%r" % (err4,))
H.eq("C12 另一台引擎照常收到", sorted(cd4.get(("qd", "cn"), {})), ["m"])


def _c_bad_rows():
    """脏数据（缺字段 / 非 dict / 非法 realm / 非数时刻）一律跳过，不抛。"""
    payload = {"wb": {"items": [
        {"realm": "cn", "modelCooldowns": [{"model": "", "expiresAt": 9999999999}]},
        {"realm": "cn", "modelCooldowns": [{"model": "noexp"}]},
        {"realm": "cn", "modelCooldowns": [{"model": "strtime", "expiresAt": "abc"}]},
        {"realm": "mars", "modelCooldowns": [{"model": "otherrealm", "expiresAt": 9999999999}]},
        "not-a-dict",
        {"realm": "cn", "modelCooldowns": "not-a-list"},
        {"realm": "cn", "modelCooldowns": [{"model": "ok", "expiresAt": time.time() + 120}]},
    ]}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


ok_flag, cd5 = H.guard("C13 脏数据不让收集器抛异常", _c_bad_rows)
if ok_flag:
    H.eq("C14 脏行全被跳过，只留下合法的那条",
         sorted(cd5[0][("wb", "cn")]), ["ok"])


#: 引擎 /accounts 的**原始**键是 `accounts`（不是 aggregator 归一后的 `items`）。
#: 这个区别踩过一次：按 items 读的时候三家都读空，冷却表永远是空的，
#: 而接口本身回 200 —— 静默失效，测试也全绿。两条形状都钉住。
def _c_raw_accounts_key():
    payload = {"wb": {"accounts": [row(models={"raw-shape": 600})],
                      "storage": "x", "usable": 1}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd6, _, _ = _c_raw_accounts_key()
H.eq("C15 认引擎原始回包的 accounts 键（不是只有 items）",
     sorted(cd6.get(("wb", "cn"), {})), ["raw-shape"])


def _c_items_key():
    payload = {"wb": {"items": [row(models={"agg-shape": 600})]}}
    return with_stub(payload, lambda: router.collect_cooldowns(PORTS))


cd7, _, _ = _c_items_key()
H.eq("C16 也认 aggregator 归一后的 items 键",
     sorted(cd7.get(("wb", "cn"), {})), ["agg-shape"])


# ---------------------------------------------------- D: order_by_cooldown
def _d_rank():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 600}, ("qd", "cn"): {}},
             parked={("trae", "cn")})
    cands = [("wb", "cn"), ("qd", "cn"), ("trae", "cn")]
    return s, cands


s_d, cands_d = _d_rank()
H.eq("D01 模型冷却的跳被降权、整区 parked 的更靠后",
     router.order_by_cooldown(cands_d, s_d, "m"),
     [("qd", "cn"), ("wb", "cn"), ("trae", "cn")])
H.eq("D02 别的模型不受这个冷却影响（顺序原样）",
     router.order_by_cooldown(cands_d, s_d, "other"), cands_d)
H.eq("D03 链长不变（只降权不摘跳）",
     len(router.order_by_cooldown(cands_d, s_d, "m")), len(cands_d))
H.eq("D04 空链原样返回", router.order_by_cooldown([], s_d, "m"), [])
H.eq("D05 空模型名不触发降权", router.order_by_cooldown(cands_d, s_d, ""), cands_d)


def _d_stable():
    """档内保持原顺序：两个都在冷却时，不因排序把原来的优先级打乱。"""
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 60},
                        ("qd", "cn"): {"m": now + 9000}})
    return router.order_by_cooldown([("wb", "cn"), ("qd", "cn")], s, "m")


H.eq("D06 档内稳定：都冷却时保持原顺序（不按剩余时间重排）",
     _d_stable(), [("wb", "cn"), ("qd", "cn")])


def _d_expired():
    """冷却表里残留一个已过期时刻 -> 不算冷却（快照过期不拦）。"""
    s = Snap(cooldowns={("wb", "cn"): {"m": time.time() - 1}})
    return router.order_by_cooldown([("wb", "cn"), ("qd", "cn")], s, "m")


H.eq("D07 已过期的冷却不再降权", _d_expired(), [("wb", "cn"), ("qd", "cn")])


# ------------------------------------------------------------ E: hold_until
def _e_all_cooled():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 300},
                        ("qd", "cn"): {"m": now + 100}})
    return router.hold_until([("wb", "cn"), ("qd", "cn")], s, "m")


H.check("E01 全链都在冷却 -> 返回最早恢复时刻", _e_all_cooled() is not None)


def _e_earliest():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 900},
                        ("qd", "cn"): {"m": now + 100}})
    hold = router.hold_until([("wb", "cn"), ("qd", "cn")], s, "m")
    return hold, now


hold_e, now_e = _e_earliest()
H.check("E02 返回的是**最早**的恢复时刻（等最短的那一跳就够）",
        hold_e is not None and abs(hold_e - (now_e + 100)) < 5,
        "hold=%r now+100=%r" % (hold_e, now_e + 100))


def _e_one_free():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 300}})
    return router.hold_until([("wb", "cn"), ("qd", "cn")], s, "m")


H.eq("E03 只要有一跳没有这个模型的冷却 -> 不拦（那一跳还能试）", _e_one_free(), None)


def _e_unknown():
    s = Snap(cooldowns={})
    return router.hold_until([("wb", "cn")], s, "m")


H.eq("E04 引擎没答 /accounts（空表）-> 不拦（不知道就不拦）", _e_unknown(), None)


def _e_empty():
    return router.hold_until([], Snap(), "m")


H.eq("E05 空候选链 -> 不拦（交给 fallback_candidates 的老路）", _e_empty(), None)


def _e_other_model():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"other": now + 300}})
    return router.hold_until([("wb", "cn")], s, "m")


H.eq("E06 冷却的是别的模型 -> 不拦", _e_other_model(), None)


def _e_after_expiry():
    """到点放行：冷却时刻过去之后 hold_until 返回 None。"""
    s = Snap(cooldowns={("wb", "cn"): {"m": time.time() - 1}})
    return router.hold_until([("wb", "cn")], s, "m")


H.eq("E07 冷却到点后放行（hold_until -> None）", _e_after_expiry(), None)


def _e_model_cooldown_until():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 600}})
    return (router.model_cooldown_until(s, "wb", "cn", "m"),
            router.model_cooldown_until(s, "wb", "cn", "other"),
            router.model_cooldown_until(s, "qd", "cn", "m"),
            router.model_cooldown_until(s, "wb", "cn", ""))


a, b, c, d = _e_model_cooldown_until()
H.check("E08 有冷却的模型给出恢复时刻", a is not None and a > now_e, "a=%r" % (a,))
H.eq("E09 没冷却的模型 -> None", b, None)
H.eq("E10 别的引擎 -> None（按引擎×区分开）", c, None)
H.eq("E11 空模型名 -> None", d, None)


# ------------------------------------------- F: cooldown_429_body / summary
def _f_body():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 120},
                        ("qd", "cn"): {"m": now + 240}})
    return router.cooldown_429_body("m", now + 120, [("wb", "cn"), ("qd", "cn")], s)


body_f = _f_body()
err = body_f.get("error") or {}
H.eq("F01 429 正文的 code 是 model_cooldown", err.get("code"), "model_cooldown")
H.eq("F02 type 是 rate_limit_error（OpenAI 的 429 形状）",
     err.get("type"), "rate_limit_error")
H.check("F03 retry_after 是正整数秒", isinstance(err.get("retry_after"), int)
        and err["retry_after"] > 0, "retry_after=%r" % (err.get("retry_after"),))
H.check("F04 retry_after 与恢复时刻对得上（±3s）",
        abs(err["retry_after"] - 120) <= 3, "retry_after=%r" % (err.get("retry_after"),))
H.eq("F05 resumes_at 是 epoch 整数", isinstance(err.get("resumes_at"), int), True)
H.eq("F06 hops 覆盖每一跳", len(err.get("hops") or []), 2)
H.check("F07 hops 里每一条都带剩余秒数",
        all(isinstance(h.get("seconds_left"), int) for h in err["hops"]),
        "hops=%r" % (err.get("hops"),))
H.check("F08 正文里有可读的恢复时间（人看得懂）",
        "resumes at" in (err.get("message") or ""), "msg=%r" % (err.get("message"),))


def _f_summary():
    now = time.time()
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 500, "n": now + 50},
                        ("qd", "cn"): {"m": now - 10}})
    return router.cooldown_summary(s), router.cooldown_summary(s, "m")


all_rows, only_m = _f_summary()
H.eq("F09 summary 只列仍在冷却的（过期的不出现）", len(all_rows), 2)
H.eq("F10 summary 按剩余时间升序", [r["model"] for r in all_rows], ["n", "m"])
H.eq("F11 指定模型时只报这个模型", [r["model"] for r in only_m], ["m"])
H.check("F12 summary 每条带 engine/realm/until/seconds_left",
        all(set(("engine", "realm", "model", "until", "seconds_left")) <= set(r)
            for r in all_rows), "rows=%r" % (all_rows,))


# --------------------------------------------- G: 与候选链的端到端组合
def _g_end_to_end():
    """候选链 -> 降权 -> 拦停判定，走一遍真实调用顺序。"""
    now = time.time()
    models = {"wb": {"cn": {"m"}, "intl": set()},
              "qd": {"cn": {"m"}, "intl": set()},
              "trae": {"cn": {"m"}, "intl": set()}}
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 300},
                        ("qd", "cn"): {"m": now + 300}},
             parked=set(), models=models)
    s.ready = {"wb": 9, "qd": 3, "trae": 1}
    base = router.candidates_for("m", s, {"prefer_realm": "cn", "engine_prefer": "auto"})
    ordered = router.order_by_cooldown(base, s, "m")
    hold = router.hold_until(ordered, s, "m")
    return base, ordered, hold


base_g, ordered_g, hold_g = _g_end_to_end()
H.check("G01 候选链里三跳都在（原链完整）", len(base_g) == 3, "base=%r" % (base_g,))
H.check("G02 未冷却的 trae 被排到最前",
        ordered_g and ordered_g[0] == ("trae", "cn"), "ordered=%r" % (ordered_g,))
H.check("G03 前两跳都在冷却、第三跳不在 -> 不拦停", hold_g is None, "hold=%r" % (hold_g,))


def _g_all_hold():
    now = time.time()
    models = {"wb": {"cn": {"m"}, "intl": set()},
              "qd": {"cn": {"m"}, "intl": set()},
              "trae": {"cn": {"m"}, "intl": set()}}
    s = Snap(cooldowns={("wb", "cn"): {"m": now + 300},
                        ("qd", "cn"): {"m": now + 300},
                        ("trae", "cn"): {"m": now + 300}},
             models=models)
    s.ready = {"wb": 9, "qd": 3, "trae": 1}
    base = router.candidates_for("m", s, {"prefer_realm": "cn", "engine_prefer": "auto"})
    return router.hold_until(base, s, "m")


H.check("G04 三跳全在冷却 -> 拦停（冷却期内不调度）", _g_all_hold() is not None)


H.finish()
