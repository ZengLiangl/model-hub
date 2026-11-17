# -*- coding: utf-8 -*-
"""_test_merged_router.py —— 合并模式智能路由：候选排序 / 同名合并 / 合并层设置。

纯单元：不碰引擎端口。覆盖：

  * router.candidates_for —— 区域优先（prefer_realm）+ 引擎排序（auto=就绪多者先、
    显式 wb/qd 固定序）+ 只保留「该引擎在该区清单里有这个模型」的组合；
  * router.merged_model_list —— 同名模型合一条、engines/realm 并集、realm_exclusive；
  * router.extract_model —— chat / responses / messages 三协议的 model 提取；
  * settings 层 —— merged_keys 掩码/匹配/替换（掩码条目保留旧值）、routing 校验、
    面板口令默认 admin 与改密校验。

口径与 _test_aggregator.py 一致：harness 的 check()，自报 SUMMARY 行。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import harness as H
from _lib import paths as P

_ROUTER = os.path.join(P.HUB, "core", "router.py")
_SETTINGS = os.path.join(P.HUB, "core", "settings.py")
if not (os.path.exists(_ROUTER) and os.path.exists(_SETTINGS)):
    H.skip("R01 hub/core/router.py 或 settings.py 尚未落地 —— 合并路由判据整体跳过", _ROUTER)
    H.finish()

sys.path.insert(0, os.path.dirname(P.HUB))
from hub.core import router
from hub.core import settings


class Snapshot(object):
    def __init__(self, models=None, ready=None):
        self.models = models or {}
        self.ready = ready or {}
        self.errors = {}


def snap(models, ready):
    return Snapshot(models=models, ready=ready)


CFG_CN_AUTO = {"prefer_realm": "cn", "engine_prefer": "auto"}

# ---------- 候选排序 ----------
s = snap({"wb": {"cn": {"glm-5.3-flash"}, "intl": {"glm-5.3-flash"}},
          "qd": {"cn": {"glm-5.3-flash"}, "intl": set()}},
         {"wb": 27, "qd": 3})
H.eq("R02 cn 优先 + auto 按就绪多者先（wb27>qd3）+ intl 只剩 wb",
     router.candidates_for("glm-5.3-flash", s, CFG_CN_AUTO),
     [("wb", "cn"), ("qd", "cn"), ("wb", "intl")])

s = snap({"wb": {"cn": {"m"}, "intl": {"m"}}, "qd": {"cn": {"m"}, "intl": {"m"}}},
         {"wb": 5, "qd": 9})
H.eq("R03 prefer=intl 翻转区域顺序，区内 auto 仍按就绪排（qd9>wb5）",
     router.candidates_for("m", s, {"prefer_realm": "intl", "engine_prefer": "auto"}),
     [("qd", "intl"), ("wb", "intl"), ("qd", "cn"), ("wb", "cn")])

s = snap({"wb": {"cn": {"m"}}, "qd": {"cn": {"m"}}}, {"wb": 99, "qd": 1})
H.eq("R04 显式 engine_prefer=qd 压过就绪数",
     router.candidates_for("m", s, {"prefer_realm": "cn", "engine_prefer": "qd"}),
     [("qd", "cn"), ("wb", "cn")])

s = snap({"wb": {"cn": set(), "intl": {"m"}}, "qd": {"cn": {"m"}, "intl": set()}},
         {"wb": 5, "qd": 5})
H.eq("R05 只保留「该引擎在该区有此模型」的组合",
     router.candidates_for("m", s, CFG_CN_AUTO),
     [("qd", "cn"), ("wb", "intl")])

H.eq("R06 空模型名 -> 无候选", router.candidates_for("", snap({}, {}), CFG_CN_AUTO), [])

s = snap({"wb": {"cn": {"glm-5.3-flash"}}}, {"wb": 1})
H.eq("R07 模型名大小写不敏感（快照侧已小写）",
     router.candidates_for("GLM-5.3-Flash", s, CFG_CN_AUTO), [("wb", "cn")])

# ---------- 同名合并 ----------
s = snap({"wb": {"cn": {"m"}, "intl": set()}, "qd": {"cn": {"m"}, "intl": {"m"}}}, {})
by_id = {d["id"]: d for d in router.merged_model_list(s)}
H.check("R08 同名模型合一条", "m" in by_id and len(router.merged_model_list(s)) == 1,
        sorted(by_id))
# 第 100 轮起有三家：没有该模型的引擎给**空列表**（前端据此显示"这家没这个模型"），
# 而不是把它从 map 里删掉 —— 删掉的话"缺引擎"和"引擎在该区没有"就分不清了。
H.eq("R08b engines 并集（wb 只 cn、qd 双区、trae 无该模型→空列表）", by_id.get("m", {}).get("engines"),
     {"wb": ["cn"], "qd": ["cn", "intl"], "trae": []})
H.eq("R08c realm 并集排序", by_id.get("m", {}).get("realm"), ["cn", "intl"])
H.check("R08d 双区 -> 非独占", by_id.get("m", {}).get("realm_exclusive") is False)

s = snap({"wb": {"cn": set(), "intl": {"gpt-5.5"}}}, {})
one = router.merged_model_list(s)
H.check("R09 单区 -> realm_exclusive=True", one and one[0]["realm_exclusive"] is True
        and one[0]["realm"] == ["intl"], one)

s = snap({"wb": {"cn": {"b", "a"}}}, {})
H.eq("R10 合并清单按 id 排序", [d["id"] for d in router.merged_model_list(s)], ["a", "b"])

# ---------- 只发有账号的「引擎 × 区域」（第 245/246 轮）----------
# 由来：引擎的 /v1/models 是「目录」，与有没有登录号无关 —— trae 池子空着也照样
# 倒出整份静态清单，于是合并清单里全是点不动的模型（实测 DeepSeek-V4-Flash 只挂在
# trae 上）。再细一层是**区域**：wb 有号但没有国际区的号时，wb intl 目录照样被合并
# 进来（实测 kimi-k3.1-pro-highspeed 只在 wb intl 目录里）。
# 快照因此多记两个数：/health.accounts（总数）与 accounts_by_realm（按区）。
class SnapAcct(Snapshot):
    def __init__(self, models=None, ready=None, accounts=None, by_realm=None):
        Snapshot.__init__(self, models=models, ready=ready)
        self.accounts = accounts or {}
        self.accounts_by_realm = by_realm or {}


s = SnapAcct({"wb": {"cn": {"m"}}, "trae": {"cn": {"only-trae"}, "intl": {"only-trae"}}},
             {"wb": 1, "trae": 0}, {"wb": 2, "trae": 0})
by_id = {d["id"]: d for d in router.merged_model_list(s)}
H.check("R22 0 个号的引擎不发布：只挂在它上面的模型整条不返回",
        "only-trae" not in by_id and "m" in by_id, sorted(by_id))
H.eq("R22b engines 键集合照旧含全部引擎（没号的那家给空列表）",
     sorted(by_id["m"].get("engines") or {}), sorted(router.ENGINES))
H.eq("R22c 有号的引擎照常列", by_id["m"]["engines"].get("wb"), ["cn"])

s = SnapAcct({"wb": {"cn": {"m"}}, "trae": {"intl": {"m"}}}, {}, {"wb": 3, "trae": 0})
by_id = {d["id"]: d for d in router.merged_model_list(s)}
H.eq("R23 同名模型：没号引擎贡献的那一区也从 realm 并集里消失",
     (by_id["m"]["engines"].get("trae"), by_id["m"]["realm"],
      by_id["m"]["realm_exclusive"]), ([], ["cn"], True))

s = SnapAcct({"wb": {"cn": {"m"}}, "qd": {"cn": {"n"}}}, {},
             {"wb": 0, "qd": 0, "trae": 0})
H.eq("R24 一家号都没有 -> 退回目录视图（首次运行不留空清单）",
     [d["id"] for d in router.merged_model_list(s)], ["m", "n"])

H.eq("R25 账号数未知（老引擎没报 / health 没答）-> 照发，不拿猜测删清单",
     [d["id"] for d in router.merged_model_list(snap({"wb": {"cn": {"m"}}}, {}))], ["m"])
H.eq("R25b engine_has_accounts：未知=True / 0=False / 正数=True",
     (router.engine_has_accounts(snap({}, {}), "trae"),
      router.engine_has_accounts(SnapAcct({}, {}, {"trae": 0}), "trae"),
      router.engine_has_accounts(SnapAcct({}, {}, {"trae": 2}), "trae")),
     (True, False, True))
H.eq("R25c publishing_realms：有号才发 / 一家都没号时全发",
     (router.publishing_realms(SnapAcct({}, {}, {"wb": 1, "qd": 0, "trae": 0})),
      router.publishing_realms(SnapAcct({}, {}, {"wb": 0, "qd": 0, "trae": 0}))),
     ({"wb": list(router.REALMS), "qd": [], "trae": []},
      {e: list(router.REALMS) for e in router.ENGINES}))

# ---- 区域一层（第 246 轮）：wb 有号、但只有国内区的号 ----
s = SnapAcct({"wb": {"cn": {"glm-cn"}, "intl": {"kimi-k3.1-pro-highspeed"}}},
             {}, {"wb": 2}, {"wb": {"cn": 2}})
H.eq("R27 只有国内区号 -> wb 的 intl 目录整段不发（国际区独有模型不返回）",
     [d["id"] for d in router.merged_model_list(s)], ["glm-cn"])
H.eq("R27b publishing_realms 报出按区收窄的结果",
     router.publishing_realms(s)["wb"], ["cn"])

s = SnapAcct({"wb": {"cn": {"glm-cn"}, "intl": {"kimi"}}}, {}, {"wb": 2},
             {"wb": {"cn": 2, "intl": 0}})
H.eq("R28 按区表里显式 0 与缺键等价（都是「该区没号」）",
     [d["id"] for d in router.merged_model_list(s)], ["glm-cn"])

# 护栏：有号、但按区表里一个 REALMS 的区都对不上（账号 realm 没填成 ""）——
# 不能把它摘光，那等于把「有号」误判成「没号」。
s = SnapAcct({"wb": {"cn": {"glm-cn"}, "intl": {"kimi"}}}, {}, {"wb": 2}, {"wb": {"": 2}})
H.eq("R28b 按区表对不上 REALMS 时退回两区（不把有号的引擎摘光）",
     (router.publishing_realms(s)["wb"],
      [d["id"] for d in router.merged_model_list(s)]), (list(router.REALMS), ["glm-cn", "kimi"]))

s = SnapAcct({"wb": {"cn": {"glm-cn"}}, "qd": {"cn": {"q-cn"}, "intl": {"q-intl"}}},
             {}, {"wb": 2, "qd": 1, "trae": 0}, {"wb": {"cn": 2}})   # qd 没报按区 -> 两区照发
H.eq("R29 没报按区数的引擎（qd/trae）两区照发 —— 不拿猜测删区",
     router.publishing_realms(s), {"wb": ["cn"], "qd": list(router.REALMS), "trae": []})
H.eq("R29b 清单里 qd 的国际区模型仍在", [d["id"] for d in router.merged_model_list(s)],
     ["glm-cn", "q-cn", "q-intl"])

s = SnapAcct({"wb": {"cn": {"glm-cn"}}}, {}, {"wb": 0, "qd": 0, "trae": 0},
             {"wb": {"cn": 0}})
H.eq("R30 按区表全 0 且三家总数都是 0 -> 退回目录视图",
     [d["id"] for d in router.merged_model_list(s)], ["glm-cn"])

# 阳性对照：把过滤去掉（等价于老实现）必须让 R22/R27 判红 —— 防止判据本身写空。
class _Unfiltered(object):
    @staticmethod
    def merged_model_list(snapshot):
        by_id = {}
        for engine in router.ENGINES:
            for realm in router.REALMS:
                for mid in (snapshot.models.get(engine) or {}).get(realm) or set():
                    by_id.setdefault(mid, {"id": mid, "engines": {}})
        return [by_id[k] for k in sorted(by_id)]


_bad = _Unfiltered.merged_model_list(
    SnapAcct({"trae": {"cn": {"only-trae"}}}, {}, {"wb": 1, "trae": 0}))
H.check("R26 阳性对照：不过滤时 only-trae 会漏出来（判据不是空的）",
        any(d["id"] == "only-trae" for d in _bad), _bad)
_bad2 = _Unfiltered.merged_model_list(
    SnapAcct({"wb": {"intl": {"kimi-k3.1-pro-highspeed"}}}, {}, {"wb": 2}, {"wb": {"cn": 2}}))
H.check("R31 阳性对照：不过滤时 wb 的国际区模型会漏出来",
        any(d["id"] == "kimi-k3.1-pro-highspeed" for d in _bad2), _bad2)

# ---- 路由口径与清单口径对齐（第 247 轮）----
# 由来：订单链是 wb cn 502 → trae cn 502 → wb intl 503。wb 只有国内区的号，
# 链尾那一跳 (wb, intl) 必然 503「该区域没有任何账号」，于是客户端看到的是
# 「去面板加个国际区的号」，真实原因（前面的上游 502）被整段盖掉。
s = SnapAcct({"wb": {"cn": {"m"}, "intl": {"m", "intl-only"}},
              "qd": {"cn": {"m"}, "intl": set()}},
             {"wb": 3, "qd": 1},
             {"wb": 1, "qd": 1}, {"wb": {"cn": 1}})
H.eq("R32 只有国内区号 -> (wb,intl) 不进候选链",
     router.candidates_for("m", s, CFG_CN_AUTO), [("wb", "cn"), ("qd", "cn")])
H.eq("R32b 只挂在没号那一区的模型：不筛就无路可走 -> 退回原链（仍试一次，不返回空）",
     router.candidates_for("intl-only", s, CFG_CN_AUTO), [("wb", "intl")])

s = SnapAcct({"wb": {"cn": {"m"}}, "trae": {"cn": {"m"}}},
             {"wb": 1, "trae": 9}, {"wb": 1, "trae": 0})
H.eq("R33 0 个号的引擎不进候选链（清单不发它，路由也不拿它试）",
     router.candidates_for("m", s, CFG_CN_AUTO), [("wb", "cn")])

s = SnapAcct({"wb": {"cn": {"m"}}}, {"wb": 1}, {"wb": 0, "qd": 0, "trae": 0})
H.eq("R34 三家都没号 -> 候选链退回不筛的版本（不是空链）",
     router.candidates_for("m", s, CFG_CN_AUTO), [("wb", "cn")])

H.eq("R35 realm_has_accounts：未知=True / 该区 0=False / 该区有号=True",
     (router.realm_has_accounts(snap({}, {}), "wb", "intl"),
      router.realm_has_accounts(SnapAcct({}, {}, {"wb": 1}, {"wb": {"cn": 1}}), "wb", "intl"),
      router.realm_has_accounts(SnapAcct({}, {}, {"wb": 1}, {"wb": {"cn": 1}}), "wb", "cn")),
     (True, False, True))

# 清单外的模型（拼错的模型名 / 新模型）：兜底链也不能瞎子摸象到没号的区
s = SnapAcct({}, {}, {"wb": 1}, {"wb": {"cn": 1}})
H.eq("R36 清单外模型：兜底链按账号挑区（wb 无国际区号 -> 只给 cn）",
     router.fallback_candidates(s, CFG_CN_AUTO, "wb"), [("wb", "cn")])
H.eq("R36b 账号表不知道 -> 退回 (engine, None)，老行为不变",
     router.fallback_candidates(snap({}, {}), CFG_CN_AUTO, "wb"), [("wb", None)])
H.eq("R36c 账号表说哪个区都没号 -> 也退回 (engine, None)",
     router.fallback_candidates(SnapAcct({}, {}, {"wb": 1}, {"wb": {"": 1}}),
                                CFG_CN_AUTO, "wb"), [("wb", None)])


class _UnfilteredCandidates(object):
    """阳性对照：老实现（不按账号筛）。必须能证明 R32 的绿不是判据写空。"""
    @staticmethod
    def candidates_for(model, snapshot, config):
        first = config["prefer_realm"]
        out = []
        for realm in [first] + [r for r in router.REALMS if r != first]:
            for engine in [e for e in router.ENGINES
                           if router.engine_holds(snapshot, e, realm, model)]:
                out.append((engine, realm))
        return out


_s_ctrl = SnapAcct({"wb": {"cn": {"m"}, "intl": {"m"}}}, {}, {"wb": 1}, {"wb": {"cn": 1}})
H.check("R37 阳性对照：不按账号筛时 (wb,intl) 就在链里（R32 的判据不是空的）",
        ("wb", "intl") in _UnfilteredCandidates.candidates_for("m", _s_ctrl, CFG_CN_AUTO),
        _UnfilteredCandidates.candidates_for("m", _s_ctrl, CFG_CN_AUTO))
H.check("R38 同一份快照走真实现：链里没有 (wb,intl)",
        ("wb", "intl") not in router.candidates_for("m", _s_ctrl, CFG_CN_AUTO),
        router.candidates_for("m", _s_ctrl, CFG_CN_AUTO))

# ---- 引擎没报按区数时：按账号池数（第 248 轮）----
# 由来（用户报的）：下拉里出现 claude-opus-4.7 / claude-sonnet-4.6，可这台机器
# 一个国际账号都没有 —— 那两个模型只挂在 trae 的 intl 目录里，而 trae 的 /health
# 只报总数、不报按区数，于是「没报 = 两区照发」把它们发了出去。
from unittest import mock as _mock          # noqa: E402
from hub.core import aggregator as _agg     # noqa: E402

_fake_pool = {"items": [{"engine": "trae", "realm": "cn"},
                        {"engine": "wb", "realm": "cn"},
                        {"engine": "wb", "realm": "cn"}], "errors": {}}
with _mock.patch.object(_agg, "hub_accounts", lambda ports: _fake_pool):
    H.eq("R39 按账号池数出按区账号数（trae 只有 cn、wb 两个 cn）",
         router._realm_counts_from_accounts({"wb": 1, "qd": 2, "trae": 3}),
         {"trae": {"cn": 1}, "wb": {"cn": 2}})
with _mock.patch.object(_agg, "hub_accounts", side_effect=RuntimeError("boom")):
    H.eq("R39b 账号池取不到 -> 空 dict（不猜，老行为不变）",
         router._realm_counts_from_accounts({"wb": 1}), {})

# 拿到按区数之后，清单要真的收窄（否则这个兜底等于没生效）
s = SnapAcct({"trae": {"cn": {"trae-cn-model"}, "intl": {"claude-opus-4.7"}}},
             {}, {"trae": 1}, {"trae": {"cn": 1}})
H.eq("R40 trae 只有国内号 -> intl 独有模型（claude-*）不进合并清单",
     [d["id"] for d in router.merged_model_list(s)], ["trae-cn-model"])
H.eq("R40b 发布范围只剩 cn", router.publishing_realms(s)["trae"], ["cn"])

# ---------- 三协议 model 提取 ----------
for label, doc in (("chat", {"model": "glm-5.3-flash", "messages": []}),
                   ("responses", {"model": "glm-5.3-flash", "stream": True}),
                   ("messages", {"model": "glm-5.3-flash"})):
    H.eq("R11 extract_model(%s)" % label,
         router.extract_model(json.dumps(doc).encode("utf-8")), "glm-5.3-flash")
H.eq("R12 坏 body -> 空串", router.extract_model(b"not json"), "")
H.eq("R12b 空 body -> 空串", router.extract_model(b""), "")
H.eq("R12c 非对象 body -> 空串", router.extract_model(b"[]"), "")

# ---------- 合并层设置 ----------
_tmp = tempfile.mkdtemp(prefix="hub-settings-")
_saved_env = os.environ.get("HUB_SETTINGS")
os.environ["HUB_SETTINGS"] = os.path.join(_tmp, "settings.json")
try:
    H.eq("R13 routing 默认（开 / cn / auto）", settings.routing(),
         {"enabled": True, "prefer_realm": "cn", "engine_prefer": "auto"})
    H.eq("R13b merged_keys 默认空", settings.merged_keys(), [])
    H.check("R13c 默认口令 admin 可过", settings.panel_password_default()
            and settings.verify_panel_password("admin"))
    H.check("R13d 错口令不过", not settings.verify_panel_password("nope")
            and not settings.verify_panel_password(""))

    settings.set_routing({"prefer_realm": "intl", "engine_prefer": "qd", "enabled": False})
    H.eq("R14 routing 往返", settings.routing(),
         {"enabled": False, "prefer_realm": "intl", "engine_prefer": "qd"})
    for bad, why in (({"prefer_realm": "mars"}, "非法区"), ({"engine_prefer": "gpt"}, "非法引擎"),
                     ("cn", "非对象")):
        try:
            settings.set_routing(bad); ok = False
        except ValueError:
            ok = True
        H.check("R15 set_routing 拒绝%s" % why, ok)

    settings.set_merged_keys([{"id": "km0", "name": "hebing",
                               "key": "mrg-merge-key-001", "enabled": True}])
    masked = settings.merged_keys(masked=True)
    H.eq("R16 掩码 = 前6 + 省略号 + 后4", masked[0]["key"], "mrg-me\u2026-001")
    H.check("R16b 命中合并 key", settings.match_merged_key("mrg-merge-key-001") is not None)
    H.check("R16c 未命中/空 不过", settings.match_merged_key("wrong") is None
            and settings.match_merged_key("") is None)
    # 掩码条目保存时保留旧值（面板只回传掩码）
    settings.set_merged_keys([{"id": "km0", "name": "x", "key": "mrg-me\u2026-001", "enabled": True}])
    H.check("R17 掩码回写保留旧明文", settings.match_merged_key("mrg-merge-key-001") is not None)
    out = settings.set_merged_keys([{"key": "mrg-another-key", "enabled": True}])
    H.check("R17b 新条目自动补 id", out and out[0]["id"].startswith("mkey-"), out)
    for bad, why in (([{"key": "ab", "enabled": True}], "过短 key"), ("not a list", "非列表")):
        try:
            settings.set_merged_keys(bad); ok = False
        except ValueError:
            ok = True
        H.check("R18 set_merged_keys 拒绝%s" % why, ok)

    settings.set_merged_keys([{"id": "k", "key": "zzz-key-001", "enabled": False}])
    H.check("R19 停用的 key 不命中", settings.match_merged_key("zzz-key-001") is None)

    ok, msg = settings.set_panel_password("admin", "new-pass-123")
    H.check("R20 改密成功", ok, msg)
    H.check("R20b 改密后默认标志清除、新口令可过、旧口令不过",
            (not settings.panel_password_default())
            and settings.verify_panel_password("new-pass-123")
            and not settings.verify_panel_password("admin"))
    ok, _ = settings.set_panel_password("admin", "another")
    H.check("R21 当前口令错 -> 改密被拒", not ok)
    ok, _ = settings.set_panel_password("new-pass-123", "ab")
    H.check("R21b 新口令过短 -> 被拒", not ok)
finally:
    if _saved_env is None:
        os.environ.pop("HUB_SETTINGS", None)
    else:
        os.environ["HUB_SETTINGS"] = _saved_env

H.finish()
