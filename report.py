#!/usr/bin/env python3
"""
Studio A 新機預約自動通知（北一區 / 北二區）

活動清單集中在 activities.json，門市與 shopId 在 regions/<region>.json。
Discord 日報 + GitHub Pages HTML 報表。

用法：
    python3 report.py --region n1
    python3 report.py --region n2 --dry-run
"""

import argparse
import json
import os
import re
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


warnings.filterwarnings("ignore")
import requests

CONFIG_PATH = Path.home() / "studioa_reservation_config.json"
REPO_DIR    = Path(__file__).parent

# Discord 限制：單一 embed description 4096 字、單次 POST 合計 6000 字、最多 10 則
MAX_DESC_CHARS  = 3800
MAX_BATCH_CHARS = 5500
MAX_BATCH_EMBED = 8
# 每個活動最多列出幾組規格，其餘合併成一行
TOP_SPEC_GROUPS = 8


def load_region(region):
    """讀區域設定；有 sub_regions 的（如 all）會把子區的門市與歷史檔合併進來"""
    cfg = json.loads((REPO_DIR / "regions" / f"{region}.json").read_text())
    if cfg.get("sub_regions"):
        subs = [json.loads((REPO_DIR / "regions" / f"{r}.json").read_text()) for r in cfg["sub_regions"]]
        cfg["shops"]    = [s for sub in subs for s in sub["shops"]]
        cfg["shop_ids"] = {k: v for sub in subs for k, v in sub["shop_ids"].items()}
        cfg["regions"]  = [{"name": sub["name"], "shops": sub["shops"],
                            "history_file": str(REPO_DIR / sub["history_file"])} for sub in subs]
    else:
        cfg["regions"] = [{"name": cfg["name"], "shops": cfg["shops"],
                           "history_file": str(REPO_DIR / cfg["history_file"])}]
    return cfg


def load_config(region: str = "n1"):
    """token / webhook 取自環境變數，本機沒設時退回 ~/studioa_reservation_config.json"""
    region_cfg = load_region(region)
    activities = json.loads((REPO_DIR / "activities.json").read_text())["activities"]

    local = {}
    if CONFIG_PATH.exists():
        local = json.loads(CONFIG_PATH.read_text())

    token = os.environ.get("STUDIOA_TOKEN") or local.get("token")
    if not token:
        sys.exit("❌ 找不到 token（環境變數 STUDIOA_TOKEN 或本機 config）")

    webhook = os.environ.get(region_cfg["webhook_env"]) or local.get("discord_webhook")
    if not webhook:
        sys.exit(f"❌ 找不到 webhook（環境變數 {region_cfg['webhook_env']}）")

    return {
        "token":           token,
        "base_url":        "https://www.studioa.com.tw/backend/api/shopcms",
        "activities":      activities,
        "shops":           region_cfg["shops"],
        "shop_ids":        region_cfg["shop_ids"],
        "history_file":    str(REPO_DIR / region_cfg["history_file"]),
        "html_output":     str(REPO_DIR / region_cfg["html_output"]),
        "pages_url":       region_cfg["pages_url"],
        "region_name":     region_cfg["name"],
        "regions":         region_cfg["regions"],
        "discord_webhook": webhook,
    }

# ── API ───────────────────────────────────────────────────────────────
def fetch_activity(cfg, activity_id):
    """抓單一活動（門市在伺服器端就篩好，大型活動可省下十倍時間）"""
    headers = {"Authorization": cfg["token"]}
    shop_q  = "&".join(f"ShopIds={sid}" for sid in cfg["shop_ids"].values())
    skip, page_size = 0, 500
    items = []

    while True:
        url = (
            f"{cfg['base_url']}/reservation-activity/reservation-user-list"
            f"?SkipCount={skip}&MaxResultCount={page_size}"
            f"&ReservationActivityIds={activity_id}&{shop_q}"
        )
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        dto = resp.json()["data"]["userReservationListOutDtos"]
        for it in dto["items"]:
            if it.get("shopName"):
                it["shopName"] = it["shopName"].strip()   # 新莊宏匯有前置空格
        items.extend(dto["items"])
        skip += page_size
        if skip >= dto["totalCount"] or not dto["items"]:
            break

    return items

def fetch_all_reservations(cfg):
    acts = cfg["activities"]
    with ThreadPoolExecutor(max_workers=len(acts)) as pool:
        results = pool.map(lambda a: fetch_activity(cfg, a["id"]), acts)
    out = []
    for r in results:
        out.extend(r)
    return out

