"""修复 trading_graph.py 中的格式问题。"""

# 读取文件
with open('tradingagents/graph/trading_graph.py', 'r', encoding='gbk') as f:
    content = f.read()

# 修复格式问题：将 `n 替换为实际的换行符
content = content.replace('`n', '\n')

# 保存文件
with open('tradingagents/graph/trading_graph.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("成功修复文件格式")

# 验证修复结果
with open('tradingagents/graph/trading_graph.py', 'r', encoding='utf-8') as f:
    content = f.read()
    
# 检查函数是否正确
if 'api_key_pool = self.config.get("api_key_pool")' in content:
    print("✓ API Key 池支持已添加")
else:
    print("✗ API Key 池支持未找到")
