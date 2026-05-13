#!/usr/bin/env python3
"""
Studio A 新機預約自動通知
每天早上9點自動執行，透過 Discord 傳送 MacBook Neo / MacBook Air M5 預約摘要
"""

import argparse
import json
import os
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path


warnings.filterwarnings("ignore")
import requests

CONFIG_PATH = Path.home() / "studioa_reservation_config.json"
REPO_DIR    = Path(__file__).parent

def load_config(region: str = "n1"):
    """載入設定：本機用 JSON 檔，GitHub Actions 用環境變數 + region 設定檔"""
    region_cfg = json.loads((REPO_DIR / "regions" / f"{region}.json").read_text())

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            base = json.load(f)
        # 本機模式：用 region 設定覆蓋 shops / history_file
        base["shops"]           = region_cfg["shops"]
        base["history_file"]    = str(REPO_DIR / region_cfg["history_file"])
        base["html_output"]     = str(REPO_DIR / region_cfg["html_output"])
        base["pages_url"]       = region_cfg["pages_url"]
        base["region_name"]     = region_cfg["name"]
        # 本機以 discord_webhook 欄位為主（不分區）
        return base
    else:
        # GitHub Actions 模式
        webhook_key = region_cfg["webhook_env"]
        return {
            "token":           os.environ["STUDIOA_TOKEN"],
            "base_url":        "https://www.studioa.com.tw/backend/api/shopcms",
            "activities": {
                "MacBook Neo":    "3a1ff33e-40e5-9fc9-349b-9ac47b354fb0",
                "MacBook Air M5": "3a1ff280-2379-5b06-7c11-319979aa2c59",
            },
            "shops":           region_cfg["shops"],
            "history_file":    str(REPO_DIR / region_cfg["history_file"]),
            "html_output":     str(REPO_DIR / region_cfg["html_output"]),
            "pages_url":       region_cfg["pages_url"],
            "region_name":     region_cfg["name"],
            "discord_webhook": os.environ[webhook_key],
        }

# ── API ───────────────────────────────────────────────────────────────
def fetch_all_reservations(cfg):
    headers = {"Authorization": cfg["token"]}
    activity_ids = list(cfg["activities"].values())
    base         = cfg["base_url"]
    my_shops     = set(cfg.get("shops", []))

    skip, page_size = 0, 500
    all_items = []

    while True:
        act_params = "&".join(f"ReservationActivityIds={aid}" for aid in activity_ids)
        url = (
            f"{base}/reservation-activity/reservation-user-list"
            f"?SkipCount={skip}&MaxResultCount={page_size}"
            f"&{act_params}"
        )
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()["data"]

        items = data["userReservationListOutDtos"]["items"]
        # 正規化門市名稱（去除前後空白）
        for it in items:
            if it.get("shopName"):
                it["shopName"] = it["shopName"].strip()
        # 只保留屬於我方門市的資料
        if my_shops:
            items = [it for it in items if it.get("shopName") in my_shops]
        all_items.extend(items)

        fetched_so_far = skip + page_size
        if fetched_so_far >= data["userReservationListOutDtos"]["totalCount"]:
            break
        skip += page_size

    return all_items

# ── 產品名稱精簡化 ────────────────────────────────────────────────────
def short_spec(product):
    """'MacBook Neo (13吋，A18 Pro) (8GB/512GB) / 四色/胭粉色' → '8GB/512GB｜胭粉色'"""
    first  = product.find("(")
    second = product.find("(", first + 1) if first != -1 else -1
    if second == -1:
        return product
    rest = product[second + 1:].replace("四色/", "").replace("雙色/", "")
    if ") / " in rest:
        spec, color = rest.split(") / ", 1)
        return f"{spec}｜{color.strip()}"
    return rest.rstrip(")")

# 型號縮寫（Discord 訊息用）
MODEL_SHORT = {
    "MacBook Neo":    "Neo",
    "MacBook Air M5": "Air M5",
}
# 今日新增顏色 emoji
MODEL_DOT = {
    "MacBook Neo":    "🟣",
    "MacBook Air M5": "🔵",
}

