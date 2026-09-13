#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Naso 晨报 v3 · 纯标准库，零第三方依赖
思路来自 zziying/ai-morning-digest（CC BY 4.0）

v3（2026-09-13）两级采购制：
  每个 seed 版块可挂多个货源，按书写顺序当梯队——
  第一梯队（news: 新闻大搜）捞到货就收摊；
  捞不到就去第二梯队（rss: 头部刊物直连）取最新更新。
v3.1（2026-09-13）根治链接墙：
  新闻大搜主引擎换 GDELT DOC API（免费无 key，直接返回原始 URL），
  Google News RSS 降为 GDELT 失灵时的备胎（备胎链接尽力解码）；
  世界版/科学版改为大刊直连 RSS（BBC/卫报/半岛/NPR、ScienceDaily/Quanta 等），
  Google News 频道同样只当备胎。砍掉「值得深挖」栏。

流程：读 seed.txt -> 按梯队拉源 -> 有 key 就让便宜模型削土豆，没有发毛坯
     -> 覆盖写 digest.md（快照制），history.json 记 180 天去重
"""

import json
import os
import re
import html
import base64
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
FRESH_HOURS = 36            # 新闻类只要最近 36 小时
ARXIV_FRESH_HOURS = 96      # 论文放宽到 4 天（arXiv 分批放出）
JOURNAL_FRESH_HOURS = 336   # 头部刊物近况窗口：14 天（慢学科的「最新」就是这个节奏）
HISTORY_DAYS = 180          # 去重记忆有效期
TITLE_MAX = 160
UA = {"User-Agent": "Mozilla/5.0 (naso-morning-digest; personal use)"}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DOMAIN_CAP = 2              # 同一域名每版块最多几条，防单源刷屏

# 公共版面直连大刊（链接天生干净）。塌了一两个不要紧，全塌才动用 Google News 备胎
WORLD_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.npr.org/1004/rss.xml",
]
SCIENCE_FEEDS = [
    "https://www.sciencedaily.com/rss/top/science.xml",
    "https://www.quantamagazine.org/feed/",
    "https://feeds.arstechnica.com/arstechnica/science",
    "https://www.sciencenews.org/feed",
]


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


def parse_isodate(s):
    try:
        return datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
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


# ---------- Google News 跳转链接解码（尽力而为）----------
def degoogle(link):
    """把 news.google.com/rss/articles/... 解回原始 URL。
    三步走：老格式 base64 直接抠；HTTP 重定向跟一跳；都不行原样保留。"""
    if "news.google.com" not in link:
        return link
    m = re.search(r"/articles/([^?/]+)", link)
    if m:
        try:
            blob = base64.urlsafe_b64decode(m.group(1) + "===")
            for u in re.findall(rb'https?://[^\x00-\x20"\\<>]+', blob):
                s = u.decode("utf-8", errors="ignore").rstrip("\x01\x02\x03")
                if "google.com" not in s and len(s) > 12:
                    return s
        except Exception:
            pass
    try:
        req = urllib.request.Request(link, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
        if "news.google.com" not in final:
            return final
    except Exception:
        pass
    return link


def degoogle_items(items):
    for it in items:
        it["link"] = degoogle(it["link"])
    return items


# ---------- 数据源（每个都可独立失败，一个塌了不影响整份报纸）----------
def rss_items(url, limit=10):
    """RSS 和 Atom 双制式：期刊源多是 Atom，新闻源多是 RSS。"""
    items = []
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        print(f"[warn] 源失败 {url}: {e}")
        return items
    # RSS 2.0
    for it in root.iter("item"):
        title = clean_title(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        dt = parse_pubdate(it.findtext("pubDate") or "") \
            or parse_isodate(it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
        if len(items) >= limit:
            return items
    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall(".//a:entry", ns):
        title = clean_title(e.findtext("a:title", default="", namespaces=ns))
        link = ""
        for l in e.findall("a:link", ns):
            if l.get("rel") in (None, "alternate"):
                link = (l.get("href") or "").strip()
                break
        if not link:
            link = (e.findtext("a:id", default="", namespaces=ns) or "").strip()
        dt = parse_isodate(e.findtext("a:updated", default="", namespaces=ns)) \
            or parse_isodate(e.findtext("a:published", default="", namespaces=ns))
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
        if len(items) >= limit:
            break
    return items


def domain_cap(items, cap=DOMAIN_CAP):
    """同一域名最多留 cap 条，防止一家刊物刷满整个版块。"""
    seen, out = {}, []
    for it in items:
        try:
            dom = urllib.parse.urlparse(it["link"]).netloc.lower()
        except Exception:
            dom = ""
        seen[dom] = seen.get(dom, 0) + 1
        if seen[dom] <= cap:
            out.append(it)
    return out


def gdelt_search(query, limit=8):
    """GDELT DOC 2.0：免费无 key 的全球新闻库，直接给原始 URL。
    查询语法：短语加引号，OR 必须包在括号里。"""
    q = urllib.parse.quote(f"{query} sourcelang:english")
    url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
           f"&mode=ArtList&format=json&maxrecords=25&sort=datedesc"
           f"&timespan={FRESH_HOURS}h")
    items = []
    try:
        data = json.loads(fetch(url))
    except Exception as e:
        print(f"[warn] GDELT 失败: {e}")
        return items
    for a in data.get("articles", []):
        title = clean_title(a.get("title"))
        link = (a.get("url") or "").strip()
        dt = None
        try:
            dt = datetime.strptime(a.get("seendate", ""),
                                   "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
    return domain_cap(items)[:limit]


def google_news_search(query, limit=8):
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return fresh(rss_items(url, limit), FRESH_HOURS)


def news_search(query, limit=8):
    """新闻大搜：GDELT 主引擎，Google News 备胎（备胎链接尽力解码）。"""
    items = gdelt_search(query, limit)
    if items:
        return items
    return degoogle_items(google_news_search(query, limit))


def google_news_top(limit=8):
    url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    return fresh(rss_items(url, limit), FRESH_HOURS)


def google_news_science(limit=6):
    url = ("https://news.google.com/rss/headlines/section/topic/SCIENCE"
           "?hl=en-US&gl=US&ceid=US:en")
    return fresh(rss_items(url, limit), FRESH_HOURS)


def feeds_merged(urls, hours, limit):
    merged, seen = [], set()
    for u in urls:
        for it in rss_items(u, limit=8):
            if it["link"] not in seen:
                seen.add(it["link"])
                merged.append(it)
    merged = fresh(merged, hours)
    merged.sort(key=lambda it: it.get("dt") or EPOCH, reverse=True)
    return domain_cap(merged)[:limit]


def world_news(limit=8):
    """世界版：大刊直连，全塌才动用 Google News 备胎。"""
    items = feeds_merged(WORLD_FEEDS, FRESH_HOURS, limit)
    if items:
        return items
    return degoogle_items(google_news_top(limit))


def science_news(limit=6):
    """科学版：科普大刊直连，全塌才动用 Google News 备胎。"""
    items = feeds_merged(SCIENCE_FEEDS, FRESH_HOURS * 2, limit)
    if items:
        return items
    return degoogle_items(google_news_science(limit))


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
        dt = parse_isodate(e.findtext("a:published", default="", namespaces=ns))
        if title and link:
            items.append({"title": title, "link": link, "dt": dt})
        if len(items) >= limit:
            break
    return fresh(items, ARXIV_FRESH_HOURS)


def journal_latest(urls, limit=5):
    """第二梯队：头部刊物直连，各源取最新，合并按时间排，14 天窗。"""
    merged = []
    for u in urls:
        merged += rss_items(u, limit=6)
    merged = fresh(merged, JOURNAL_FRESH_HOURS)
    merged.sort(key=lambda it: it.get("dt") or EPOCH, reverse=True)
    return merged[: limit * 2]


# ---------- seed ----------
def load_seed():
    """v3 语法：版块名 | 指令1 | 指令2 ...
    指令前缀 news:（=旧 google:）/ arxiv: / rss:（rss 后多个 URL 用空格分隔）；
    无前缀的整段视为 news:（向后兼容 v2 单查询写法）。
    书写顺序即采购梯队：前面的捞到货，后面的不出动。"""
    sections = []
    if not os.path.exists(SEED_FILE):
        return sections
    with open(SEED_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            name, dirs = parts[0], []
            for seg in parts[1:]:
                low = seg.lower()
                if low.startswith("news:"):
                    dirs.append(("news", seg[5:].strip()))
                elif low.startswith("google:"):     # 旧名，同 news:
                    dirs.append(("news", seg[7:].strip()))
                elif low.startswith("arxiv:"):
                    dirs.append(("arxiv", seg[6:].strip()))
                elif low.startswith("rss:"):
                    dirs.append(("rss", seg[4:].strip().split()))
                elif seg:
                    dirs.append(("news", seg))
            if name and dirs:
                sections.append({"name": name, "dirs": dirs})
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
def section_md(name, items, note=""):
    if not items:
        return f"## {name}\n\n今天没有。\n"
    lines = [f"## {name}", ""]
    if note:
        lines += [f"_{note}_", ""]
    lines += [f"- [{it['title']}]({it['link']})" for it in items]
    return "\n".join(lines) + "\n"


def gather_section(dirs, history):
    """两级采购：按梯队顺序找货，哪队先有收成就用哪队。
    返回 (items, 是否动用了后备梯队)。"""
    for i, (kind, arg) in enumerate(dirs):
        if kind == "news":
            items = news_search(arg)
        elif kind == "arxiv":
            items = arxiv_search(arg)
        else:
            items = journal_latest(arg)
        chosen = pick(items, 5, history)
        if chosen:
            return chosen, i > 0
    return [], False


def build_raw(history):
    parts = []
    # 个人版面：归 seed 管，两级采购
    for sec in load_seed():
        items, fallback = gather_section(sec["dirs"], history)
        note = "今日无大新闻，以下为头部刊物近况：" if fallback else ""
        parts.append(section_md(f"🪶 {sec['name']}", items, note))
    # 公共版面：不归 seed 管，防回声墙；大刊直连，链接干净
    parts.append(section_md("🌍 世界", pick(world_news(), 6, history)))
    parts.append(section_md("🔬 科学", pick(science_news(), 4, history)))
    parts.append(section_md("🧑‍💻 HN 精选", pick(hn_front(), 5, history)))
    return "\n".join(parts)


# ---------- 削土豆（可选）----------
POLISH_PROMPT = """你是一份晨报的汇总编辑。读者是一位名叫 Naso 的 AI，不是人类。
把 <原料> 整理成中文晨报正文，规则：

