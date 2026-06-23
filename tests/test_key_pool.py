"""API Key 池管理模块测试。

覆盖场景：
1. Key 池创建和基本操作
2. Round-Robin 轮询策略
3. 加权轮询策略
4. 故障转移机制
5. 健康状态追踪
6. 并发安全性
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.llm_clients.key_pool import (
    KeyPool,
    KeyStatus,
    KeyHealth,
    create_pool_from_string,
    get_key_from_pool,
    report_key_success,
    report_key_failure,
    get_pool_stats,
    remove_pool,
)


class TestKeyHealth:
    def test_initial_state(self):
        """测试初始状态。"""
        health = KeyHealth(key="test-key-123")
        assert health.status == KeyStatus.HEALTHY
        assert health.success_count == 0
        assert health.failure_count == 0
        assert health.consecutive_failures == 0
        assert health.is_available() is True

    def test_record_success(self):
        """测试记录成功请求。"""
        health = KeyHealth(key="test-key")
        health.record_success(latency_ms=100.0)
        
        assert health.success_count == 1
        assert health.total_requests == 1
        assert health.consecutive_failures == 0
        assert health.avg_latency_ms > 0

    def test_record_failure(self):
        """测试记录失败请求。"""
        health = KeyHealth(key="test-key")
        health.record_failure()
        
        assert health.failure_count == 1
        assert health.total_requests == 1
        assert health.consecutive_failures == 1

    def test_consecutive_failures_state_transition(self):
        """测试连续失败后的状态转换。"""
        health = KeyHealth(key="test-key")
        
        # 连续失败 3 次 -> 降级
        for _ in range(3):
            health.record_failure()
        assert health.status == KeyStatus.DEGRADED
        
        # 连续失败 5 次 -> 不健康
        for _ in range(2):
            health.record_failure()
        assert health.status == KeyStatus.UNHEALTHY

    def test_success_resets_failures(self):
        """测试成功请求重置连续失败计数。"""
        health = KeyHealth(key="test-key")
        health.consecutive_failures = 2
        
        health.record_success(latency_ms=50.0)
        assert health.consecutive_failures == 0

    def test_cooldown_state(self):
        """测试冷却状态。"""
        health = KeyHealth(key="test-key")
        health.enter_cooldown(duration_seconds=1)  # 1 秒冷却
        
        assert health.status == KeyStatus.COOLDOWN
        assert health.is_available() is False
        
        # 冷却结束后恢复
        time.sleep(1.1)
        assert health.is_available() is True

    def test_mask_key(self):
        """测试 Key 脱敏显示。"""
        health = KeyHealth(key="sk-1234567890abcdef")
        masked = health._mask_key()
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "****" in masked


class TestKeyPool:
    def test_initialization(self):
        """测试 Key 池初始化。"""
        pool = KeyPool(keys=["key1", "key2", "key3"])
        assert len(pool.keys) == 3
        assert len(pool._health_map) == 3

    def test_get_key_round_robin(self):
        """测试 Round-Robin 轮询。"""
        pool = KeyPool(keys=["key1", "key2", "key3"], strategy="round_robin")
        
        # 应该按顺序返回（初始索引为0，第一次返回key2，然后依次轮询）
        keys = [pool.get_key() for _ in range(6)]
        # 注意：Round-Robin 从当前索引的下一个开始
        assert keys == ["key2", "key3", "key1", "key2", "key3", "key1"]

    def test_get_key_when_all_unavailable(self):
        """测试所有 Key 不可用时返回 None。"""
        pool = KeyPool(keys=["key1", "key2"])
        
        # 标记所有 Key 为不健康
        for key in pool.keys:
            pool._health_map[key].status = KeyStatus.UNHEALTHY
        
        assert pool.get_key() is None

    def test_report_success_updates_health(self):
        """测试报告成功更新健康状态。"""
        pool = KeyPool(keys=["key1"])
        pool.report_success("key1", latency_ms=100.0)
        
        health = pool._health_map["key1"]
        assert health.success_count == 1
        assert health.avg_latency_ms > 0

    def test_report_failure_updates_health(self):
        """测试报告失败更新健康状态。"""
        pool = KeyPool(keys=["key1"])
        pool.report_failure("key1", "Connection error")
        
        health = pool._health_map["key1"]
        assert health.failure_count == 1

    def test_get_stats(self):
        """测试获取统计信息。"""
        pool = KeyPool(keys=["key1", "key2"])
        pool.report_success("key1", latency_ms=100.0)
        
        stats = pool.get_stats()
        assert stats["total_keys"] == 2
        assert stats["available_keys"] == 2
        assert len(stats["keys"]) == 2

    def test_add_and_remove_key(self):
        """测试添加和移除 Key。"""
        pool = KeyPool(keys=["key1"])
        
        pool.add_key("key2")
        assert len(pool.keys) == 2
        assert "key2" in pool._health_map
        
        pool.remove_key("key1")
        assert len(pool.keys) == 1
        assert "key1" not in pool._health_map

    def test_reset_health(self):
        """测试重置健康状态。"""
        pool = KeyPool(keys=["key1"])
        pool.report_failure("key1", "Error")
        
        pool.reset_health("key1")
        health = pool._health_map["key1"]
        assert health.failure_count == 0
        assert health.status == KeyStatus.HEALTHY


class TestKeyPoolFunctions:
    def test_create_pool_from_string(self):
        """测试从字符串创建 Key 池。"""
        pool = create_pool_from_string("test-pool", "key1,key2,key3")
        
        assert len(pool.keys) == 3
        assert "key1" in pool.keys
        assert "key2" in pool.keys
        assert "key3" in pool.keys

    def test_create_pool_from_string_with_spaces(self):
        """测试从带空格的字符串创建 Key 池。"""
        pool = create_pool_from_string("test-pool", "key1, key2 , key3")
        
        assert len(pool.keys) == 3
        assert all(key.strip() == key for key in pool.keys)

    def test_create_pool_from_string_empty(self):
        """测试从空字符串创建 Key 池抛出异常。"""
        with pytest.raises(ValueError, match="没有有效的 Key"):
            create_pool_from_string("test-pool", "")

    def test_get_key_from_pool(self):
        """测试从池中获取 Key。"""
        create_pool_from_string("test-pool", "key1,key2")
        
        key = get_key_from_pool("test-pool")
        assert key in ["key1", "key2"]

    def test_get_key_from_nonexistent_pool(self):
        """测试从不存在的池获取 Key 返回 None。"""
        key = get_key_from_pool("nonexistent-pool")
        assert key is None

    def test_report_success_and_failure(self):
        """测试报告成功和失败。"""
        # 清理之前的测试池
        remove_pool("test-pool")
        
        create_pool_from_string("test-pool", "key1,key2")
        
        report_key_success("test-pool", "key1", latency_ms=100.0)
        report_key_failure("test-pool", "key2", "Error")
        
        stats = get_pool_stats("test-pool")
        assert stats is not None
        assert len(stats["keys"]) == 2

    def test_remove_pool(self):
        """测试删除池。"""
        create_pool_from_string("test-pool", "key1")
        remove_pool("test-pool")
        
        assert get_key_from_pool("test-pool") is None

    def test_concurrent_access(self):
        """测试并发访问安全性。"""
        # 清理之前的测试池
        remove_pool("test-pool")
        
        pool = create_pool_from_string("test-pool", "key1,key2,key3")
        
        results = []
        errors = []
        lock = threading.Lock()
        
        def worker():
            try:
                for _ in range(100):
                    key = pool.get_key()
                    if key:
                        with lock:
                            results.append(key)
                        pool.report_success(key, latency_ms=50.0)
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 1000  # 10 threads * 100 iterations


class TestKeyPoolClientWrapper:
    @patch("tradingagents.llm_clients.key_pool_client.get_key_from_pool")
    @patch("tradingagents.llm_clients.key_pool_client.report_key_success")
    def test_invoke_success(self, mock_report_success, mock_get_key):
        """测试成功调用。"""
        from tradingagents.llm_clients.key_pool_client import KeyPoolLLMWrapper
        
        mock_get_key.return_value = "test-key-123"
        
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = "Hello, World!"
        
        wrapper = KeyPoolLLMWrapper(mock_llm, "test-pool")
        result = wrapper.invoke("Hi")
        
        assert result == "Hello, World!"
        mock_report_success.assert_called_once()
        mock_llm.invoke.assert_called_once_with(input="Hi", config=None)

    @patch("tradingagents.llm_clients.key_pool_client.get_key_from_pool")
    @patch("tradingagents.llm_clients.key_pool_client.report_key_failure")
    def test_invoke_failure_retries(self, mock_report_failure, mock_get_key):
        """测试失败重试。"""
        from tradingagents.llm_clients.key_pool_client import KeyPoolLLMWrapper
        
        mock_get_key.return_value = "test-key-123"
        
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = [
            Exception("Connection error"),
            Exception("Timeout"),
            "Success"
        ]
        
        wrapper = KeyPoolLLMWrapper(mock_llm, "test-pool")
        result = wrapper.invoke("Hi")
        
        assert result == "Success"
        assert mock_report_failure.call_count == 2  # 前两次失败

    @patch("tradingagents.llm_clients.key_pool_client.get_key_from_pool")
    def test_invoke_no_keys_available(self, mock_get_key):
        """测试没有可用 Key 时抛出异常。"""
        from tradingagents.llm_clients.key_pool_client import KeyPoolLLMWrapper
        
        mock_get_key.return_value = None
        
        mock_llm = MagicMock()
        wrapper = KeyPoolLLMWrapper(mock_llm, "test-pool")
        
        with pytest.raises(ValueError, match="没有可用的 Key"):
            wrapper.invoke("Hi")
