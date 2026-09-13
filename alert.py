#!/usr/bin/env python3
"""
Studio A 預約即時異動通知
依排程執行，有異動才推送 Discord（新增預約 / 取消 / 放棄）
"""

import argparse
import json
import os
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

def load_region(region):
    """讀區域設定；有 sub_regions 的（如 all）會把子區的門市合併進來"""
    cfg = json.loads((REPO_DIR / "regions" / f"{region}.json").read_text())
    if cfg.get("sub_regions"):
        subs = [json.loads((REPO_DIR / "regions" / f"{r}.json").read_text()) for r in cfg["sub_regions"]]
        cfg["shops"]    = [s for sub in subs for s in sub["shops"]]
        cfg["shop_ids"] = {k: v for sub in subs for k, v in sub["shop_ids"].items()}
        cfg["regions"]  = [{"name": sub["name"], "shops": sub["shops"],
                            "alert_state_file": sub["alert_state_file"]} for sub in subs]
    else:
        cfg["regions"] = [{"name": cfg["name"], "shops": cfg["shops"],
                           "alert_state_file": cfg["alert_state_file"]}]
    return cfg


def load_config(region: str = "n1"):
    region_cfg = load_region(region)
    activities = json.loads((REPO_DIR / "activities.json").read_text())["activities"]

    local = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

    token = os.environ.get("STUDIOA_TOKEN") or local.get("token")
    if not token:
        sys.exit("❌ 找不到 token（環境變數 STUDIOA_TOKEN 或本機 config）")
    webhook = os.environ.get(region_cfg["webhook_env"]) or local.get("discord_webhook")
    if not webhook:
        sys.exit(f"❌ 找不到 webhook（環境變數 {region_cfg['webhook_env']}）")

    return {
        "token":            token,
        "base_url":         "https://www.studioa.com.tw/backend/api/shopcms",
        "activities":       activities,
        "shops":            region_cfg["shops"],
        "shop_ids":         region_cfg["shop_ids"],
        "alert_state_file": str(REPO_DIR / region_cfg["alert_state_file"]),
        "region_name":      region_cfg["name"],
        "regions":          region_cfg["regions"],
        "discord_webhook":  webhook,
    }

# ── API ───────────────────────────────────────────────────────────────
def fetch_activity(cfg, activity_id):
    headers = {"Authorization": cfg["token"]}
    shop_q  = "&".join(f"ShopIds={sid}" for sid in cfg["shop_ids"].values())
    skip, ps = 0, 500
    items = []
    while True:
        url = (
            f"{cfg['base_url']}/reservation-activity/reservation-user-list"
            f"?SkipCount={skip}&MaxResultCount={ps}"
            f"&ReservationActivityIds={activity_id}&{shop_q}"
        )
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        dto = resp.json()["data"]["userReservationListOutDtos"]
        for it in dto["items"]:
            if it.get("shopName"):
                it["shopName"] = it["shopName"].strip()
        items.extend(dto["items"])
        skip += ps
        if skip >= dto["totalCount"] or not dto["items"]:
            break
    return items

def fetch_items(cfg):
    acts = cfg["activities"]
    with ThreadPoolExecutor(max_workers=len(acts)) as pool:
        results = pool.map(lambda a: fetch_activity(cfg, a["id"]), acts)
    out = []
    for r in results:
        out.extend(r)
    return out

# ── 統計 ──────────────────────────────────────────────────────────────
def summarise(items, cfg):
    id_to_group = {a["id"]: a["group"] for a in cfg["activities"]}
    cancel_statuses = {"已取消", "已取消(已遞補)"}
    abandon_statuses = {"放棄", "放棄(已遞補)"}

    by_store_model_active   = defaultdict(lambda: defaultdict(int))
    by_store_cancel_abandon = defaultdict(lambda: defaultdict(int))  # [store][cancel|abandon]

    for it in items:
        store  = it.get("shopName", "")
        model  = id_to_group.get(it["reservationActivityId"], "其他")
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
    if p.exists():
        return json.loads(p.read_text())
    if len(cfg["regions"]) < 2:
        return None
    merged = {"by_store_model_active": {}, "by_store_cancel_abandon": {}}
    for r in cfg["regions"]:
        sp = REPO_DIR / r["alert_state_file"]
        if not sp.exists():
            return None   # 任一子區沒有基準就重新建立，避免誤報
        sub = json.loads(sp.read_text())
        for key in merged:
            merged[key].update(sub.get(key, {}))
    return merged

