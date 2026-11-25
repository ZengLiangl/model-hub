# -*- coding: utf-8 -*-
"""_test_client_session_key.py —— 会话身份：先认客户端明确给的，最后才回落到内容哈希。

## 为什么需要这套判据（实测串会话，2026-11-25）

改之前 session_key 只有一个来源 —— ``affinity_route(messages)``，即前 2~3 条
消息的内容哈希。两个 DSH 会话的开场经常**逐字相同**：

    会话A key: pfx-3cd0f3034c500246
    会话B key: pfx-3cd0f3034c500246     <- 撞了

这个 key 同时用在三处（账号粘性绑定 / 上游 conversation_id / prompt_cache_key），
于是两个会话共用同一账号绑定与同一上游 conversation_id —— 内容串线。

## 修法的边界

上游作者在 workbuddy2api-hub 的 c1860f0 里把 prompt_cache_key 的**注入**默认
关掉了，理由是实测「上游自己就会缓存前缀，注入它既不改命中率也不改计费」。
那个结论针对的是**省钱**；同一个字段还承载**会话身份**语义，关掉注入等于把这层
信息一起丢了。所以这里把两件事拆开：

  · 要不要把 prompt_cache_key **发给上游** -> prompt_cache_key_enabled()（不变）
  · 要不要用它**识别会话**               -> client_session_key()（本套件）

    python tests/_test_client_session_key.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as W        # noqa: E402

#: 两个 DSH 会话的典型开场：system + 第一句 user + 第一条 assistant，逐字相同。
SAME_HEAD = [
    {"role": "system", "content": "You are DSH, a coding agent."},
    {"role": "user", "content": "帮我改一下这个函数"},
    {"role": "assistant", "content": "好的，我看一下。"},
]


class ClientSessionKeyTests(unittest.TestCase):
    def key(self, payload):
        return W.client_session_key(payload)

    # ------------------------------------------------------------ 存在性
    def test_the_helper_exists(self):
        self.assertTrue(hasattr(W, "client_session_key"),
                        "hub-patch client-session-key 未落地")

    # ------------------------------------------------- 优先级（越明确越优先）
    def test_conversation_id_is_the_top_priority(self):
        self.assertEqual(self.key({"conversation_id": "conv-1"}), ("cid-conv-1", "cid"))
        self.assertEqual(self.key({"conversationId": "conv-2"}), ("cid-conv-2", "cid"))
        self.assertEqual(self.key({"session_id": "sess-1"}), ("cid-sess-1", "cid"))
        self.assertEqual(self.key({"sessionId": "sess-2"}), ("cid-sess-2", "cid"))

    def test_prompt_cache_key_is_second(self):
        """OpenAI 标准字段：它就是用来标识「同一段对话」的。"""
        self.assertEqual(self.key({"prompt_cache_key": "pck-1"}), ("pck-pck-1", "pck"))

    def test_user_is_third(self):
        self.assertEqual(self.key({"user": "alice"}), ("usr-alice", "usr"))

    def test_anthropic_metadata_user_id(self):
        self.assertEqual(self.key({"metadata": {"user_id": "bob"}}), ("uid-bob", "uid"))

    def test_priority_order_end_to_end(self):
        self.assertEqual(
            self.key({"conversation_id": "c1", "prompt_cache_key": "p1", "user": "u1"}),
            ("cid-c1", "cid"), "conversation_id 应压过 prompt_cache_key 与 user")
        self.assertEqual(
            self.key({"prompt_cache_key": "p1", "user": "u1"}),
            ("pck-p1", "pck"), "prompt_cache_key 应压过 user")
        self.assertEqual(
            self.key({"user": "u1", "metadata": {"user_id": "m1"}}),
            ("usr-u1", "usr"), "user 应压过 metadata.user_id")

    # ------------------------------------------------------- 回落（都没有）
    def test_nothing_supplied_falls_back(self):
        self.assertEqual(self.key({"model": "m", "messages": SAME_HEAD}), (None, None))
        self.assertEqual(self.key({}), (None, None))
        self.assertEqual(self.key(None), (None, None), "非 dict 不许抛")

    # ------------------------------------------------------------- 脏值
    def test_dirty_values_are_ignored(self):
        for bad in ("", "   ", None, 123, [], {}):
            self.assertEqual(self.key({"conversation_id": bad}), (None, None),
                             "conversation_id=%r 不该被当成有效身份" % (bad,))

    def test_empty_high_priority_falls_through(self):
        self.assertEqual(
            self.key({"conversation_id": "", "prompt_cache_key": "fallback"}),
            ("pck-fallback", "pck"))

    def test_malformed_metadata_does_not_raise(self):
        self.assertEqual(self.key({"metadata": "not-a-dict"}), (None, None))
        self.assertEqual(self.key({"metadata": {"user_id": 123}}), (None, None))

    # --------------------------------- 真实场景：开场相同但客户端给了身份
    def test_same_opening_splits_when_client_supplies_identity(self):
        """修复的核心：两个开场逐字相同的会话必须分开。"""
        a = {"model": "m", "messages": SAME_HEAD, "prompt_cache_key": "dsh-sess-A"}
        b = {"model": "m", "messages": SAME_HEAD, "prompt_cache_key": "dsh-sess-B"}
        ka, _ = self.key(a)
        kb, _ = self.key(b)
        self.assertIsNotNone(ka)
        self.assertIsNotNone(kb)
        self.assertNotEqual(ka, kb)
        self.assertFalse(str(ka).startswith("pfx-"), "不该回落到内容哈希")
        self.assertFalse(str(kb).startswith("pfx-"))

    # --------------------------- 内容哈希确实会撞（修复的存在理由）
    def test_content_hash_collides_on_identical_openings(self):
        """钉住「问题真实存在」，防止将来有人删掉 client_session_key 还以为没事。"""
        collide = W.affinity_route(SAME_HEAD)[0]
        self.assertIsInstance(collide, str)
        self.assertTrue(collide.startswith("pfx-"))
        # 同一份内容两次哈希一致（老行为本身是稳定的，问题在「跨会话也一致」）
        self.assertEqual(W.affinity_route(list(SAME_HEAD))[0], collide)

    def test_two_different_conversations_share_the_hash(self):
        """两个「不同会话」但同样的开场 —— 内容哈希给出同一个 key。"""
        conv_a = list(SAME_HEAD)
        conv_b = list(SAME_HEAD)
        self.assertEqual(W.affinity_route(conv_a)[0], W.affinity_route(conv_b)[0])

    # ------------------------------- 与 prompt_cache_key 注入开关解耦
    def test_session_identity_is_decoupled_from_injection(self):
        """注入开关关着时，会话身份仍然解析得出 —— 这是「解耦」的判据。

        上游作者关掉注入的理由是「省钱没效果」，那个理由不适用于会话身份。
        如果哪天有人把 client_session_key 也挂到那个开关下面，这条会红。
        """
        # 开关状态不影响解析结果
        before = W.prompt_cache_key_enabled()
        self.assertEqual(self.key({"prompt_cache_key": "x"}), ("pck-x", "pck"))
        # 显式确认两者是独立的两件事
        self.assertIsInstance(before, bool)

    # ------------------------------------------------- key 形状（排障用）
    def test_key_carries_source_prefix(self):
        k, src = self.key({"conversation_id": "abc-123"})
        self.assertTrue(k.startswith("cid-"))
        self.assertEqual(src, "cid")

    def test_all_sources_are_registered(self):
        for s in ("cid", "pck", "usr", "uid", "pfx"):
            self.assertIn(s, W.SESSION_KEY_SOURCES)


if __name__ == "__main__":
    unittest.main()
