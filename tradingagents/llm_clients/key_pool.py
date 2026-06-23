"""
API Key 池管理模块：实现多 Key 的轮询、负载均衡和故障转移。

核心功能：
1. Key 轮询：Round-Robin 方式分配 Key，避免单个 Key 过载
2. 负载均衡：根据使用情况动态调整 Key 分配
3. 故障转移：自动跳过失败的 Key，切换到下一个可用 Key
4. 并发安全：线程安全的 Key 访问机制
5. 健康检查：监控 Key 的可用性状态

使用场景：
- LLM API 并发限制优化
- 多账号负载均衡
- 自动故障转移
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

_logger = logging.getLogger(__name__)


class KeyStatus(Enum):
    """Key 状态枚举。"""
    HEALTHY = "healthy"      # 健康可用
    DEGRADED = "degraded"    # 降级（响应慢但可用）
    UNHEALTHY = "unhealthy"  # 不可用
    COOLDOWN = "cooldown"    # 冷却中（临时不可用）


@dataclass
class KeyHealth:
    """Key 健康状态信息。"""
    key: str
    status: KeyStatus = KeyStatus.HEALTHY
    success_count: int = 0
    failure_count: int = 0
    last_success_time: float = 0
    last_failure_time: float = 0
    total_requests: int = 0
    avg_latency_ms: float = 0
    consecutive_failures: int = 0
    
    def record_success(self, latency_ms: float):
        """记录成功请求。"""
        self.success_count += 1
        self.total_requests += 1
        self.consecutive_failures = 0
        self.last_success_time = time.time()
        
        # 更新平均延迟（指数移动平均）
        alpha = 0.3
        self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms
        
        # 如果之前是降级或冷却状态，恢复健康
        if self.status in (KeyStatus.DEGRADED, KeyStatus.COOLDOWN):
            self.status = KeyStatus.HEALTHY
            _logger.info(f"Key {self._mask_key()} 恢复为健康状态")
    
    def record_failure(self):
        """记录失败请求。"""
        self.failure_count += 1
        self.total_requests += 1
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        
        # 根据连续失败次数更新状态
        if self.consecutive_failures >= 5:
            self.status = KeyStatus.UNHEALTHY
            _logger.warning(f"Key {self._mask_key()} 连续失败 {self.consecutive_failures} 次，标记为不健康")
        elif self.consecutive_failures >= 3:
            self.status = KeyStatus.DEGRADED
            _logger.warning(f"Key {self._mask_key()} 连续失败 {self.consecutive_failures} 次，标记为降级")
    
    def enter_cooldown(self, duration_seconds: int = 60):
        """进入冷却状态。"""
        self.status = KeyStatus.COOLDOWN
        self._cooldown_until = time.time() + duration_seconds
        _logger.info(f"Key {self._mask_key()} 进入冷却状态 {duration_seconds} 秒")
    
    def is_available(self) -> bool:
        """检查 Key 是否可用。"""
        if self.status == KeyStatus.UNHEALTHY:
            return False
        
        if self.status == KeyStatus.COOLDOWN:
            # 检查冷却时间是否结束
            if time.time() > getattr(self, '_cooldown_until', 0):
                self.status = KeyStatus.HEALTHY
                _logger.info(f"Key {self._mask_key()} 冷却结束，恢复为健康状态")
                return True
            return False
        
        return True
    
    def _mask_key(self) -> str:
        """脱敏 Key 显示。"""
        if len(self.key) > 8:
            return f"{self.key[:4]}****{self.key[-4:]}"
        return "****"
    
    @property
    def weight(self) -> float:
        """计算权重（用于加权轮询）。"""
        if not self.is_available():
            return 0
        
        # 基础权重
        weight = 1.0
        
        # 根据状态调整
        if self.status == KeyStatus.DEGRADED:
            weight *= 0.5
        
        # 根据延迟调整（延迟越低权重越高）
        if self.avg_latency_ms > 0:
            latency_factor = max(0.5, min(1.5, 1000 / self.avg_latency_ms))
            weight *= latency_factor
        
        # 根据成功率调整
        if self.total_requests > 0:
            success_rate = self.success_count / self.total_requests
            weight *= success_rate
        
        return weight


@dataclass
class KeyPool:
    """API Key 池。
    
    管理多个 API Key，提供负载均衡和故障转移。
    
    特性：
    - Round-Robin 轮询分配
    - 加权轮询（根据健康状态）
    - 自动故障转移
    - 线程安全访问
    """
    keys: List[str]
    strategy: str = "round_robin"  # round_robin, weighted, random
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _current_index: int = 0
    _health_map: Dict[str, KeyHealth] = field(default_factory=dict)
    
    def __post_init__(self):
        """初始化后处理。"""
        for key in self.keys:
            if key not in self._health_map:
                self._health_map[key] = KeyHealth(key=key)
    
    def get_key(self) -> Optional[str]:
        """获取下一个可用的 Key。
        
        Returns:
            可用的 API Key，如果所有 Key 都不可用则返回 None
        """
        with self._lock:
            available_keys = self._get_available_keys()
            if not available_keys:
                _logger.warning("所有 API Key 都不可用")
                return None
            
            return self._select_key(available_keys)
    
    def report_success(self, key: str, latency_ms: float = 0):
        """报告 Key 使用成功。"""
        with self._lock:
            health = self._health_map.get(key)
            if health:
                health.record_success(latency_ms)
                _logger.debug(f"Key {health._mask_key()} 请求成功，延迟 {latency_ms:.1f}ms")
    
    def report_failure(self, key: str, error: Optional[str] = None):
        """报告 Key 使用失败。"""
        with self._lock:
            health = self._health_map.get(key)
            if health:
                health.record_failure()
                _logger.warning(f"Key {health._mask_key()} 请求失败: {error}")
                
                # 如果连续失败超过阈值，进入冷却
                if health.consecutive_failures >= 3:
                    cooldown_duration = min(300, 60 * (health.consecutive_failures - 2))
                    health.enter_cooldown(cooldown_duration)
    
    def _get_available_keys(self) -> List[str]:
        """获取所有可用的 Key。"""
        return [key for key in self.keys if self._health_map[key].is_available()]
    
    def _select_key(self, available_keys: List[str]) -> str:
        """选择下一个 Key。"""
        if self.strategy == "round_robin":
            return self._select_round_robin(available_keys)
        elif self.strategy == "weighted":
            return self._select_weighted(available_keys)
        elif self.strategy == "random":
            return self._select_random(available_keys)
        else:
            return self._select_round_robin(available_keys)
    
    def _select_round_robin(self, available_keys: List[str]) -> str:
        """Round-Robin 选择。"""
        if not available_keys:
            return None
        
        # 简单的 Round-Robin：返回下一个可用的 Key
        # 找到当前索引对应的 Key 是否可用
        current_key = self.keys[self._current_index % len(self.keys)]
        
        # 如果当前 Key 可用，返回下一个
        if current_key in available_keys:
            idx = available_keys.index(current_key)
            next_idx = (idx + 1) % len(available_keys)
            selected_key = available_keys[next_idx]
        else:
            # 否则返回第一个可用的
            selected_key = available_keys[0]
        
        # 更新全局索引（指向选中的 Key）
        try:
            self._current_index = self.keys.index(selected_key)
        except ValueError:
            self._current_index = 0
        
        return selected_key
    
    def _select_weighted(self, available_keys: List[str]) -> str:
        """加权选择。"""
        import random
        
        # 计算权重列表
        weights = [self._health_map[key].weight for key in available_keys]
        total_weight = sum(weights)
        
        if total_weight == 0:
            return random.choice(available_keys)
        
        # 加权随机选择
        r = random.uniform(0, total_weight)
        cumulative = 0
        for key, weight in zip(available_keys, weights):
            cumulative += weight
            if r <= cumulative:
                return key
        
        return available_keys[-1]
    
    def _select_random(self, available_keys: List[str]) -> str:
        """随机选择。"""
        import random
        return random.choice(available_keys)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取 Key 池统计信息。"""
        with self._lock:
            stats = {
                "total_keys": len(self.keys),
                "available_keys": len(self._get_available_keys()),
                "strategy": self.strategy,
                "keys": []
            }
            
            for key in self.keys:
                health = self._health_map[key]
                stats["keys"].append({
                    "key_mask": health._mask_key(),
                    "status": health.status.value,
                    "success_count": health.success_count,
                    "failure_count": health.failure_count,
                    "total_requests": health.total_requests,
                    "avg_latency_ms": round(health.avg_latency_ms, 1),
                    "consecutive_failures": health.consecutive_failures,
                    "weight": round(health.weight, 3)
                })
            
            return stats
    
    def reset_health(self, key: Optional[str] = None):
        """重置健康状态。"""
        with self._lock:
            if key:
                if key in self._health_map:
                    self._health_map[key] = KeyHealth(key=key)
                    _logger.info(f"重置 Key {self._health_map[key]._mask_key()} 的健康状态")
            else:
                for k in self.keys:
                    self._health_map[k] = KeyHealth(key=k)
                _logger.info("重置所有 Key 的健康状态")
    
    def remove_key(self, key: str):
        """从池中移除 Key。"""
        with self._lock:
            if key in self.keys:
                self.keys.remove(key)
                if key in self._health_map:
                    del self._health_map[key]
                _logger.info(f"从 Key 池中移除 Key")
    
    def add_key(self, key: str):
        """添加 Key 到池中。"""
        with self._lock:
            if key not in self.keys:
                self.keys.append(key)
                self._health_map[key] = KeyHealth(key=key)
                _logger.info(f"添加新 Key 到 Key 池")


