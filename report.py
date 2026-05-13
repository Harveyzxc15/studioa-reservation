#!/usr/bin/env python3
"""
Studio A 新機預約自動通知
每天早上9點自動執行，透過 Discord 傳送 MacBook Neo / MacBook Air M5 預約摘要
"""

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

# GitHub Actions 執行時從環境變數注入敏感資訊
REPO_DIR = Path(__file__).parent

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        # GitHub Actions 模式：從環境變數讀取
        cfg = {
            "token":           os.environ["STUDIOA_TOKEN"],
            "base_url":        "https://www.studioa.com.tw/backend/api/shopcms",
            "activities": {
                "MacBook Neo":    "3a1ff33e-40e5-9fc9-349b-9ac47b354fb0",
                "MacBook Air M5": "3a1ff280-2379-5b06-7c11-319979aa2c59",
            },
            "shops":           ["士林", "大葉高島屋", "微風", "羅東", "美麗華", "阿波羅"],
            "history_file":    str(REPO_DIR / "state" / "history.json"),
            "discord_webhook": os.environ["DISCORD_WEBHOOK"],
        }
    return cfg

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
        # 只保留屬於我方門市的資料
        if my_shops:
            items = [it for it in items if it.get("shopName") in my_shops]
        all_items.extend(items)

        fetched_so_far = skip + page_size
        if fetched_so_far >= data["userReservationListOutDtos"]["totalCount"]:
            break
        skip += page_size

    return all_items

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

    # ── 共用輔助：產品名稱精簡化 ─────────────────────────────────────
    def short_spec(product):
        """'MacBook Neo (13吋，A18 Pro) (8GB/512GB) / 四色/胭粉色'
           → '8GB/512GB｜胭粉色'"""
        first  = product.find("(")
        second = product.find("(", first + 1) if first != -1 else -1
        if second == -1:
            return product
        rest = product[second + 1:].replace("四色/", "").replace("雙色/", "")
        if ") / " in rest:
            spec, color = rest.split(") / ", 1)
            return f"{spec}｜{color.strip()}"
        return rest.rstrip(")")

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
            "footer":      {"text": "自動報表 · 每日 09:00 更新"},
            "timestamp":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]

    resp = requests.post(webhook, json={"embeds": embeds}, timeout=15)
    resp.raise_for_status()
    print(f"  ✅ Discord 通知已發送（{resp.status_code}）")

# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    cfg       = load_config()
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

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 發送 Discord 通知...")
    try:
        send_discord(cfg, stats, today_str, yesterday)
    except Exception as e:
        print(f"❌ Discord 發送失敗：{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
