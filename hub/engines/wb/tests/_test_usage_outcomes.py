"""账本四种终态各归各桶：completed / failed / client_aborted / throttled。

背景（2026-11-26 实测）：面板「失败 821、成功率 50.7%」看着像一场大故障，实际
是三类东西被混进了同一个数字：

  * client_aborted —— Cursor 自己把流断了（545 条，占「失败」的 56%）；
  * throttled      —— 模型冷却期内网关本地短路，**根本没打上游**（一个 4 小时
                      窗口被客户端每 8 秒重试撞出 249 条）；
  * failed         —— 真正的上游错误。

前两类混进来，成功率就被客户端重试行为左右，而不是被网关健康度左右。这里钉住
折叠口径：四类各进各的桶，errors 只数真失败，成功率分母 = requests + errors。

    python tests/_test_usage_outcomes.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自包含：不碰真实数据目录，也不需要 _isolated_dirs（本仓里没有这个模块）。
# 折叠函数是纯函数，不读写盘，所以只要把引擎目录放进 sys.path 就够了。
import tempfile    # noqa: E402
_TMP = tempfile.mkdtemp(prefix="wb-usage-outcomes-")
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(_TMP, "accounts"))
os.environ.setdefault("USAGE_DIR", os.path.join(_TMP, "usage"))

import wb_proxy as P    # noqa: E402


def row(outcome, **extra):
    data = {"at": 1795000000, "iso": "2026-11-26T10:00:00", "model": "m",
            "outcome": outcome}
    data.update(extra)
    return data


class FoldOutcomeTests(unittest.TestCase):
    def fold(self, rows):
        snap = P._empty_stats()
        for r in rows:
            P._fold_usage_snapshot(r, snap, None, None, None, pricing_on=False)
        return snap

    def test_four_outcomes_land_in_four_buckets(self):
        snap = self.fold([
            row("completed"),
            row("failed", status=500),
            row("client_aborted"),
            row("throttled", status=429),
        ])
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["errors"], 1, "只有 failed 该进 errors")
        self.assertEqual(snap["client_aborted"], 1)
        self.assertEqual(snap["throttled"], 1)

    def test_success_rate_denominator_excludes_aborts_and_throttles(self):
        snap = self.fold([
            row("completed"), row("completed"), row("completed"),
            row("failed", status=500),
            row("client_aborted"), row("client_aborted"),
            row("throttled", status=429), row("throttled", status=429),
        ])
        attempts = snap["requests"] + snap["errors"]
        self.assertEqual(attempts, 4, "成功率分母只该是 成功+真失败")
        self.assertEqual(round(snap["requests"] * 100.0 / attempts, 1), 75.0)

    def test_upstream_aborted_is_a_real_failure(self):
        # 上游中途断流是网关侧故障，与「客户端主动断开」相反，必须留在 errors
        snap = self.fold([row("upstream_aborted", status=502)])
        self.assertEqual(snap["errors"], 1)
        self.assertEqual(snap["client_aborted"], 0)

    def test_legacy_rows_without_outcome_still_count(self):
        # 老账本行只有 error/status，没有 outcome 字段
        snap = self.fold([{"at": 1, "model": "m", "error": True, "status": 503}])
        self.assertEqual(snap["errors"], 1)


class RateLimitedLocalTests(unittest.TestCase):
    def test_local_flag_marks_gateway_synthesised_429(self):
        exc = P.RateLimited(None, "usage exceeds frequency limit", wait=60,
                            message="pool cooling", local=True)
        self.assertTrue(exc.local)

    def test_upstream_429_is_not_local(self):
        exc = P.RateLimited(http_error=Exception("429"), detail="6004 quota",
                            wait=60)
        self.assertFalse(exc.local, "真打上游的 429 不能标成本地短路")


if __name__ == "__main__":
    unittest.main(verbosity=2)