# ── 統計 ──────────────────────────────────────────────────────────────
def analyse(items, cfg, today_str):
    id_to_model = {v: k for k, v in cfg["activities"].items()}

    by_model        = defaultdict(list)
    by_store        = defaultdict(list)
    by_store_model  = defaultdict(lambda: defaultdict(list))
    by_model_prod   = defaultdict(lambda: defaultdict(int))
    # 各店狀態計數：by_store_status[store][status] = count
    by_store_status = defaultdict(lambda: defaultdict(int))
    today_new       = []

    # 已預約：各店 × 各型號
    by_store_model_active = defaultdict(lambda: defaultdict(int))
    # 各型號「已預約」總計
    by_model_active = defaultdict(int)
    # 各型號 × 各規格「已預約」數量
    by_model_prod_active = defaultdict(lambda: defaultdict(int))

    # 已配貨：各店 × 各型號
    by_store_model_allocated = defaultdict(lambda: defaultdict(int))
    # 各型號「已配貨」總計
    by_model_allocated = defaultdict(int)
    # 各型號 × 各規格「已配貨」數量
    by_model_prod_allocated = defaultdict(lambda: defaultdict(int))

    for item in items:
        model   = id_to_model.get(item["reservationActivityId"], "其他")
        store   = item["shopName"]
        product = item["productName"].replace("預約｜", "")
        date    = item.get("reservationTimeValue", "")
        status  = item.get("statusName", "")

        by_model[model].append(item)
        by_store[store].append(item)
        by_store_model[store][model].append(item)
        by_model_prod[model][product] += 1
        by_store_status[store][status] += 1

        if status == "已預約":
            by_store_model_active[store][model] += 1
            by_model_active[model] += 1
            by_model_prod_active[model][product] += 1
            if date == today_str:
                today_new.append({"store": store, "model": model, "product": product})

        elif status == "已配貨":
            by_store_model_allocated[store][model] += 1
            by_model_allocated[model] += 1
            by_model_prod_allocated[model][product] += 1

    return {
        "by_model":                  dict(by_model),
        "by_store":                  dict(by_store),
        "by_store_model":            {k: dict(v) for k, v in by_store_model.items()},
        "by_model_prod":             {k: dict(v) for k, v in by_model_prod.items()},
        "by_store_status":           {k: dict(v) for k, v in by_store_status.items()},
        "by_store_model_active":     {k: dict(v) for k, v in by_store_model_active.items()},
        "by_model_active":           dict(by_model_active),
        "by_model_prod_active":      {k: dict(v) for k, v in by_model_prod_active.items()},
        "by_store_model_allocated":  {k: dict(v) for k, v in by_store_model_allocated.items()},
        "by_model_allocated":        dict(by_model_allocated),
        "by_model_prod_allocated":   {k: dict(v) for k, v in by_model_prod_allocated.items()},
        "today_new":                 today_new,
    }

# ── 歷史 ──────────────────────────────────────────────────────────────
def load_history(cfg):
    p = Path(cfg["history_file"])
    return json.loads(p.read_text()) if p.exists() else {}

def save_history(cfg, today_str, stats):
    history = load_history(cfg)
    history[today_str] = {
        "by_model_active":           dict(stats["by_model_active"]),
        "by_store_model_active":     {s: dict(m) for s, m in stats["by_store_model_active"].items()},
        "by_model_allocated":        dict(stats["by_model_allocated"]),
        "by_store_model_allocated":  {s: dict(m) for s, m in stats["by_store_model_allocated"].items()},
    }
    Path(cfg["history_file"]).write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history

def get_yesterday(history, today_str):
    dates = sorted(d for d in history if d != today_str)
    return history[dates[-1]] if dates else None

