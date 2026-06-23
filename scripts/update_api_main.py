"""更新 api/main.py 中所有 create_llm_client 调用，添加 api_key_pool 支持。"""

# 读取文件
with open('api/main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 替换模式1：简单调用（只有 api_key）
old_pattern1 = '''client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )'''

new_pattern1 = '''client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
            api_key_pool=config.get("api_key_pool"),
        )'''

# 替换模式2：带 timeout 参数的调用
old_pattern2 = '''client = create_llm_client(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=_CONFIG_PROBE_TIMEOUT_SECONDS,'''

new_pattern2 = '''client = create_llm_client(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            api_key_pool=config.get("api_key_pool"),
            timeout=_CONFIG_PROBE_TIMEOUT_SECONDS,'''

# 替换模式3：带 timeout 参数的调用（不同缩进）
old_pattern3 = '''client = create_llm_client(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,'''

new_pattern3 = '''client = create_llm_client(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                api_key_pool=config.get("api_key_pool"),
                timeout=timeout,'''

# 执行替换
count = 0
if old_pattern1 in content:
    content = content.replace(old_pattern1, new_pattern1)
    count += 1
    print("[OK] Replaced pattern 1 (simple call)")

if old_pattern2 in content:
    content = content.replace(old_pattern2, new_pattern2)
    count += 1
    print("[OK] Replaced pattern 2 (with timeout)")

if old_pattern3 in content:
    content = content.replace(old_pattern3, new_pattern3)
    count += 1
    print("[OK] Replaced pattern 3 (with timeout, different indent)")

# 保存文件
with open('api/main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\nTotal replaced: {count} create_llm_client calls")
