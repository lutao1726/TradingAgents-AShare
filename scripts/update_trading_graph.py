"""修改 trading_graph.py 以支持 API Key 池。"""
import re

# 读取文件（尝试不同编码）
encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1']
content = None

for encoding in encodings:
    try:
        with open('tradingagents/graph/trading_graph.py', 'r', encoding=encoding) as f:
            content = f.read()
        print(f"成功使用 {encoding} 编码读取文件")
        break
    except UnicodeDecodeError:
        continue

if content is None:
    print("无法读取文件")
    exit(1)

# 旧的函数内容
old_func = '''    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        elif provider in ("anthropic", "xai"):
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        return kwargs'''

# 新的函数内容
new_func = '''    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        elif provider in ("anthropic", "xai"):
            api_key = self.config.get("api_key")
            if api_key:
                kwargs["api_key"] = api_key

        # 处理 API Key 池（如果有）
        api_key_pool = self.config.get("api_key_pool")
        if api_key_pool:
            kwargs["api_key_pool"] = api_key_pool

        return kwargs'''

# 替换
if old_func in content:
    content = content.replace(old_func, new_func)
    with open('tradingagents/graph/trading_graph.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("成功更新 _get_provider_kwargs 函数")
else:
    print("未找到旧函数内容，可能已经更新")
    # 打印部分内容以调试
    print("\n文件中 _get_provider_kwargs 函数的内容：")
    match = re.search(r'def _get_provider_kwargs.*?return kwargs', content, re.DOTALL)
    if match:
        print(match.group())