# ── Discord ───────────────────────────────────────────────────────────
def send_discord(cfg, stats, today_str, yesterday):
    webhook = cfg["discord_webhook"]
    models  = list(cfg["activities"].keys())
    stores  = sorted(stats["by_store"].keys())

    # ── Embed 1：整體總覽 ─────────────────────────────────────────────
    total_active     = sum(stats["by_model_active"].values())
    prev_active      = sum(yesterday.get("by_model_active", {}).values()) if yesterday else None
    total_allocated  = sum(stats["by_model_allocated"].values())
    prev_allocated   = sum(yesterday.get("by_model_allocated", {}).values()) if yesterday else None
    today_new_count  = len(stats["today_new"])

    def diff_label(cur, prev):
        if prev is None: return "首次執行"
        d = cur - prev
        return f"{'+'if d>=0 else ''}{d}"

    # 等待池增加 → 綠，減少 → 橘，持平 → 藍
    if prev_active is None:
        diff_color = 0x3498db
    elif total_active > prev_active:
        diff_color = 0x2ecc71
    elif total_active < prev_active:
        diff_color = 0xe67e22
    else:
        diff_color = 0x3498db

    overview_lines = [
        f"📌 **等待到貨：{total_active} 人**（較昨日 {diff_label(total_active, prev_active)}）",
        f"📦 **已配貨待取機：{total_allocated} 人**（較昨日 {diff_label(total_allocated, prev_allocated)}）",
        f"🆕 今日新進等待池：{today_new_count} 筆",
    ]

    # ── Embed 2 & 3：各型號規格「已預約」明細 ────────────────────────
    model_embed_colors = {
        "MacBook Neo":    0x9b59b6,   # 🟣 紫
        "MacBook Air M5": 0x3498db,   # 🔵 藍
    }
    model_embeds = []
    for model in models:
        dot   = MODEL_DOT.get(model, "⚪")
        curr_m = stats["by_model_active"].get(model, 0)
        prev_m = yesterday.get("by_model_active", {}).get(model) if yesterday else None
        if prev_m is not None:
            d = curr_m - prev_m
            sign = "+" if d >= 0 else ""
            total_diff = f"　較昨日 {sign}{d}"
        else:
            total_diff = ""

        # 各規格「已預約」數量
        prod_active = stats["by_model_prod_active"].get(model, {})
        spec_lines = (
            [f"　{short_spec(s)}：**{c} 人**"
             for s, c in sorted(prod_active.items(), key=lambda x: -x[1])]
            if prod_active else ["　（尚無資料）"]
        )

        # 各規格「已配貨」數量
        curr_alloc = stats["by_model_allocated"].get(model, 0)
        prev_alloc = yesterday.get("by_model_allocated", {}).get(model) if yesterday else None
        alloc_diff = ""
        if prev_alloc is not None:
            d = curr_alloc - prev_alloc
            alloc_diff = f"　較昨日 {'+'if d>=0 else ''}{d}"
        prod_alloc = stats["by_model_prod_allocated"].get(model, {})
        alloc_lines = (
            [f"　{short_spec(s)}：**{c} 人**"
             for s, c in sorted(prod_alloc.items(), key=lambda x: -x[1])]
            if prod_alloc else ["　（尚無資料）"]
        )

        description = (
            f"📌 等待到貨：**{curr_m} 人**{total_diff}\n"
            + "\n".join(spec_lines)
            + f"\n\n📦 已配貨待取機：**{curr_alloc} 人**{alloc_diff}\n"
            + "\n".join(alloc_lines)
        )
        model_embeds.append({
            "title":       f"{dot} {model} 規格明細",
            "description": description,
            "color":       model_embed_colors.get(model, 0x95a5a6),
        })

    # ── Embed 4：各門市「已預約」明細（Neo / Air M5 分開 + 每日差異）──
    def diff_str(cur, prev):
        if prev is None or cur == prev:
            return str(cur)
        d = cur - prev
        return f"{cur}（+{d}）" if d > 0 else f"{cur}（{d}）"

    store_lines = []
    for store in stores:
        curr_active = stats["by_store_model_active"].get(store, {})
        prev_active_store = (yesterday.get("by_store_model_active", {}).get(store, {})
                             if yesterday else {})

        # 門市合計
        store_total = sum(curr_active.get(m, 0) for m in models)
        prev_store_total = (
            sum(prev_active_store.get(m, 0) for m in models) if yesterday else None
        )
        if prev_store_total is not None:
            d = store_total - prev_store_total
            sign = "+" if d >= 0 else ""
            total_str = f"**{store_total}**（{sign}{d}）"
        else:
            total_str = f"**{store_total}**"

        # 已預約：各型號，有變動才顯示差異
        model_parts = []
        for m in models:
            cur   = curr_active.get(m, 0)
            prev  = prev_active_store.get(m) if yesterday else None
            dot   = MODEL_DOT.get(m, "⚪")
            short = MODEL_SHORT.get(m, m)
            if prev is not None and cur != prev:
                d    = cur - prev
                sign = "+" if d >= 0 else ""
                model_parts.append(f"{dot}{short} **{cur}**（{sign}{d}）")
            else:
                model_parts.append(f"{dot}{short} **{cur}**")

        # 已配貨：有才顯示
        curr_alloc_store = stats["by_store_model_allocated"].get(store, {})
        prev_alloc_store = (yesterday.get("by_store_model_allocated", {}).get(store, {})
                            if yesterday else {})
        alloc_total = sum(curr_alloc_store.get(m, 0) for m in models)
        alloc_parts = []
        for m in models:
            c = curr_alloc_store.get(m, 0)
            if c:
                alloc_parts.append(f"{MODEL_DOT.get(m,'⚪')}{MODEL_SHORT.get(m,m)} {c}")
        alloc_str = (
            f"\n　　📦配貨 {'　'.join(alloc_parts)}" if alloc_total else ""
        )

        store_lines.append(
            f"**{store}**　{total_str}人　{'　'.join(model_parts)}{alloc_str}"
        )

    # ── Embed 5：今日新增（🟣🔵 顏色區分型號）────────────────────────
    new_items = stats["today_new"]
    if new_items:
        new_by_store = defaultdict(lambda: defaultdict(list))
        for item in new_items:
            new_by_store[item["store"]][item["model"]].append(item["product"])

        new_lines = []
        for store in sorted(new_by_store.keys()):
            for model in models:
                products = new_by_store[store].get(model, [])
                if not products:
                    continue
                dot  = MODEL_DOT.get(model, "⚪")
                specs = defaultdict(int)
                for p in products:
                    specs[p] += 1
                spec_str = "、".join(
                    f"{short_spec(s)}×{n}" if n > 1 else short_spec(s)
                    for s, n in specs.items()
                )
                new_lines.append(
                    f"{dot} **{store}**｜{MODEL_SHORT.get(model, model)}：{spec_str}"
                )
    else:
        new_lines = ["今日尚無新增預約"]

    # ── 組合所有 embeds ───────────────────────────────────────────────
    embeds = [
        {
            "title":       f"📱 Studio A 新機預約日報　{today_str}",
            "description": "\n".join(overview_lines),
            "color":       diff_color,
        },
        *model_embeds,   # Embed 2（Neo）& Embed 3（Air M5）
        {
            "title":       "🏪 各門市明細",
            "description": "\n".join(store_lines),
            "color":       0xe67e22,
        },
        {
            "title":       f"🆕 今日新增（共 {len(new_items)} 筆）",
            "description": "\n".join(new_lines),
            "color":       0x1abc9c,
            "footer":      {"text": f"自動報表 · 每日 09:00 更新　｜　📊 完整報表：{cfg.get('pages_url','')}"},
            "timestamp":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]

    resp = requests.post(webhook, json={"embeds": embeds}, timeout=15)
    resp.raise_for_status()
    print(f"  ✅ Discord 通知已發送（{resp.status_code}）")

# ── HTML 報表 ─────────────────────────────────────────────────────────
def generate_html(cfg, stats, today_str, history):
    models      = list(cfg["activities"].keys())
    shops       = cfg.get("shops", [])
    MODEL_COLOR = {"MacBook Neo": "#9b59b6", "MacBook Air M5": "#3498db"}
    MODEL_SHORT = {"MacBook Neo": "Neo", "MacBook Air M5": "Air M5"}
    MODEL_DOT   = {"MacBook Neo": "🟣", "MacBook Air M5": "🔵"}

    total_active    = sum(stats["by_model_active"].values())
    total_allocated = sum(stats["by_model_allocated"].values())
    today_new_count = len(stats["today_new"])

    # ── 折線圖資料（歷史）──────────────────────────────────────────────
    sorted_dates = sorted(history.keys())
    chart_labels = json.dumps(sorted_dates, ensure_ascii=False)
    chart_datasets = []
    palette = {"MacBook Neo": "#9b59b6", "MacBook Air M5": "#3498db"}
    for model in models:
        vals = [history[d].get("by_model_active", {}).get(model, 0) for d in sorted_dates]
        chart_datasets.append({
            "label":           MODEL_DOT.get(model, "") + " " + model,
            "data":            vals,
            "borderColor":     palette.get(model, "#888"),
            "backgroundColor": palette.get(model, "#888") + "22",
            "tension":         0.4,
            "fill":            True,
            "pointRadius":     4,
            "pointHoverRadius": 6,
        })
    chart_datasets_json = json.dumps(chart_datasets, ensure_ascii=False)

    # ── 規格明細 HTML ──────────────────────────────────────────────────
    spec_cards = ""
    for model in models:
        color        = MODEL_COLOR.get(model, "#888")
        dot          = MODEL_DOT.get(model, "")
        active_total = stats["by_model_active"].get(model, 0)
        alloc_total  = stats["by_model_allocated"].get(model, 0)

        active_rows = ""
        for spec, cnt in sorted(stats["by_model_prod_active"].get(model, {}).items(), key=lambda x: -x[1]):
            pct = round(cnt / active_total * 100) if active_total else 0
            active_rows += f'<tr><td>{short_spec(spec)}</td><td class="num">{cnt}</td><td><div class="bar-wrap"><div class="bar" style="width:{pct}%;background:{color}"></div></div></td></tr>'

        alloc_rows = ""
        for spec, cnt in sorted(stats["by_model_prod_allocated"].get(model, {}).items(), key=lambda x: -x[1]):
            alloc_rows += f'<tr><td>{short_spec(spec)}</td><td class="num">{cnt}</td><td></td></tr>'
        alloc_section = f'<div class="alloc-label">📦 已配貨待取機：{alloc_total} 人</div><table class="spec-table"><thead><tr><th>規格</th><th>人數</th><th></th></tr></thead><tbody>{alloc_rows or "<tr><td colspan=3 class=muted>無</td></tr>"}</tbody></table>'

        spec_cards += f'<div class="card" style="border-top:4px solid {color}"><div class="card-title">{dot} {model}</div><div class="stat-row"><div class="stat-box"><div class="stat-num">{active_total}</div><div class="stat-label">📌 等待到貨</div></div><div class="stat-box"><div class="stat-num">{alloc_total}</div><div class="stat-label">📦 已配貨</div></div></div><div class="section-label">等待到貨規格</div><table class="spec-table"><thead><tr><th>規格</th><th>人數</th><th></th></tr></thead><tbody>{active_rows or "<tr><td colspan=3 class=muted>無</td></tr>"}</tbody></table>{alloc_section}</div>'

    # ── 門市 HTML ──────────────────────────────────────────────────────
    store_rows = ""
    for store in shops:
        active = stats["by_store_model_active"].get(store, {})
        alloc  = stats["by_store_model_allocated"].get(store, {})
        total  = sum(active.get(m, 0) for m in models)
        neo    = active.get("MacBook Neo", 0)
        air    = active.get("MacBook Air M5", 0)
        a_parts = []
        if alloc.get("MacBook Neo"): a_parts.append(f'🟣{alloc["MacBook Neo"]}')
        if alloc.get("MacBook Air M5"): a_parts.append(f'🔵{alloc["MacBook Air M5"]}')
        alloc_str = " ".join(a_parts) if a_parts else "—"
        store_rows += f'<tr><td class="store-name">{store}</td><td class="num">{total}</td><td><span class="neo-badge">🟣 {neo}</span></td><td><span class="air-badge">🔵 {air}</span></td><td class="alloc-cell">{alloc_str}</td></tr>'

    # ── 今日新增 HTML ──────────────────────────────────────────────────
    new_by_store = defaultdict(lambda: defaultdict(list))
    for it in stats["today_new"]:
        new_by_store[it["store"]][it["model"]].append(it["product"])

    new_rows = ""
    for store in sorted(new_by_store):
        for model in models:
            prods = new_by_store[store].get(model, [])
            if not prods: continue
            specs = defaultdict(int)
            for p in prods: specs[p] += 1
            for spec, cnt in specs.items():
                badge_color = "#9b59b6" if model == "MacBook Neo" else "#3498db"
                label = MODEL_SHORT.get(model, model)
                cnt_str = f" ×{cnt}" if cnt > 1 else ""
                new_rows += f'<tr><td><span class="badge" style="background:{badge_color}">{label}</span></td><td>{store}</td><td>{short_spec(spec)}{cnt_str}</td></tr>'

    new_section = (
        f"<table class='data-table'><thead><tr><th>型號</th><th>門市</th><th>規格</th></tr></thead><tbody>{new_rows}</tbody></table>"
        if new_rows else "<div class='empty'>今日尚無新增預約</div>"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Studio A 預約日報 {today_str}</title>
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
.green .ov-num{{color:#27ae60}}.purple .ov-num{{color:#9b59b6}}
.blue .ov-num{{color:#3498db}}.orange .ov-num{{color:#e67e22}}
.chart-card{{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}}
.chart-card h2{{font-size:15px;font-weight:700;margin-bottom:16px;color:#34495e}}
.section-title{{font-size:15px;font-weight:700;margin:24px 0 12px;color:#34495e}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}}
@media(max-width:700px){{.cards{{grid-template-columns:1fr}}}}
.card{{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.card-title{{font-size:16px;font-weight:700;margin-bottom:14px}}
.stat-row{{display:flex;gap:12px;margin-bottom:16px}}
.stat-box{{background:#f8f9fa;border-radius:8px;padding:10px 16px;flex:1;text-align:center}}
.stat-num{{font-size:26px;font-weight:800}}
.stat-label{{font-size:11px;color:#7f8c8d;margin-top:2px}}
.section-label{{font-size:11px;font-weight:600;color:#7f8c8d;margin:12px 0 6px;text-transform:uppercase;letter-spacing:.5px}}
.alloc-label{{font-size:12px;font-weight:600;color:#e67e22;margin:14px 0 6px}}
.spec-table{{width:100%;border-collapse:collapse;font-size:13px}}
.spec-table th{{text-align:left;padding:6px 8px;border-bottom:2px solid #eee;color:#7f8c8d;font-size:11px;font-weight:600}}
.spec-table td{{padding:7px 8px;border-bottom:1px solid #f0f0f0}}
.bar-wrap{{background:#f0f0f0;border-radius:4px;height:6px;width:100px}}
.bar{{height:6px;border-radius:4px}}
.data-table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);font-size:14px}}
.data-table th{{padding:10px 14px;background:#f8f9fa;text-align:left;font-size:12px;color:#7f8c8d;font-weight:600}}
.data-table td{{padding:12px 14px;border-bottom:1px solid #f0f0f0}}
.store-name{{font-weight:700}}.num{{text-align:right;font-weight:700}}
.neo-badge{{background:#f3e8ff;color:#7c3aed;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}}
.air-badge{{background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}}
.alloc-cell{{font-size:13px;color:#e67e22;font-weight:600}}
.badge{{color:#fff;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}}
.muted{{color:#aaa}}.empty{{background:#fff;border-radius:12px;padding:20px;text-align:center;color:#aaa;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.footer{{margin-top:32px;text-align:center;font-size:12px;color:#bdc3c7}}
</style>
</head>
<body>
<h1>📱 Studio A 新機預約日報</h1>
<div class="subtitle">{today_str} · {cfg.get("region_name","")}{len(shops)} 門市</div>

<div class="overview">
  <div class="ov-box green"><div class="ov-num">{total_active}</div><div class="ov-label">📌 等待到貨總人數</div></div>
  <div class="ov-box orange"><div class="ov-num">{total_allocated}</div><div class="ov-label">📦 已配貨待取機</div></div>
  <div class="ov-box purple"><div class="ov-num">{stats["by_model_active"].get("MacBook Neo",0)}</div><div class="ov-label">🟣 MacBook Neo 等待</div></div>
  <div class="ov-box blue"><div class="ov-num">{stats["by_model_active"].get("MacBook Air M5",0)}</div><div class="ov-label">🔵 MacBook Air M5 等待</div></div>
  <div class="ov-box"><div class="ov-num">{today_new_count}</div><div class="ov-label">🆕 今日新增預約</div></div>
</div>

<div class="chart-card">
  <h2>📈 等待到貨人數走勢</h2>
  <canvas id="trendChart" height="90"></canvas>
</div>

<div class="section-title">各型號規格明細</div>
<div class="cards">{spec_cards}</div>

<div class="section-title">各門市明細</div>
<table class="data-table">
  <thead><tr><th>門市</th><th style="text-align:right">等待</th><th>Neo</th><th>Air M5</th><th>已配貨</th></tr></thead>
  <tbody>{store_rows}</tbody>
</table>

<div class="section-title">今日新增（共 {today_new_count} 筆）</div>
{new_section}

<div class="footer">自動報表 · Studio A {cfg.get("region_name","")} · {today_str}</div>

<script>
const ctx = document.getElementById('trendChart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: {chart_labels},
    datasets: {chart_datasets_json}
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ position: 'top' }},
      tooltip: {{
        callbacks: {{
          label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y + ' 人'
        }}
      }}
    }},
    scales: {{
      y: {{
        beginAtZero: false,
        ticks: {{ callback: v => v + ' 人' }}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

    out_path = Path(cfg.get("html_output", str(REPO_DIR / "docs" / "index.html")))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"  ✅ HTML 報表已產生：{out_path}")
    return out_path


# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="n1", choices=["n1", "n2"],
                        help="執行區域：n1=北一區, n2=北二區")
    args = parser.parse_args()

    cfg       = load_config(args.region)
    today_str = datetime.now().strftime("%Y/%m/%d")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始抓取預約資料...")
    try:
        items = fetch_all_reservations(cfg)
    except Exception as e:
        print(f"❌ API 呼叫失敗：{e}")
        sys.exit(1)

    print(f"  ✅ 取得 {len(items)} 筆資料（我方門市）")

    stats     = analyse(items, cfg, today_str)
    history   = load_history(cfg)
    yesterday = get_yesterday(history, today_str)
    save_history(cfg, today_str, stats)
    history   = load_history(cfg)   # reload with today included for chart

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 產生 HTML 報表...")
    try:
        generate_html(cfg, stats, today_str, history)
    except Exception as e:
        print(f"⚠️  HTML 產生失敗（不影響 Discord）：{e}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 發送 Discord 通知...")
    try:
        send_discord(cfg, stats, today_str, yesterday)
    except Exception as e:
        print(f"❌ Discord 發送失敗：{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
