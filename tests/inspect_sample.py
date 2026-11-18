"""打印样本页第一条观影记录的原始 HTML，用于确定解析规则。"""
import re
import sys
from pathlib import Path

sample = Path(sys.argv[1] if len(sys.argv) > 1 else "../samples/collect_ahbei.html")
html = sample.read_text(encoding="utf-8")

blocks = re.split(r'<div class="item comment-item"', html)[1:]
print("items:", len(blocks))

if blocks:
    b = blocks[0]
    # 截断过长的图片地址，便于阅读
    b = re.sub(r'(src="[^"]{50})[^"]*(")', r"\1...\2", b)
    print(b[:3000])
    print("=" * 60)
    print("最后一个 item 尾部:")
    print(blocks[-1][:600])
