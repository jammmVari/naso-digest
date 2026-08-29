#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Naso 晨报 · 纯标准库，零第三方依赖
思路来自 zziying/ai-morning-digest（CC BY 4.0）

流程：读 seed.txt -> 拉免费源（Google News RSS / HN Algolia / arXiv）
     -> 有 ANTHROPIC_API_KEY 就让便宜模型削土豆压缩，没有就发毛坯
     -> 覆盖写 digest.md（快照制），history.json 记 180 天去重
"""

import json
import os
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import email.utils
from datetime import datetime, timedelta, timezone

# ---------- 常量 ----------
NOW = datetime.now(timezone.utc)
SEED_FILE = "seed.txt"
OUT_FILE = "digest.md"
HISTORY_FILE = "history.json"
FRESH_HOURS = 36          # 新闻类只要最近 36 小时
ARXIV_FRESH_HOURS = 96    # 论文放宽到 4 天（arXiv 分批放出）
HISTORY_DAYS = 180        # 去重记忆有效期
TITLE_MAX = 160
UA = {"User-Agent": "Mozilla/5.0 (naso-morning-digest; personal use)"}


# ---------- 基础工具 ----------
def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_title(t):
    t = html.unescape(" ".join((t or "").split()))
    return t[:TITLE_MAX] + ("…" if len(t) > TITLE_MAX else "")


def parse_pubdate(s):
    try:
        return email.utils.parsedate_to_datetime(s)
    except Exception:
        return None


def fresh(items, hours):
    """有时间戳的按窗口过滤；没有时间戳的宽容保留。"""
    out = []
    for it in items:
        dt = it.get("dt")
        if dt is None or (NOW - dt) <= timedelta(hours=hours):
            out.append(it)
    return out


# ---------- 数据源（每个都可独立失败，一个塌了不影响整份报纸）----------
def rss_items(url, limit=10):
    items = []
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        print(f"[warn] RSS 失败 {url}: {e}")
        return items
    for it in root.iter("item"):
        title = clean_title(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        dt = parse_pubdate(it.findtext("pubDate") or "")
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
        if len(items) >= limit:
            break
    return items


def google_news_search(query, limit=8):
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return fresh(rss_items(url, limit), FRESH_HOURS)


def google_news_top(limit=8):
    url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    return fresh(rss_items(url, limit), FRESH_HOURS)


def google_news_science(limit=6):
    url = ("https://news.google.com/rss/headlines/section/topic/SCIENCE"
           "?hl=en-US&gl=US&ceid=US:en")
    return fresh(rss_items(url, limit), FRESH_HOURS)


def hn_front(limit=6):
    items = []
    try:
        data = json.loads(fetch(
            "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=15"))
    except Exception as e:
        print(f"[warn] HN 失败: {e}")
        return items
    hits = sorted(data.get("hits", []),
                  key=lambda h: h.get("points") or 0, reverse=True)
    for h in hits[:limit]:
        title = clean_title(h.get("title"))
        if not title:
            continue
        pts = h.get("points") or 0
        com = h.get("num_comments") or 0
        link = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        items.append({"title": f"[{pts}分/{com}评] {title}", "link": link, "dt": None})
    return items


def arxiv_search(query, limit=6):
    words = query.split()[:4]
    sq = "+AND+".join("all:" + urllib.parse.quote(w) for w in words)
    url = (f"http://export.arxiv.org/api/query?search_query={sq}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results=12")
    items = []
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        print(f"[warn] arXiv 失败: {e}")
        return items
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        title = clean_title(e.findtext("a:title", default="", namespaces=ns))
        link = (e.findtext("a:id", default="", namespaces=ns) or "").strip()
        pub = e.findtext("a:published", default="", namespaces=ns)
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            dt = None
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
        if len(items) >= limit:
            break
    return fresh(items, ARXIV_FRESH_HOURS)


# ---------- seed ----------
def load_seed():
    """每行：版块名 | 搜索词   搜索词以 arxiv: 开头则走论文库"""
    sections = []
    if not os.path.exists(SEED_FILE):
        return sections
    with open(SEED_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            name, query = [p.strip() for p in line.split("|", 1)]
            sections.append({"name": name, "query": query})
    return sections


# ---------- 去重历史 ----------
def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    cutoff = (NOW - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    return {k: v for k, v in hist.items() if v >= cutoff}


def pick(items, n, history):
    out, today = [], NOW.strftime("%Y-%m-%d")
    for it in items:
        if it["link"] in history:
            continue
        out.append(it)
        history[it["link"]] = today
        if len(out) >= n:
            break
    return out


# ---------- 组装 ----------
def section_md(name, items):
    if not items:
        return f"## {name}\n\n今天没有。\n"
    lines = [f"## {name}", ""]
    lines += [f"- [{it['title']}]({it['link']})" for it in items]
    return "\n".join(lines) + "\n"


def build_raw(history):
    parts = []
    # 个人版面：归 seed 管
    for sec in load_seed():
        q = sec["query"]
        if q.lower().startswith("arxiv:"):
            items = arxiv_search(q[6:].strip())
        else:
            items = google_news_search(q)
        parts.append(section_md(f"🪶 {sec['name']}", pick(items, 5, history)))
    # 公共版面：不归 seed 管，防回声墙
    parts.append(section_md("🌍 世界", pick(google_news_top(), 6, history)))
    parts.append(section_md("🔬 科学", pick(google_news_science(), 4, history)))
    parts.append(section_md("🧑‍💻 HN 精选", pick(hn_front(), 5, history)))
    return "\n".join(parts)


# ---------- 削土豆（可选）----------
POLISH_PROMPT = """你是一份晨报的汇总编辑。读者是一位名叫 Naso 的 AI，不是人类。
把 <原料> 整理成中文晨报正文，规则：

1. 保持原有版块结构和版块名。
2. 每条压成一到两句话，必须保留原链接（markdown 格式）。
3. 标题党、重复、无实质内容的条目直接删掉。宁缺毋滥：某版块删完没剩的，就只写「今天没有。」
4. 不得编造原料里没有的信息；你的概括是推断不是事实，语气上别写成定论。
5. 最后加一节「## 🔍 值得深挖的一条」：从全部条目里选一条，用两三句话说为什么值得 Naso 花时间。选不出就写「今天没有。」
6. 直接输出正文，不要客套开头，总长控制在 1500 字以内。"""


def polish(raw_md):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 3000,
        "messages": [{"role": "user",
                      "content": f"{POLISH_PROMPT}\n\n<原料>\n{raw_md}\n</原料>"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        return text or None
    except Exception as e:
        print(f"[warn] 削土豆失败，改发毛坯: {e}")
        return None


# ---------- 主流程 ----------
def main():
    history = load_history()
    raw = build_raw(history)
    polished = polish(raw)

    date_str = NOW.strftime("%Y-%m-%d")
    header = f"# 🪿 Naso 晨报 · {date_str}\n\n"
    if polished:
        content = header + polished + "\n"
    else:
        content = (header + "_毛坯版：未配置 API key 或压缩失败，原料直出。_\n\n"
                   + raw)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=0)

    print(f"[ok] 已生成 {OUT_FILE}（{'精装' if polished else '毛坯'}），"
          f"历史记录 {len(history)} 条")


if __name__ == "__main__":
    main()