# ── 規格分組 ──────────────────────────────────────────────────────────
def spec_group(product, style):
    """把完整品名收斂成分組標題，顏色/錶帶一律不進標題"""
    name = product.replace("預約｜", "").strip()

    if style == "watch":
        head = name.split("/")[0]
        m = re.match(r"(Apple Watch\s+.+?)\s*\(([^)]*)\)", head)
        if not m:
            return head.replace("Apple Watch ", "")
        series = m.group(1).replace("Apple Watch ", "").strip()
        # 尺寸不一定在第一個逗號區段（SE 是 '2026, 40mm,GPS, 鋁金屬錶殼'）
        mm   = re.search(r"(\d+\s*mm)", m.group(2))
        size = mm.group(1).replace(" ", "") if mm else m.group(2).split(",")[0].strip()
        return f"{series} {size}"

    if style == "iphone":
        model = re.match(r"(iPhone[^\(]*)", name)
        model = model.group(1).strip() if model else name
        cap   = re.search(r"(\d+\s*(?:GB|TB))", name)
        return f"{model} {cap.group(1)}" if cap else model

    if style == "mac":
        parts = re.findall(r"\(([^)]*)\)", name)
        head  = re.match(r"([^\(]*)", name).group(1).strip()
        if len(parts) >= 2:
            return f"{head} {parts[0].split('，')[0].strip()} {parts[1].strip()}"
        return head

    # plain：'AirPods 5/AirPods 5 搭配無線充電盒' → '搭配無線充電盒'
    if "/" in name:
        head, tail = name.split("/", 1)
        tail = tail.strip()
        if tail.startswith(head.strip()):
            tail = tail[len(head.strip()):].strip() or head.strip()
        return tail
    return name

def model_of(product):
    """'iPhone 18 Pro Max (6.9吋/256GB)/冰川藍' → 'iPhone 18 Pro Max'"""
    name = product.replace("預約｜", "").strip()
    m = re.match(r"(iPhone[^\(]*)", name)
    return m.group(1).strip() if m else name


def resolve_group(act, product):
    """回傳 (分組鍵, 顯示名, emoji, 顏色)；設了 split_by_model 的活動會依機型再拆"""
    if act.get("split_by_model"):
        model = model_of(product)
        style = act.get("split_styles", {}).get(model)
        if style:
            return f"{act['group']}|{model}", model, style["emoji"], style["color"]
        return f"{act['group']}|{model}", model, act["emoji"], act["color"]
    return act["group"], act["name"], act["emoji"], act["color"]


def spec_color(product, style):
    """取出顏色：iPhone 取斜線後的機身色，Watch 取錶殼色，Mac 取機身色"""
    name = product.replace("預約｜", "").strip()

    if style == "watch":
        tail = name[name.rfind(")") + 1:].strip().lstrip("/").strip()
        m = re.match(r"(.*?錶殼)", tail)
        return m.group(1).strip() if m else (tail.split("/")[0].strip() or "未標示")

    if style in ("iphone", "mac"):
        tail = name[name.rfind(")") + 1:].strip().lstrip("/").strip()
        if not tail and "/" in name:
            tail = name.rsplit("/", 1)[1].strip()
        tail = tail.replace("四色/", "").replace("雙色/", "")
        return tail or "未標示"

    return "未標示"