# 全局 Key 池管理器
_key_pools: Dict[str, KeyPool] = {}
_pools_lock = threading.Lock()


def get_key_pool(pool_id: str, keys: Optional[List[str]] = None, strategy: str = "round_robin") -> KeyPool:
    """获取或创建 Key 池。
    
    Args:
        pool_id: 池 ID（通常是用户 ID 或配置 ID）
        keys: API Key 列表（仅在创建新池时使用）
        strategy: 选择策略（round_robin, weighted, random）
    
    Returns:
        KeyPool 实例
    """
    with _pools_lock:
        if pool_id not in _key_pools:
            if not keys:
                raise ValueError(f"创建新的 Key 池需要提供 keys 参数")
            _key_pools[pool_id] = KeyPool(keys=keys, strategy=strategy)
            _logger.info(f"创建新的 Key 池 {pool_id}，包含 {len(keys)} 个 Key")
        return _key_pools[pool_id]


def get_key_from_pool(pool_id: str) -> Optional[str]:
    """从指定池中获取 Key。
    
    Args:
        pool_id: 池 ID
    
    Returns:
        可用的 API Key
    """
    with _pools_lock:
        pool = _key_pools.get(pool_id)
        if not pool:
            return None
        return pool.get_key()


def report_key_success(pool_id: str, key: str, latency_ms: float = 0):
    """报告 Key 使用成功。"""
    with _pools_lock:
        pool = _key_pools.get(pool_id)
        if pool:
            pool.report_success(key, latency_ms)


