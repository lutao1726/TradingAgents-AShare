"""演示 Key 池日志输出效果。

运行此脚本可以看到请求时使用的 Key 信息。
"""
import logging
import sys
from pathlib import Path

# 确保能找到项目模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tradingagents.llm_clients.key_pool import create_pool_from_string
from tradingagents.llm_clients.key_pool_client import KeyPoolLLMWrapper

# 配置日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 创建 Key 池
pool_id = "demo-pool"
keys = ["sk-1234567890abcdef", "sk-abcdef1234567890", "sk-fedcba0987654321"]
pool = create_pool_from_string(pool_id, ",".join(keys))

# 模拟 LLM
class MockLLM:
    def invoke(self, input, config=None, **kwargs):
        return f"Response to: {input}"

# 创建包装器
wrapper = KeyPoolLLMWrapper(MockLLM(), pool_id)

# 模拟多次请求
print("\n" + "="*60)
print("Key 池日志演示")
print("="*60 + "\n")

for i in range(5):
    try:
        result = wrapper.invoke(f"Request {i+1}")
        print(f"结果: {result}")
    except Exception as e:
        print(f"错误: {e}")

print("\n" + "="*60)
print("日志演示完成")
print("="*60)