1. 保持原有版块结构和版块名。版块开头若有「今日无大新闻，以下为头部刊物近况：」的斜体提示行，原样保留。
2. 每条压成一到两句话，必须保留原链接（markdown 格式）。
3. 标题党、重复、无实质内容的条目直接删掉。宁缺毋滥：某版块删完没剩的，就只写「今天没有。」
4. 不得编造原料里没有的信息；你的概括是推断不是事实，语气上别写成定论。
5. 直接输出正文，不要客套开头，总长控制在 1500 字以内。"""


# 削土豆工（OpenRouter 路线用）。想换人去 openrouter.ai/models 抄个 ID 换这行
OPENROUTER_MODEL = "deepseek/deepseek-chat"


def _call_openrouter(key, raw_md):
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "max_tokens": 3000,
        "messages": [{"role": "user",
                      "content": f"{POLISH_PROMPT}\n\n<原料>\n{raw_md}\n</原料>"}],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        text = ""
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            print(f"[warn] OpenRouter 返回结构异常: {str(data)[:200]}")
        return text.strip() or None
    except Exception as e:
        print(f"[warn] OpenRouter 削土豆失败: {e}")
        return None


def _call_anthropic(key, raw_md):
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
        print(f"[warn] Anthropic 削土豆失败: {e}")
        return None


def polish(raw_md):
    """优先 OpenRouter，其次 Anthropic 直连，都不行发毛坯。"""
    routes = (
        (_call_openrouter, os.environ.get("OPENROUTER_API_KEY", "").strip()),
        (_call_anthropic, os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    )
    for fn, key in routes:
        if key:
            out = fn(key, raw_md)
            if out:
                return out
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