def report_key_failure(pool_id: str, key: str, error: Optional[str] = None):
    """报告 Key 使用失败。"""
    with _pools_lock:
        pool = _key_pools.get(pool_id)
        if pool:
            pool.report_failure(key, error)


def get_pool_stats(pool_id: str) -> Optional[Dict[str, Any]]:
    """获取 Key 池统计信息。"""
    with _pools_lock:
        pool = _key_pools.get(pool_id)
        if pool:
            return pool.get_stats()
        return None


def remove_pool(pool_id: str):
    """删除 Key 池。"""
    with _pools_lock:
        if pool_id in _key_pools:
            del _key_pools[pool_id]
            _logger.info(f"删除 Key 池 {pool_id}")


# 便捷函数：从字符串创建 Key 池
def create_pool_from_string(
    pool_id: str,
    keys_string: str,
    strategy: str = "round_robin"
) -> KeyPool:
    """从逗号分隔的字符串创建 Key 池。
    
    Args:
        pool_id: 池 ID
        keys_string: 逗号分隔的 Key 字符串
        strategy: 选择策略
    
    Returns:
        KeyPool 实例
    """
    keys = [k.strip() for k in keys_string.split(",") if k.strip()]
    if not keys:
        raise ValueError("Key 字符串中没有有效的 Key")
    
    return get_key_pool(pool_id, keys, strategy)


def get_key_for_request(pool_id: str, strategy: str = "round_robin") -> Optional[str]:
    """为请求获取 Key（带自动初始化）。
    
    如果池不存在，返回 None。实际使用中应该先调用 get_key_pool 或 create_pool_from_string。
    
    Args:
        pool_id: 池 ID
        strategy: 选择策略
    
    Returns:
        可用的 API Key
    """
    return get_key_from_pool(pool_id)
