"""AI 叙事：读取 profile.json + 短评，调用 OpenAI 兼容 API 生成「观影人格」。

配置（环境变量，均可选）:
  DOUBAN_WRAPPED_API_KEY / OPENAI_API_KEY   API 密钥（未设置则跳过 AI 叙事）
  DOUBAN_WRAPPED_BASE_URL                   默认 https://api.openai.com/v1
  DOUBAN_WRAPPED_MODEL                      默认 gpt-4o-mini

输出 data/narrative.json，report 生成报告页时会自动并入。
"""

from __future__ import annotations

import csv
import json
import os
import random
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "你是一位毒舌但温柔的影评人老友，擅长从一个人的观影记录里读出这个人的性格。"
    "请根据给出的豆瓣观影统计数据与短评样本，用中文输出一个 JSON 对象（不要多余文字）：\n"
    '{"persona": "250字以内的第二人称观影人格总结，具体、有画面感、引用其短评风格",\n'
    ' "tags": ["三个各不超过6字的品味标签"],\n'
    ' "one_liner": "一句话锐评（30字以内）"}'
)


def _collect_comments(csv_file: Path, limit: int = 30) -> list[dict]:
    samples: list[dict] = []
    with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "watched" and r.get("comment"):
                samples.append({
                    "title": r.get("title", ""),
                    "my_rating": r.get("my_rating", ""),
                    "douban_rating": r.get("douban_rating", ""),
                    "comment": r["comment"][:80],
                })
    random.shuffle(samples)
    return samples[:limit]


def build_prompt(profile: dict, comments: list[dict]) -> str:
    slim = {
        "overview": profile.get("overview"),
        "rating_distribution": profile.get("rating_distribution"),
        "unrated": profile.get("unrated"),
        "top_genres": (profile.get("major_genre_stats") or [])[:8],
        "top_countries": (profile.get("country_stats") or [])[:8],
        "top_directors": (profile.get("director_stats") or [])[:8],
        "decades": profile.get("decade_stats"),
        "top_rated_titles": [f["title"] for f in (profile.get("top_rated") or [])[:10]],
        "gems": [f["title"] for f in (profile.get("gems") or [])[:8]],
        "strict_picks": [f["title"] for f in (profile.get("strict_picks") or [])[:8]],
        "comment_samples": comments,
    }
    return (
        "以下是我全部豆瓣观影记录的统计画像和部分短评，请生成我的观影人格：\n"
        + json.dumps(slim, ensure_ascii=False, indent=1)
    )


def call_llm(prompt: str, api_key: str, base_url: str, model: str,
             timeout: float = 120.0) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def run(profile_file: str = "data/profile.json", csv_file: str = "data/movies.csv",
        out_file: str = "data/narrative.json") -> int:
    api_key = os.environ.get("DOUBAN_WRAPPED_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("未设置 DOUBAN_WRAPPED_API_KEY / OPENAI_API_KEY，跳过 AI 叙事。")
        print("配置任意 OpenAI 兼容接口后再运行: python -m douban_wrapped ai")
        return 1
    base_url = os.environ.get("DOUBAN_WRAPPED_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("DOUBAN_WRAPPED_MODEL", DEFAULT_MODEL)

    src = Path(profile_file)
    if not src.exists():
        print(f"未找到 {src}，请先运行 analyze。")
        return 1
    profile = json.loads(src.read_text(encoding="utf-8"))
    comments = _collect_comments(Path(csv_file)) if Path(csv_file).exists() else []
    prompt = build_prompt(profile, comments)

    print(f"调用 {model} @ {base_url} 生成观影人格...")
    try:
        narrative = call_llm(prompt, api_key, base_url, model)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        print(f"AI 调用失败: {e}")
        return 1
    if not isinstance(narrative, dict) or "persona" not in narrative:
        print(f"AI 返回格式异常: {str(narrative)[:200]}")
        return 1

    dst = Path(out_file)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(narrative, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"观影人格已生成 -> {dst}")
    print(f"标签: {narrative.get('tags')}")
    print(f"锐评: {narrative.get('one_liner')}")
    return 0