def save_state(cfg, state):
    p = Path(cfg.get("alert_state_file", str(REPO_DIR / "state" / "alert_state_n1.json")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))

# ── 差異計算 ──────────────────────────────────────────────────────────
def group_maps(cfg):
    """回傳 (順序, emoji 對照, 名稱對照)，同 group 的活動只出現一次"""
    order, dot, short = [], {}, {}
    for a in cfg["activities"]:
        g = a["group"]
        if g not in order:
            order.append(g)
            dot[g]   = a["emoji"]
            short[g] = a["name"]
    return order, dot, short

def detect_changes(curr, prev, cfg):
    models, MODEL_DOT, MODEL_SHORT = group_maps(cfg)
    multi   = len(cfg["regions"]) > 1
    changes = []

    def by_region(line_fn):
        """對每區的門市產生行；多區時加上區名小標，沒變動的區不印"""
        blocks = []
        for r in cfg["regions"]:
            lines = [ln for store in r["shops"] for ln in line_fn(store)]
            if not lines:
                continue
            blocks.append((f"　**{r['name']}**\n" if multi else "") + "\n".join(lines))
        return "\n".join(blocks)

    def active_lines(sign):
        def fn(store):
            out = []
            for model in models:
                cur = curr["by_store_model_active"].get(store, {}).get(model, 0)
                old = prev["by_store_model_active"].get(store, {}).get(model, 0)
                if (sign > 0 and cur > old) or (sign < 0 and cur < old):
                    d = cur - old
                    out.append(f"　{MODEL_DOT.get(model, '⚪')}{MODEL_SHORT.get(model, model)} "
                               f"{store} {'+' if d > 0 else ''}{d}（共 {cur} 人）")
            return out
        return fn

    def ca_lines(kind):
        def fn(store):
            cur = curr["by_store_cancel_abandon"].get(store, {}).get(kind, 0)
            old = prev["by_store_cancel_abandon"].get(store, {}).get(kind, 0)
            return [f"　{store} +{cur - old}（累計 {cur}）"] if cur > old else []
        return fn

    for title, fn in (("➕ **新增等待：**", active_lines(+1)),
                      ("➖ **等待減少：**", active_lines(-1)),
                      ("❌ **取消：**",     ca_lines("cancel")),
                      ("🚫 **放棄：**",     ca_lines("abandon"))):
        body = by_region(fn)
        if body:
            changes.append(f"{title}\n{body}")

    return changes

# ── Discord ───────────────────────────────────────────────────────────
def send_alert(cfg, changes, curr, now_str):
    models, MODEL_DOT, MODEL_SHORT = group_maps(cfg)
    shops  = cfg.get("shops", [])

    # 目前等待池總覽：各區總計＋全部合計
    def region_total(shops):
        return sum(curr["by_store_model_active"].get(s, {}).get(m, 0) for s in shops for m in models)

    total = region_total(cfg.get("shops", []))
    by_model = {m: sum(curr["by_store_model_active"].get(s, {}).get(m, 0) for s in cfg.get("shops", []))
                for m in models}
    model_str = "　".join(
        f"{MODEL_DOT.get(m,'⚪')}{MODEL_SHORT.get(m,m)} {c}"
        for m, c in by_model.items() if c
    ) or "（目前無等待中預約）"

    desc = "\n\n".join(changes)
    if len(cfg["regions"]) > 1:
        region_str = "・".join(f"{r['name']} {region_total(r['shops'])}" for r in cfg["regions"])
        desc += f"\n\n> 目前等待到貨：**{total} 人**（{region_str}）\n> {model_str}"
    else:
        desc += f"\n\n> 目前等待到貨：**{total} 人**　{model_str}"

    region_name = cfg.get("region_name", "")
    embed = {
        "title":       f"⚡ {region_name} 預約異動通知　{now_str}",
        "description": desc,
        "color":       0xf39c12,
        "footer":      {"text": f"{region_name} · 預約異動偵測"},
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
    parser.add_argument("--region", default="n1", choices=["n1", "n2", "all"],
                        help="執行區域：n1=北一區, n2=北二區, all=兩區合併")
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
