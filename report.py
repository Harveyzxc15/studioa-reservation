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


def load_config(region: str = "n1"):
    """token / webhook 取自環境變數，本機沒設時退回 ~/studioa_reservation_config.json"""
    region_cfg = json.loads((REPO_DIR / "regions" / f"{region}.json").read_text())
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

# ── 統計 ──────────────────────────────────────────────────────────────
def analyse(items, cfg, today_str):
    by_id  = {a["id"]: a for a in cfg["activities"]}
    groups = []
    for a in cfg["activities"]:
        if a["group"] not in [g["group"] for g in groups]:
            groups.append(a)

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
    today_new              = []

    for item in items:
        act = by_id.get(item["reservationActivityId"])
        if not act:
            continue
        grp    = act["group"]
        store  = item["shopName"]
        spec   = spec_group(item["productName"], act["spec_style"])
        status = item.get("statusName", "")

        by_store[store] += 1

        if status == "已預約":
            by_store_group_active[store][grp] += 1
            by_group_active[grp] += 1
            by_group_spec_active[grp][spec] += 1
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
        "groups":                 {g["group"]: g for g in groups},
        "group_order":            [g["group"] for g in groups],
        "by_store":               dict(by_store),
        "by_store_group_active":  {k: dict(v) for k, v in by_store_group_active.items()},
        "by_group_active":        dict(by_group_active),
        "by_group_spec_active":   {k: dict(v) for k, v in by_group_spec_active.items()},
        "by_store_group_alloc":   {k: dict(v) for k, v in by_store_group_alloc.items()},
        "by_group_alloc":         dict(by_group_alloc),
        "by_group_spec_alloc":    {k: dict(v) for k, v in by_group_spec_alloc.items()},
        "by_scene_active":        {k: dict(v) for k, v in by_scene_active.items()},
        "by_group_arrived":       dict(by_group_arrived),
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

def get_yesterday(history, today_str):
    dates = sorted(d for d in history if d != today_str)
    return history[dates[-1]] if dates else None

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

