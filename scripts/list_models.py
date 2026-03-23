"""
列出当前 API Key 可用的所有模型，重点显示支持 embedContent 的模型。
用于诊断服务器端模型不可用的问题。

用法：python -m scripts.list_models
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import settings
from google import genai

client = genai.Client(api_key=settings.gemini_api_key)

print(f"API Key: ...{settings.gemini_api_key[-6:]}\n")

embed_models = []
gen_models = []

for m in client.models.list():
    name = m.name
    actions = getattr(m, 'supported_actions', None) or []
    if 'embedContent' in actions:
        embed_models.append(name)
    if 'generateContent' in actions:
        gen_models.append(name)

print("=== 支持 embedContent 的模型 ===")
if embed_models:
    for m in embed_models:
        print(f"  {m}")
else:
    print("  （无）— 此 API Key 不支持任何 embedding 模型！")

print()
print("=== 支持 generateContent 的模型 ===")
for m in gen_models:
    print(f"  {m}")