# ── 統計 ──────────────────────────────────────────────────────────────
def analyse(items, cfg, today_str):
    by_id = {a["id"]: a for a in cfg["activities"]}
    # 分組資訊在掃描資料時建立（split_by_model 的活動要看到實際機型才知道有哪幾組）
    group_meta  = {}
    group_order = []
    for a in cfg["activities"]:
        if a.get("split_by_model"):
            for model, st in a.get("split_styles", {}).items():
                key = f"{a['group']}|{model}"
                if key not in group_meta:
                    group_meta[key] = {"name": model, "emoji": st["emoji"], "color": st["color"]}
                    group_order.append(key)
        elif a["group"] not in group_meta:
            group_meta[a["group"]] = {"name": a["name"], "emoji": a["emoji"], "color": a["color"]}
            group_order.append(a["group"])

    by_store               = defaultdict(int)
    by_store_group_active  = defaultdict(lambda: defaultdict(int))
    by_group_active        = defaultdict(int)
    by_group_spec_active   = defaultdict(lambda: defaultdict(int))
    by_store_group_alloc   = defaultdict(lambda: defaultdict(int))
    by_group_alloc         = defaultdict(int)
    by_group_spec_alloc    = defaultdict(lambda: defaultdict(int))
    by_scene_active        = defaultdict(lambda: defaultdict(int))
    by_group_arrived       = defaultdict(int)
    by_store_group_arrived = defaultdict(lambda: defaultdict(int))
    by_group_color_active  = defaultdict(lambda: defaultdict(int))
    by_store_spec_active   = defaultdict(lambda: defaultdict(int))
    by_store_color_active  = defaultdict(lambda: defaultdict(int))
    today_new              = []

    for item in items:
        act = by_id.get(item["reservationActivityId"])
        if not act:
            continue
        grp, gname, gemoji, gcolor = resolve_group(act, item["productName"])
        if grp not in group_meta:
            group_meta[grp] = {"name": gname, "emoji": gemoji, "color": gcolor}
            group_order.append(grp)
        store  = item["shopName"]
        spec   = spec_group(item["productName"], act["spec_style"])
        if act.get("split_by_model"):
            # 區塊標題已經是機型，規格只留容量
            spec = spec.replace(gname, "").strip() or spec
        color  = spec_color(item["productName"], act["spec_style"])
        status = item.get("statusName", "")

        by_store[store] += 1

        if status == "已預約":
            by_store_group_active[store][grp] += 1
            by_group_active[grp] += 1
            by_group_spec_active[grp][spec] += 1
            by_group_color_active[grp][color] += 1
            if act.get("split_by_model"):
                by_store_spec_active[store][spec] += 1
                by_store_color_active[store][color] += 1
            if act.get("scene"):
                by_scene_active[grp][act["scene"]] += 1
            if item.get("reservationTimeValue", "") == today_str:
                today_new.append({"store": store, "group": grp, "spec": spec})

        elif status == "已配貨":
            by_store_group_alloc[store][grp] += 1
            by_group_alloc[grp] += 1
            by_group_spec_alloc[grp][spec] += 1

        elif status == "已到貨":
            by_group_arrived[grp] += 1
            by_store_group_arrived[store][grp] += 1

    return {
        "groups":                 group_meta,
        "group_order":            group_order,
        "by_store":               dict(by_store),
        "by_store_group_active":  {k: dict(v) for k, v in by_store_group_active.items()},
        "by_group_active":        dict(by_group_active),
        "by_group_spec_active":   {k: dict(v) for k, v in by_group_spec_active.items()},
        "by_store_group_alloc":   {k: dict(v) for k, v in by_store_group_alloc.items()},
        "by_group_alloc":         dict(by_group_alloc),
        "by_group_spec_alloc":    {k: dict(v) for k, v in by_group_spec_alloc.items()},
        "by_scene_active":        {k: dict(v) for k, v in by_scene_active.items()},
        "by_group_arrived":       dict(by_group_arrived),
        "by_group_color_active":  {k: dict(v) for k, v in by_group_color_active.items()},
        "by_store_spec_active":   {k: dict(v) for k, v in by_store_spec_active.items()},
        "by_store_color_active":  {k: dict(v) for k, v in by_store_color_active.items()},
        "by_store_group_arrived": {k: dict(v) for k, v in by_store_group_arrived.items()},
        "today_new":              today_new,
    }

# ── 歷史 ──────────────────────────────────────────────────────────────
def load_history(cfg):
    p = Path(cfg["history_file"])
    return json.loads(p.read_text()) if p.exists() else {}

def save_history(cfg, today_str, stats):
    history = load_history(cfg)
    history[today_str] = {
        "by_group_active":       dict(stats["by_group_active"]),
        "by_store_group_active": {s: dict(m) for s, m in stats["by_store_group_active"].items()},
        "by_group_alloc":        dict(stats["by_group_alloc"]),
        "by_group_arrived":      dict(stats["by_group_arrived"]),
        "by_store_group_alloc":  {s: dict(m) for s, m in stats["by_store_group_alloc"].items()},
    }
    Path(cfg["history_file"]).write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history

def get_yesterday(cfg, history, today_str):
    dates = sorted(d for d in history if d != today_str)
    if dates:
        return history[dates[-1]]
    if len(cfg["regions"]) < 2:
        return None

    # 合併模式第一次跑：把各子區最近一天（且為新格式）的紀錄加總
    merged = {"by_group_active": defaultdict(int), "by_store_group_active": {},
              "by_group_alloc": defaultdict(int), "by_group_arrived": defaultdict(int)}
    for r in cfg["regions"]:
        p = Path(r["history_file"])
        if not p.exists():
            return None
        sub = json.loads(p.read_text())
        sub_dates = sorted(d for d in sub if d != today_str and "by_group_active" in sub[d])
        if not sub_dates:
            return None
        day = sub[sub_dates[-1]]
        for k in ("by_group_active", "by_group_alloc", "by_group_arrived"):
            for g, n in day.get(k, {}).items():
                merged[k][g] += n
        merged["by_store_group_active"].update(day.get("by_store_group_active", {}))
    return {k: dict(v) for k, v in merged.items()}

# ── Discord ───────────────────────────────────────────────────────────
def diff_label(cur, prev):
    if prev is None:
        return "首次執行"
    d = cur - prev
    return f"{'+' if d >= 0 else ''}{d}"