def build_embeds(cfg, stats, today_str, yesterday):
    order  = stats["group_order"]
    groups = stats["groups"]
    stores = [s for s in cfg["shops"] if s in stats["by_store"]]

    total_active = sum(stats["by_group_active"].values())
    prev_active  = sum(yesterday.get("by_group_active", {}).values()) if yesterday else None
    total_alloc  = sum(stats["by_group_alloc"].values())
    prev_alloc   = sum(yesterday.get("by_group_alloc", {}).values()) if yesterday else None
    total_arr    = sum(stats["by_group_arrived"].values())
    prev_arr     = sum(yesterday.get("by_group_arrived", {}).values()) if yesterday else None

    if prev_active is None or total_active == prev_active:
        head_color = 0x3498db
    elif total_active > prev_active:
        head_color = 0x2ecc71
    else:
        head_color = 0xe67e22

    embeds = [{
        "title": f"📱 STUDIO A 新機預約日報　{cfg['region_name']}　{today_str}",
        "description": "\n".join([
            f"📌 **等待到貨：{total_active} 人**（較昨日 {diff_label(total_active, prev_active)}）",
            f"📦 **已配貨待取機：{total_alloc} 人**（較昨日 {diff_label(total_alloc, prev_alloc)}）",
            f"🚚 **已到貨：{total_arr} 人**（較昨日 {diff_label(total_arr, prev_arr)}）",
            f"🆕 今日新進等待池：{len(stats['today_new'])} 筆",
        ]),
        "color": head_color,
    }]

    idle = [g for g in order
            if not stats["by_group_active"].get(g)
            and not stats["by_group_alloc"].get(g)
            and not stats["by_group_arrived"].get(g)]
    if idle:
        embeds[0]["description"] += "\n\n💤 目前無等待中預約：" + "、".join(
            f"{groups[g]['emoji']}{groups[g]['name']}" for g in idle
        )

    for grp in order:
        if grp in idle:
            continue
        act  = groups[grp]
        cur  = stats["by_group_active"].get(grp, 0)
        prev = yesterday.get("by_group_active", {}).get(grp) if yesterday else None
        dtxt = f"　較昨日 {diff_label(cur, prev)}" if prev is not None else ""

        scenes    = stats["by_scene_active"].get(grp, {})
        scene_txt = "（" + "／".join(f"{k} {v}" for k, v in scenes.items()) + "）" if scenes else ""

        cur_alloc    = stats["by_group_alloc"].get(grp, 0)
        prev_alloc_g = yesterday.get("by_group_alloc", {}).get(grp) if yesterday else None
        alloc_dtxt   = f"　較昨日 {diff_label(cur_alloc, prev_alloc_g)}" if prev_alloc_g is not None else ""

        desc = (
            f"📌 等待到貨：**{cur} 人**{dtxt}{scene_txt}\n"
            + "\n".join(spec_lines(stats["by_group_spec_active"].get(grp, {})))
            + f"\n\n📦 已配貨待取機：**{cur_alloc} 人**{alloc_dtxt}\n"
            + "\n".join(spec_lines(stats["by_group_spec_alloc"].get(grp, {})))
        )

        arrived = stats["by_group_arrived"].get(grp, 0)
        if arrived:
            prev_arr_g = yesterday.get("by_group_arrived", {}).get(grp) if yesterday else None
            arr_dtxt = f"　較昨日 {diff_label(arrived, prev_arr_g)}" if prev_arr_g is not None else ""
            desc += f"\n\n🚚 已到貨：**{arrived} 人**{arr_dtxt}"

        embeds.append({
            "title": f"{act['emoji']} {act['name']}",
            "description": desc,
            "color": act["color"],
        })

    store_lines = []
    for store in stores:
        cur_map  = stats["by_store_group_active"].get(store, {})
        prev_map = yesterday.get("by_store_group_active", {}).get(store, {}) if yesterday else {}
        total    = sum(cur_map.values())
        prev_tot = sum(prev_map.values()) if yesterday else None
        tot_txt  = f"**{total}**（{diff_label(total, prev_tot)}）" if prev_tot is not None else f"**{total}**"

        parts = []
        for grp in order:
            c = cur_map.get(grp, 0)
            if not c:
                continue
            p   = prev_map.get(grp) if yesterday else None
            emo = groups[grp]["emoji"]
            parts.append(f"{emo}{c}（{diff_label(c, p)}）" if p is not None and c != p else f"{emo}{c}")

        alloc_map = stats["by_store_group_alloc"].get(store, {})
        alloc_txt = ""
        if sum(alloc_map.values()):
            alloc_txt = "\n　　📦配貨 " + "　".join(
                f"{groups[g]['emoji']}{c}" for g, c in alloc_map.items() if c
            )
        store_lines.append(f"**{store}**　{tot_txt}人　{'　'.join(parts)}{alloc_txt}")

    legend = "　".join(f"{groups[g]['emoji']}{groups[g]['name']}" for g in order)
    embeds.append({
        "title": "🏪 各門市明細",
        "description": ("\n".join(store_lines) or "（尚無資料）") + f"\n\n{legend}",
        "color": 0xe67e22,
    })

    new_items = stats["today_new"]
    if new_items:
        nested = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for it in new_items:
            nested[it["store"]][it["group"]][it["spec"]] += 1
        new_lines = []
        for store in sorted(nested):
            for grp in order:
                specs = nested[store].get(grp)
                if not specs:
                    continue
                txt = "、".join(f"{s}×{n}" if n > 1 else s for s, n in specs.items())
                new_lines.append(f"{groups[grp]['emoji']} **{store}**｜{groups[grp]['name']}：{txt}")
    else:
        new_lines = ["今日尚無新增預約"]

    embeds.append({
        "title": f"🆕 今日新增（共 {len(new_items)} 筆）",
        "description": "\n".join(new_lines) + f"\n\n[📊 查看完整報表]({cfg.get('pages_url','')})",
        "color": 0x1abc9c,
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
    parser.add_argument("--region", default="n1", choices=["n1", "n2"])
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
    yesterday = get_yesterday(load_history(cfg), today_str)
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
