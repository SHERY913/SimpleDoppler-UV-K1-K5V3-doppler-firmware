#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 palewire/ham-satellite-database 的 AMSAT 频率库补频点。

为什么用它：官方 SatNOGS API（db.satnogs.org）在国内直连 21 s 超时，一次都
拉不回来；这个 JSON 走 raw.githubusercontent.com，656 KB / 2,870 条 / 1,757 颗，
十几秒下完，是当前唯一能稳定拿到的机器可读频点源。它本身就是 AMSAT 官网
频率页的抓取结果，出处与 tools/fetch_amsat_freqs.py 同源。

数据口径（与 数据/sat_freq.json 的 `_rules` 一致）：
  * tone 不在这份数据里 —— 一律写 0，不编造哑音；
  * 线性/宽带记录写中心频率；FM 的两段写成"a/b"的取第一个（那是主信道）；
  * status 不是 active 的照样入表（还能收听），但加 `_unverified: true` 并把
    状态写进 blurb，界面不会把它当"现役可用"；
  * 只写"界面上仍显示频率未知"的星，已经有的条目不覆盖。

用法：
    python tools/fetch_palewire_freqs.py                 # 只报告，不改数据
    python tools/fetch_palewire_freqs.py --write         # 写进 数据/sat_freq.json
    python tools/fetch_palewire_freqs.py --offline       # 用上次缓存的 JSON
之后照常跑 embed_transponders.py --write → gen_sat_freq.py --check。
"""
import argparse
import json
import re
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "数据", "sat_freq.json")
ALLOW = os.path.join(ROOT, "数据", "amateur_allow.json")
CACHE = os.path.join(HERE, "_amsat_raw", "palewire_amsat.json")
URL = ("https://raw.githubusercontent.com/palewire/ham-satellite-database/"
       "main/data/amsat-all-frequencies.json")

sys.path.insert(0, os.path.join(ROOT, "PC工具", "多普勒工具"))
import sat_freq  # noqa: E402


def fetch(offline=False):
    if offline:
        if not os.path.exists(CACHE):
            sys.exit(f"没有缓存：{CACHE}")
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(URL, headers={"User-Agent": "SimpleDoppler/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        f.write(raw)
    print(f"下载 {len(raw)} B → {CACHE}")
    return json.loads(raw)


def mhz(text):
    """'435.310' / '435.822/435.867' / '145.875-145.950' → MHz（float）或 None。"""
    s = str(text or "").strip().replace(" ", "")
    if not s:
        return None
    for sep in ("/", "-", "–"):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            vals = []
            for p in parts:
                try:
                    vals.append(float(p))
                except ValueError:
                    pass
            if not vals:
                return None
            if len(vals) >= 2 and abs(vals[1] - vals[0]) <= 0.2:
                return round((vals[0] + vals[1]) / 2, 4)      # 线性/宽带：中心
            return round(vals[0], 4)                          # 双信道：主信道
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def in_band(v):
    return v is not None and 29.0 <= v <= 1300.0


def pick(rows):
    """同一颗星多条记录：先 active，再带下行，再单值，最后任意。"""
    def key(r):
        dl = str(r.get("downlink") or "")
        return (0 if (r.get("status") or "").lower() == "active" else 1,
                0 if dl and "/" not in dl and "-" not in dl else 1,
                0 if r.get("uplink") else 1,
                r.get("name") or "")
    for r in sorted(rows, key=key):
        if in_band(mhz(r.get("downlink"))):
            return r
    return None


def build_entry(norad, rec):
    down = mhz(rec.get("downlink"))
    up = mhz(rec.get("uplink"))
    if not in_band(up):
        up = 0.0
    mode = (rec.get("mode") or "").strip() or "下行"
    status = (rec.get("status") or "").strip()
    cs = (rec.get("callsign") or "").strip()
    # 源里的呼号字段常常是脏的（"ITMSAT-11 12" 这种把型号和月份粘在一起），
    # 只有长得像呼号的才写进 blurb，不然简介里全是噪声。
    if not re.match(r"^[A-Z]{1,3}[0-9][A-Z]{1,4}$", cs.replace(" ", "")):
        cs = ""
    bits = [f"AMSAT 频率库：{mode}，下行 {down:.3f}"]
    if up:
        bits.append(f"上行 {up:.3f}")
    if status and status.lower() != "active":
        bits.append(f"状态 {status}（已失效，仅供监听）")
    if cs:
        bits.append(f"呼号 {cs}")
    blurb = "，".join(bits) + "。"
    note = "线性的写中心频率；哑音该源不提供，记 0。"
    entry = {
        "name": (rec.get("name") or str(norad)).strip()[:24],
        "down": round(down, 4),
        "up": round(up, 4),
        "tone": 0.0,
        "mode": mode,
        "blurb": blurb,
        "transponders": [{
            "name": mode,
            "down": round(down, 4),
            "up": round(up, 4),
            "tone": 0.0,
            "note": note,
        }],
    }
    if status and status.lower() != "active":
        entry["_unverified"] = True
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写进 数据/sat_freq.json")
    ap.add_argument("--offline", action="store_true", help="用缓存，不联网")
    a = ap.parse_args()

    rows = fetch(a.offline)
    by = {}
    for r in rows:
        try:
            by.setdefault(int(r["norad_id"]), []).append(r)
        except (KeyError, TypeError, ValueError):
            continue

    with open(ALLOW, encoding="utf-8") as f:
        allow = json.load(f)
    keep = [int(x["norad"]) for x in allow["keep"]]
    known = set(sat_freq.FREQ) | set(sat_freq.EMBEDDED_TRANS) | set(sat_freq.EXTRA_FREQ)
    unknown = [n for n in keep if n not in known]

    add, blank = {}, []
    for n in unknown:
        rec = pick(by.get(n) or [])
        if rec is None:
            blank.append(n)
            continue
        add[n] = build_entry(n, rec)

    print(f"默认视图未知 {len(unknown)} 颗；可补 {len(add)} 颗；仍缺 {len(blank)} 颗")
    act = sum(1 for e in add.values() if not e.get("_unverified"))
    print(f"  其中 active {act} 颗，失效/未确认 {len(add) - act} 颗")
    for n, e in list(add.items())[:8]:
        print(f"  {n:>6}  {e['name'][:22]:<22} {e['down']:>10.3f} ↓  {e['up']:>9.3f} ↑  {e['mode']}")
    if blank:
        print(f"  仍缺：{blank}")

    if not a.write:
        print("\n（只报告。加 --write 才写进数据表。）")
        return

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    sats = data["sats"]
    n_new = 0
    for n, e in add.items():
        k = str(n)
        if k in sats:
            continue
        sats[k] = e
        n_new += 1
    # 键按 NORAD 数字排序，和已有条目的顺序一致，diff 才不会到处跳
    data["sats"] = {k: sats[k] for k in sorted(sats, key=lambda x: int(x))}
    with open(DATA, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"写入 {n_new} 条 → {DATA}（现共 {len(data['sats'])} 颗）")


if __name__ == "__main__":
    main()
