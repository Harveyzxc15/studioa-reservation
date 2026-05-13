#!/usr/bin/env python3
"""
Studio A 預約即時異動通知
每 4 小時執行，有異動才推送 Discord（新增預約 / 取消 / 放棄）
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
    region_cfg = json.loads((REPO_DIR / "regions" / f"{region}.json").read_text())

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            base = json.load(f)
        base["shops"]            = region_cfg["shops"]
        base["alert_state_file"] = str(REPO_DIR / region_cfg["alert_state_file"])
        base["region_name"]      = region_cfg["name"]
        return base
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
        "alert_state_file": str(REPO_DIR / region_cfg["alert_state_file"]),
        "region_name":     region_cfg["name"],
        "discord_webhook": os.environ[webhook_key],
    }

# ── API ───────────────────────────────────────────────────────────────
def fetch_items(cfg):
    headers   = {"Authorization": cfg["token"]}
    act_params = "&".join(
        f"ReservationActivityIds={aid}" for aid in cfg["activities"].values()
    )
    base     = cfg["base_url"]
    my_shops = set(cfg.get("shops", []))

    skip, ps = 0, 500
    all_items = []
    while True:
        url = (
            f"{base}/reservation-activity/reservation-user-list"
            f"?SkipCount={skip}&MaxResultCount={ps}&{act_params}"
        )
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        dto = resp.json()["data"]["userReservationListOutDtos"]
        for it in dto["items"]:
            if it.get("shopName"):
                it["shopName"] = it["shopName"].strip()
        items = [it for it in dto["items"] if it.get("shopName") in my_shops]
        all_items.extend(items)
        if skip + ps >= dto["totalCount"]:
            break
        skip += ps
    return all_items

# ── 統計 ──────────────────────────────────────────────────────────────
def summarise(items, cfg):
    id_to_model = {v: k for k, v in cfg["activities"].items()}
    cancel_statuses = {"已取消", "已取消(已遞補)"}
    abandon_statuses = {"放棄", "放棄(已遞補)"}

    by_store_model_active   = defaultdict(lambda: defaultdict(int))
    by_store_cancel_abandon = defaultdict(lambda: defaultdict(int))  # [store][cancel|abandon]

    for it in items:
        store  = it.get("shopName", "")
        model  = id_to_model.get(it["reservationActivityId"], "其他")
        status = it.get("statusName", "")

        if status == "已預約":
            by_store_model_active[store][model] += 1
        elif status in cancel_statuses:
            by_store_cancel_abandon[store]["cancel"] += 1
        elif status in abandon_statuses:
            by_store_cancel_abandon[store]["abandon"] += 1

    return {
        "by_store_model_active":   {s: dict(m) for s, m in by_store_model_active.items()},
        "by_store_cancel_abandon": {s: dict(v) for s, v in by_store_cancel_abandon.items()},
    }

# ── 狀態存取 ──────────────────────────────────────────────────────────
def load_state(cfg):
    p = Path(cfg.get("alert_state_file", str(REPO_DIR / "state" / "alert_state_n1.json")))
    return json.loads(p.read_text()) if p.exists() else None

def save_state(cfg, state):
    p = Path(cfg.get("alert_state_file", str(REPO_DIR / "state" / "alert_state_n1.json")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))

# ── 差異計算 ──────────────────────────────────────────────────────────
MODEL_DOT   = {"MacBook Neo": "🟣", "MacBook Air M5": "🔵"}
MODEL_SHORT = {"MacBook Neo": "Neo", "MacBook Air M5": "Air M5"}

def detect_changes(curr, prev, cfg):
    models = list(cfg["activities"].keys())
    shops  = cfg.get("shops", [])
    changes = []

    # ── 已預約變動 ────────────────────────────────────────────────────
    new_reservations = []
    lost_reservations = []
    for store in shops:
        for model in models:
            cur = curr["by_store_model_active"].get(store, {}).get(model, 0)
            old = prev["by_store_model_active"].get(store, {}).get(model, 0)
            if cur > old:
                dot = MODEL_DOT.get(model, "⚪")
                new_reservations.append(
                    f"　{dot}{MODEL_SHORT.get(model, model)} {store} +{cur - old}（共 {cur} 人）"
                )
            elif cur < old:
                dot = MODEL_DOT.get(model, "⚪")
                lost_reservations.append(
                    f"　{dot}{MODEL_SHORT.get(model, model)} {store} {cur - old}（共 {cur} 人）"
                )

    if new_reservations:
        changes.append("➕ **新增等待：**\n" + "\n".join(new_reservations))
    if lost_reservations:
        changes.append("➖ **等待減少：**\n" + "\n".join(lost_reservations))

    # ── 取消 / 放棄變動 ───────────────────────────────────────────────
    cancel_lines  = []
    abandon_lines = []
    for store in shops:
        cur_c = curr["by_store_cancel_abandon"].get(store, {}).get("cancel", 0)
        old_c = prev["by_store_cancel_abandon"].get(store, {}).get("cancel", 0)
        cur_a = curr["by_store_cancel_abandon"].get(store, {}).get("abandon", 0)
        old_a = prev["by_store_cancel_abandon"].get(store, {}).get("abandon", 0)

        if cur_c > old_c:
            cancel_lines.append(f"　{store} +{cur_c - old_c}（累計 {cur_c}）")
        if cur_a > old_a:
            abandon_lines.append(f"　{store} +{cur_a - old_a}（累計 {cur_a}）")

    if cancel_lines:
        changes.append("❌ **取消：**\n" + "\n".join(cancel_lines))
    if abandon_lines:
        changes.append("🚫 **放棄：**\n" + "\n".join(abandon_lines))

    return changes

# ── Discord ───────────────────────────────────────────────────────────
def send_alert(cfg, changes, curr, now_str):
    models = list(cfg["activities"].keys())
    shops  = cfg.get("shops", [])

    # 目前等待池總覽（簡短）
    total = sum(
        curr["by_store_model_active"].get(s, {}).get(m, 0)
        for s in shops for m in models
    )
    by_model = {}
    for m in models:
        by_model[m] = sum(
            curr["by_store_model_active"].get(s, {}).get(m, 0) for s in shops
        )
    model_str = "　".join(
        f"{MODEL_DOT.get(m,'⚪')}{MODEL_SHORT.get(m,m)} {c}"
        for m, c in by_model.items()
    )

    desc = "\n\n".join(changes)
    desc += f"\n\n> 目前等待到貨：**{total} 人**　{model_str}"

    region_name = cfg.get("region_name", "")
    embed = {
        "title":       f"⚡ {region_name} 預約異動通知　{now_str}",
        "description": desc,
        "color":       0xf39c12,
        "footer":      {"text": f"{region_name} · 每 4 小時偵測一次"},
        "timestamp":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    resp = requests.post(
        cfg["discord_webhook"], json={"embeds": [embed]}, timeout=15
    )
    resp.raise_for_status()
    print(f"  ✅ 異動通知已發送（{len(changes)} 項變動）")

# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="n1", choices=["n1", "n2"],
                        help="執行區域：n1=北一區, n2=北二區")
    args   = parser.parse_args()
    cfg    = load_config(args.region)
    now     = datetime.now()
    now_str = now.strftime("%Y/%m/%d %H:%M")

    print(f"[{now.strftime('%H:%M:%S')}] 開始偵測異動...")
    try:
        items = fetch_items(cfg)
    except Exception as e:
        print(f"❌ API 呼叫失敗：{e}")
        sys.exit(1)

    print(f"  ✅ 取得 {len(items)} 筆資料")
    curr  = summarise(items, cfg)
    prev  = load_state(cfg)

    if prev is None:
        print("  📝 首次執行，儲存基準狀態（不發送通知）")
        save_state(cfg, curr)
        return

    changes = detect_changes(curr, prev, cfg)

    if changes:
        try:
            send_alert(cfg, changes, curr, now_str)
        except Exception as e:
            print(f"❌ Discord 發送失敗：{e}")
            sys.exit(1)
    else:
        print("  ✅ 無異動，靜音")

    save_state(cfg, curr)

if __name__ == "__main__":
    main()