def spec_lines(spec_counts):
    if not spec_counts:
        return ["　（尚無資料）"]
    ordered = sorted(spec_counts.items(), key=lambda x: -x[1])
    head, rest = ordered[:TOP_SPEC_GROUPS], ordered[TOP_SPEC_GROUPS:]
    lines = [f"　{s}：**{c} 人**" for s, c in head]
    if rest:
        lines.append(f"　⋯其他 {len(rest)} 種規格：**{sum(c for _, c in rest)} 人**")
    return lines

def _w(text):
    """顯示寬度：中文與全形字算 2"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _pad(text, width, right=False):
    pad = " " * max(0, width - _w(text))
    return pad + str(text) if right else str(text) + pad


def render_table(headers, rows):
    """組成等寬表格字串，欄寬依內容自動調整"""
    cols = len(headers)
    widths = [max(_w(headers[i]), *(_w(r[i]) for r in rows)) for i in range(cols)] if rows \
             else [_w(h) for h in headers]
    out = [" ".join(_pad(headers[i], widths[i], right=(i > 0)) for i in range(cols))]
    for r in rows:
        out.append(" ".join(_pad(r[i], widths[i], right=(i > 0)) for i in range(cols)))
    return "```\n" + "\n".join(out) + "\n```"


COLOR_ABBR = {"冰川藍": "冰藍", "勃根地紅": "勃紅", "銀色": "銀", "黑色": "黑",
              "白色": "白", "金色": "金", "原色": "原"}


def color_abbr(name):
    """顏色縮成兩字以內，手機版才排得下"""
    if name in COLOR_ABBR:
        return COLOR_ABBR[name]
    t = name.replace("色", "")
    return t[:2] if t else name[:2]


def short_label(text):
    """把規格/顏色縮短成適合擠在一行的寫法"""
    t = (text
         .replace("Series ", "S").replace("Ultra ", "Ultra")
         .replace("GB", "G").replace("TB", "T")
         .replace("鋁金屬錶殼", "鋁").replace("鈦金屬錶殼", "鈦")
         .replace("不鏽鋼錶殼", "鋼").replace("錶殼", ""))
    return t.strip()


def inline_counts(counts, limit=6):
    """{'256GB': 129, ...} → '256G 129・512G 63'"""
    ordered = sorted(counts.items(), key=lambda x: -x[1])
    head, rest = ordered[:limit], ordered[limit:]
    parts = [f"{short_label(k)} {v}" for k, v in head]
    if rest:
        parts.append(f"其他 {sum(v for _, v in rest)}")
    return "・".join(parts)


def inline_pcts(counts, limit=5):
    """{'冰川藍': 175, ...} → '冰川藍 42%・勃根地紅 29%'"""
    total = sum(counts.values())
    if not total:
        return ""
    ordered = sorted(counts.items(), key=lambda x: -x[1])
    head, rest = ordered[:limit], ordered[limit:]
    parts = [f"{short_label(k)} {round(v / total * 100)}%" for k, v in head]
    if rest:
        parts.append(f"其他 {round(sum(v for _, v in rest) / total * 100)}%")
    return "・".join(parts)


def build_embeds(cfg, stats, today_str, yesterday):
    """三區塊：總覽 / 各活動 / 各門市。逐項明細一律留給 HTML 報表"""
    order  = stats["group_order"]
    groups = stats["groups"]

    total_active = sum(stats["by_group_active"].values())
    prev_active  = sum(yesterday.get("by_group_active", {}).values()) if yesterday else None
    total_alloc  = sum(stats["by_group_alloc"].values())
    total_arr    = sum(stats["by_group_arrived"].values())

    if prev_active is None or total_active == prev_active:
        head_color = 0x3498db
    elif total_active > prev_active:
        head_color = 0x2ecc71
    else:
        head_color = 0xe67e22

    diff_txt = f"（{diff_label(total_active, prev_active)}）" if prev_active is not None else ""
    head_lines = [f"**等待到貨 {total_active} 人**{diff_txt}・今日新增 {len(stats['today_new'])}"]

    multi = len(cfg["regions"]) > 1

    def region_active(r, day):
        """某區所有門市的等待到貨合計；day=None 代表沒有前一日資料"""
        if day is None:
            return None
        src = day.get("by_store_group_active", {})
        return sum(sum(src.get(s, {}).values()) for s in r["shops"])

    if multi:
        head_lines.append("・".join(
            f"{r['name']} **{region_active(r, stats)}**"
            + (f"（{diff_label(region_active(r, stats), region_active(r, yesterday))}）" if yesterday else "")
            for r in cfg["regions"]
        ))
    extra = []
    if total_alloc:
        extra.append(f"📦 已配貨待取機 {total_alloc}")
    if total_arr:
        extra.append(f"🚚 已到貨 {total_arr}")
    if extra:
        head_lines.append("・".join(extra))

    embeds = [{
        "title": f"📅 {cfg['region_name']}預約日報　{today_str}",
        "description": "\n".join(head_lines),
        "color": head_color,
    }]

    # 各活動：一則訊息，每個活動三行（標題／規格／顏色）
    blocks = []
    for grp in order:
        cur = stats["by_group_active"].get(grp, 0)
        if not cur and not stats["by_group_alloc"].get(grp) and not stats["by_group_arrived"].get(grp):
            continue   # 未開賣或已結束的活動不佔版面
        g    = groups[grp]
        prev = yesterday.get("by_group_active", {}).get(grp) if yesterday else None
        dtxt = f"（{diff_label(cur, prev)}）" if prev is not None else ""

        lines = [f"{g['emoji']} **{g['name']} {cur}**{dtxt}"]
        specs = inline_counts(stats["by_group_spec_active"].get(grp, {}))
        if specs:
            lines.append(f"　{specs}")
        colors = inline_pcts(stats["by_group_color_active"].get(grp, {}))
        if colors:
            lines.append(f"　{colors}")
        tail = []
        if stats["by_group_alloc"].get(grp):
            tail.append(f"📦 {stats['by_group_alloc'][grp]}")
        if stats["by_group_arrived"].get(grp):
            tail.append(f"🚚 {stats['by_group_arrived'][grp]}")
        if tail:
            lines.append("　" + "・".join(tail))
        blocks.append("\n".join(lines))

    if blocks:
        embeds.append({
            "title": "📊 各活動",
            "description": "\n\n".join(blocks),
            "color": 0x8e44ad,
        })

    # 手機活動（有 split_by_model 的）另外用表格呈現各門市狀況
    phone_groups = [g for g in order if "|" in g]

    def sorted_shops(shops):
        return sorted([s for s in shops if stats["by_store_group_active"].get(s)],
                      key=lambda s: -sum(stats["by_store_group_active"][s].values()))

    stores_sorted = sorted_shops(cfg["shops"])

    if phone_groups and stores_sorted:
        # 表一：各門市 × 機型（容量明細在「各活動」與 HTML，手機版排不下）
        headers = ["門市"] + [
            short_label(groups[g]["name"].replace("iPhone 18 ", "").replace("Pro Max", "Max"))
            for g in phone_groups
        ] + ["合計"]
        rows = []
        for r in cfg["regions"]:
            region_rows, sub = [], [0] * len(phone_groups)
            for st in sorted_shops(r["shops"]):
                per_model = [stats["by_store_group_active"].get(st, {}).get(g, 0) for g in phone_groups]
                if not sum(per_model):
                    continue
                sub = [a + b for a, b in zip(sub, per_model)]
                region_rows.append([st] + [str(v) for v in per_model] + [str(sum(per_model))])
            if not region_rows:
                continue
            if multi:
                rows.append([f"【{r['name']}】"] + [""] * (len(headers) - 1))
            rows.extend(region_rows)
            if multi:
                rows.append([f"{r['name']}小計"] + [str(v) for v in sub] + [str(sum(sub))])
        if rows:
            embeds.append({
                "title": "📱 各門市手機預約",
                "description": render_table(headers, rows),
                "color": 0xe74c3c,
            })

        # 表二：各門市顏色佔比
        color_tot = defaultdict(int)
        for g in phone_groups:
            for cname, n in stats["by_group_color_active"].get(g, {}).items():
                color_tot[cname] += n
        col_order = [c for c, _ in sorted(color_tot.items(), key=lambda x: -x[1])][:4]

        def pct_row(label, cmap):
            total = sum(cmap.values())
            return [label] + [f"{round(cmap.get(c, 0) / total * 100)}%" for c in col_order]

        headers2 = ["門市"] + [color_abbr(c) for c in col_order]
        rows2 = []
        for r in cfg["regions"]:
            region_rows, sub = [], defaultdict(int)
            for st in sorted_shops(r["shops"]):
                cmap = stats["by_store_color_active"].get(st, {})
                if not sum(cmap.values()):
                    continue
                for c, n in cmap.items():
                    sub[c] += n
                region_rows.append(pct_row(st, cmap))
            if not region_rows:
                continue
            if multi:
                rows2.append([f"【{r['name']}】"] + [""] * len(col_order))
            rows2.extend(region_rows)
            if multi:
                rows2.append(pct_row(f"{r['name']}小計", sub))
        if rows2:
            rows2.append(pct_row("合計" if multi else "全區", color_tot))
            embeds.append({
                "title": "🎨 各門市顏色佔比",
                "description": render_table(headers2, rows2),
                "color": 0x8e44ad,
            })

    # 各門市總計（含 Watch 等非手機活動），多區時一區一行
    def shop_line(shops):
        return "・".join(f"{st} {sum(stats['by_store_group_active'][st].values())}" for st in sorted_shops(shops))

    if multi:
        store_txt = "\n".join(f"**{r['name']}**　{shop_line(r['shops']) or '（尚無資料）'}"
                               for r in cfg["regions"])
    else:
        store_txt = shop_line(cfg["shops"]) or "（尚無資料）"

    embeds.append({
        "title": "🏪 各門市合計",
        "description": store_txt + f"\n\n[📊 完整明細（規格／顏色／今日新增）]({cfg.get('pages_url','')})",
        "color": 0xe67e22,
        "footer": {"text": f"自動報表 · {cfg['region_name']}"},
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    return embeds


def batch_embeds(embeds):
    """截斷過長內容，並依字數/則數上限拆成多次 POST"""
    safe = []
    for e in embeds:
        desc = e["description"]
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[:MAX_DESC_CHARS].rsplit("\n", 1)[0] + "\n　⋯（內容過長已省略）"
        safe.append({**e, "description": desc})

    batches, cur, cur_len = [], [], 0
    for e in safe:
        size = len(e["description"]) + len(e.get("title", ""))
        if cur and (len(cur) >= MAX_BATCH_EMBED or cur_len + size > MAX_BATCH_CHARS):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(e)
        cur_len += size
    if cur:
        batches.append(cur)
    return batches

def send_discord(cfg, embeds):
    for i, batch in enumerate(batch_embeds(embeds), 1):
        resp = requests.post(cfg["discord_webhook"], json={"embeds": batch}, timeout=15)
        resp.raise_for_status()
        print(f"  ✅ Discord 第 {i} 則已發送（{resp.status_code}，{len(batch)} 個區塊）")

def print_embeds(embeds):
    for i, batch in enumerate(batch_embeds(embeds), 1):
        print(f"\n──────── POST #{i}（{len(batch)} 區塊）────────")
        for e in batch:
            print(f"\n【{e['title']}】")
            print(e["description"])

# ── HTML 報表 ─────────────────────────────────────────────────────────
PALETTE = ["#e67e22", "#1abc9c", "#e74c3c", "#3498db", "#9b59b6", "#34495e"]

def generate_html(cfg, stats, today_str, history):
    order  = stats["group_order"]
    groups = stats["groups"]
    shops  = cfg["shops"]
    color_of = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(order)}

    total_active = sum(stats["by_group_active"].values())
    total_alloc  = sum(stats["by_group_alloc"].values())
    total_arr    = sum(stats["by_group_arrived"].values())
    new_count    = len(stats["today_new"])

    # 折線圖：只取有新版 group 統計的日期（舊資料是 MacBook 時期的 model 鍵，畫出來全是 0）
    dates = sorted(d for d in history if "by_group_active" in history[d])
    datasets = []
    for grp in order:
        datasets.append({
            "label": f"{groups[grp]['emoji']} {groups[grp]['name']}",
            "data": [history[d].get("by_group_active", {}).get(grp, 0) for d in dates],
            "borderColor": color_of[grp],
            "backgroundColor": color_of[grp] + "22",
            "tension": 0.4, "fill": True, "pointRadius": 4, "pointHoverRadius": 6,
        })

    # 總覽方塊
    ov = (
        f'<div class="ov-box green"><div class="ov-num">{total_active}</div><div class="ov-label">📌 等待到貨總人數</div></div>'
        f'<div class="ov-box orange"><div class="ov-num">{total_alloc}</div><div class="ov-label">📦 已配貨待取機</div></div>'
        f'<div class="ov-box teal"><div class="ov-num">{total_arr}</div><div class="ov-label">🚚 已到貨</div></div>'
        f'<div class="ov-box"><div class="ov-num">{new_count}</div><div class="ov-label">🆕 今日新增預約</div></div>'
    )

    # 各活動卡片
    cards = ""
    for grp in order:
        c      = color_of[grp]
        act    = groups[grp]
        a_tot  = stats["by_group_active"].get(grp, 0)
        al_tot = stats["by_group_alloc"].get(grp, 0)
        ar_tot = stats["by_group_arrived"].get(grp, 0)

        rows = ""
        for spec, cnt in sorted(stats["by_group_spec_active"].get(grp, {}).items(), key=lambda x: -x[1]):
            pct = round(cnt / a_tot * 100) if a_tot else 0
            rows += (f'<tr><td>{spec}</td><td class="num">{cnt}</td>'
                     f'<td><div class="bar-wrap"><div class="bar" style="width:{pct}%;background:{c}"></div></div></td></tr>')

        alloc_rows = "".join(
            f'<tr><td>{spec}</td><td class="num">{cnt}</td><td></td></tr>'
            for spec, cnt in sorted(stats["by_group_spec_alloc"].get(grp, {}).items(), key=lambda x: -x[1])
        )

        colors     = stats["by_group_color_active"].get(grp, {})
        color_tot  = sum(colors.values())
        color_rows = ""
        for name, cnt in sorted(colors.items(), key=lambda x: -x[1]):
            pct = round(cnt / color_tot * 100) if color_tot else 0
            color_rows += (f'<tr><td>{name}</td><td class="num">{cnt}</td><td class="num">{pct}%</td>'
                           f'<td><div class="bar-wrap"><div class="bar" style="width:{pct}%;background:{c}"></div></div></td></tr>')
        color_section = (
            f'<div class="section-label">🎨 顏色分佈</div>'
            f'<table class="spec-table"><tbody>{color_rows}</tbody></table>'
        ) if color_rows else ""

        cards += (
            f'<div class="card" style="border-top:4px solid {c}">'
            f'<div class="card-title">{act["emoji"]} {act["name"]}</div>'
            f'<div class="stat-row">'
            f'<div class="stat-box"><div class="stat-num">{a_tot}</div><div class="stat-label">📌 等待到貨</div></div>'
            f'<div class="stat-box"><div class="stat-num">{al_tot}</div><div class="stat-label">📦 已配貨</div></div>'
            f'<div class="stat-box"><div class="stat-num">{ar_tot}</div><div class="stat-label">🚚 已到貨</div></div>'
            f'</div>'
            f'<div class="section-label">等待到貨規格</div>'
            f'<table class="spec-table"><thead><tr><th>規格</th><th>人數</th><th></th></tr></thead>'
            f'<tbody>{rows or "<tr><td colspan=3 class=muted>無</td></tr>"}</tbody></table>'
            f'<div class="alloc-label">📦 已配貨待取機：{al_tot} 人</div>'
            f'<table class="spec-table"><tbody>{alloc_rows or "<tr><td colspan=3 class=muted>無</td></tr>"}</tbody></table>'
            f'{color_section}'
            f'</div>'
        )

    # 門市表：欄位隨活動數動態產生
    head_cells = "".join(f'<th>{groups[g]["emoji"]} {groups[g]["name"]}</th>' for g in order)
    store_rows = ""
    for store in shops:
        active = stats["by_store_group_active"].get(store, {})
        alloc  = stats["by_store_group_alloc"].get(store, {})
        total  = sum(active.values())
        cells  = "".join(f'<td><span class="pill">{active.get(g, 0)}</span></td>' for g in order)
        a_str  = " ".join(f'{groups[g]["emoji"]}{c}' for g, c in alloc.items() if c) or "—"
        store_rows += (f'<tr><td class="store-name">{store}</td><td class="num">{total}</td>'
                       f'{cells}<td class="alloc-cell">{a_str}</td></tr>')

    # 今日新增
    nested = defaultdict(lambda: defaultdict(int))
    for it in stats["today_new"]:
        nested[(it["store"], it["group"])][it["spec"]] += 1
    new_rows = ""
    for (store, grp), specs in nested.items():
        for spec, cnt in specs.items():
            cnt_str = f" ×{cnt}" if cnt > 1 else ""
            new_rows += (f'<tr><td><span class="badge" style="background:{color_of[grp]}">'
                         f'{groups[grp]["name"]}</span></td><td>{store}</td><td>{spec}{cnt_str}</td></tr>')
    new_section = (
        f"<table class='data-table'><thead><tr><th>活動</th><th>門市</th><th>規格</th></tr></thead><tbody>{new_rows}</tbody></table>"
        if new_rows else "<div class='empty'>今日尚無新增預約</div>"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>STUDIO A 預約日報 {today_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f9;color:#2c3e50;padding:24px}}
h1{{font-size:22px;font-weight:700;margin-bottom:4px}}
.subtitle{{color:#7f8c8d;font-size:13px;margin-bottom:24px}}
.overview{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
.ov-box{{background:#fff;border-radius:12px;padding:20px 28px;flex:1;min-width:130px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.ov-num{{font-size:36px;font-weight:800}}
.ov-label{{font-size:12px;color:#7f8c8d;margin-top:4px}}
.green .ov-num{{color:#27ae60}}.orange .ov-num{{color:#e67e22}}.teal .ov-num{{color:#1abc9c}}
.chart-card{{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}}
.chart-card h2{{font-size:15px;font-weight:700;margin-bottom:16px;color:#34495e}}
.section-title{{font-size:15px;font-weight:700;margin:24px 0 12px;color:#34495e}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}}
@media(max-width:700px){{.cards{{grid-template-columns:1fr}}}}
.card{{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.card-title{{font-size:16px;font-weight:700;margin-bottom:14px}}
.stat-row{{display:flex;gap:12px;margin-bottom:16px}}
.stat-box{{background:#f8f9fa;border-radius:8px;padding:10px 12px;flex:1;text-align:center}}
.stat-num{{font-size:24px;font-weight:800}}
.stat-label{{font-size:11px;color:#7f8c8d;margin-top:2px}}
.section-label{{font-size:11px;font-weight:600;color:#7f8c8d;margin:12px 0 6px;letter-spacing:.5px}}
.alloc-label{{font-size:12px;font-weight:600;color:#e67e22;margin:14px 0 6px}}
.spec-table{{width:100%;border-collapse:collapse;font-size:13px}}
.spec-table th{{text-align:left;padding:6px 8px;border-bottom:2px solid #eee;color:#7f8c8d;font-size:11px;font-weight:600}}
.spec-table td{{padding:7px 8px;border-bottom:1px solid #f0f0f0}}
.bar-wrap{{background:#f0f0f0;border-radius:4px;height:6px;width:100px}}
.bar{{height:6px;border-radius:4px}}
.table-wrap{{overflow-x:auto}}
.data-table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);font-size:14px}}
.data-table th{{padding:10px 14px;background:#f8f9fa;text-align:left;font-size:12px;color:#7f8c8d;font-weight:600;white-space:nowrap}}
.data-table td{{padding:12px 14px;border-bottom:1px solid #f0f0f0}}
.store-name{{font-weight:700}}.num{{text-align:right;font-weight:700}}
.pill{{background:#eef2f7;color:#334155;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}}
.alloc-cell{{font-size:13px;color:#e67e22;font-weight:600}}
.badge{{color:#fff;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}}
.muted{{color:#aaa}}.empty{{background:#fff;border-radius:12px;padding:20px;text-align:center;color:#aaa;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.footer{{margin-top:32px;text-align:center;font-size:12px;color:#bdc3c7}}
</style>
</head>
<body>
<h1>📱 STUDIO A 新機預約日報</h1>
<div class="subtitle">{today_str} · {cfg["region_name"]}{len(shops)} 門市</div>

<div class="overview">{ov}</div>

<div class="chart-card">
  <h2>📈 等待到貨人數走勢</h2>
  <canvas id="trendChart" height="90"></canvas>
</div>

<div class="section-title">各活動規格明細</div>
<div class="cards">{cards}</div>

<div class="section-title">各門市明細</div>
<div class="table-wrap"><table class="data-table">
  <thead><tr><th>門市</th><th style="text-align:right">等待</th>{head_cells}<th>已配貨</th></tr></thead>
  <tbody>{store_rows}</tbody>
</table></div>

<div class="section-title">今日新增（共 {new_count} 筆）</div>
{new_section}

<div class="footer">自動報表 · STUDIO A {cfg["region_name"]} · {today_str}</div>

<script>
new Chart(document.getElementById('trendChart').getContext('2d'), {{
  type: 'line',
  data: {{ labels: {json.dumps(dates, ensure_ascii=False)}, datasets: {json.dumps(datasets, ensure_ascii=False)} }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'top' }},
      tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y + ' 人' }} }} }},
    scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: v => v + ' 人' }} }} }}
  }}
}});
</script>
</body>
</html>"""

    out_path = Path(cfg["html_output"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"  ✅ HTML 報表已產生：{out_path}")
    return out_path

# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="n1", choices=["n1", "n2", "all"])
    parser.add_argument("--dry-run", action="store_true",
                        help="只印訊息，不推送、不寫歷史、不產生 HTML")
    args = parser.parse_args()

    cfg       = load_config(args.region)
    today_str = datetime.now().strftime("%Y/%m/%d")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] {cfg['region_name']} 開始抓取預約資料...")
    try:
        items = fetch_all_reservations(cfg)
    except Exception as e:
        print(f"❌ API 呼叫失敗：{e}")
        sys.exit(1)
    print(f"  ✅ 取得 {len(items)} 筆資料（我方門市）")

    stats     = analyse(items, cfg, today_str)
    yesterday = get_yesterday(cfg, load_history(cfg), today_str)
    embeds    = build_embeds(cfg, stats, today_str, yesterday)

    if args.dry_run:
        print_embeds(embeds)
        print("\n（--dry-run：未推送、未寫入歷史、未產生 HTML）")
        return

    history = save_history(cfg, today_str, stats)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 產生 HTML 報表...")
    try:
        generate_html(cfg, stats, today_str, history)
    except Exception as e:
        print(f"⚠️  HTML 產生失敗（不影響 Discord）：{e}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 發送 Discord 通知...")
    try:
        send_discord(cfg, embeds)
    except Exception as e:
        print(f"❌ Discord 發送失敗：{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
