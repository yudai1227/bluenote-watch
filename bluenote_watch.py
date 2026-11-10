#!/usr/bin/env python3
"""
Blue Note Tokyo 空席ウォッチャー(個人利用向け)

/reserve/plan はセッションに紐づくページのため、直接は監視できません。
毎回 /reserve/schedule/exec/<公演ID> から入り直し、「公演日時を選ぶ」の
日付ごとの 1st / 2nd カード(○ 空席あり / △ 残りわずか / × 空席なし)を読み取ります。

準備:
    pip install requests beautifulsoup4

使い方:
    # 1) まず1回だけ実行して、読み取り結果を確認(HTMLも保存)
    python bluenote_watch.py 3990 --once --dump

    # 2) 常駐監視(5分ごと)。全ステージが対象
    export NTFY_TOPIC="自分だけが知っている長いトピック名"
    python bluenote_watch.py 3990

    # 日付やステージで絞る場合(複数指定は AND)
    python bluenote_watch.py 3990 --match "11.23" --match "1st"

公演IDはスケジュールページの各公演リンクの数字
    https://reserve.bluenote.co.jp/reserve/schedule/exec/3990

通知先(環境変数。未設定ならターミナル表示のみ):
    NTFY_TOPIC            ntfy.sh のトピック名(スマホに ntfy アプリを入れて購読)
    DISCORD_WEBHOOK_URL   Discord の Webhook URL
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://reserve.bluenote.co.jp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MIN_INTERVAL = 60           # サーバーに負荷をかけないための下限(秒)

STAGE_RE = re.compile(r"^\s*(1st|2nd|3rd)\s*$", re.I)
STAGE_ANY = re.compile(r"\b(1st|2nd|3rd)\b", re.I)
DATE_RE = re.compile(r"\d{4}\s*\d{1,2}\.\d{1,2}(\s*[a-z]{3}\.?)?", re.I)

# 文字や alt で状態が書かれている場合
TEXT_MARKS = [
    ("full", ["×", "✕", "✖", "空席なし", "満席", "受付終了", "SOLD OUT"]),
    ("few", ["△", "▲", "残りわずか"]),
    ("open", ["○", "◯", "〇", "◎", "空席あり"]),
]
# class 名や画像ファイル名で状態が表されている場合(単語単位で照合)
ATTR_WORDS = [
    ("full", {"full", "soldout", "sold", "batsu", "cross", "disabled", "ng",
              "none", "closed", "close", "off", "no", "empty"}),
    ("few", {"few", "little", "sankaku", "triangle", "last", "low", "remain"}),
    ("open", {"ok", "maru", "circle", "vacant", "available", "on", "yes"}),
]
SYMBOL = {"full": "×", "few": "△", "open": "○", "unknown": "?"}
NAME = {"full": "空席なし", "few": "残りわずか", "open": "空席あり",
        "unknown": "状態不明(表示が変化)"}


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def find_card(el):
    """'1st' などの文字から、そのステージ1枚分のカード要素までさかのぼる"""
    best, cur = el, el
    while cur.parent is not None and cur.parent.name not in ("body", "html", "[document]"):
        p = cur.parent
        txt = p.get_text(" ")
        if len(STAGE_ANY.findall(txt)) > 1 or DATE_RE.search(txt):
            break
        best = cur = p
    return best


def card_signals(card, soup):
    """カードの見た目を決めていそうな属性を集める(状態判定と変化検知に使う)"""
    nodes = [card] + card.find_all(True)
    if card.name == "label" and card.get("for"):
        inp = soup.find(id=card["for"])
        if inp is not None:
            nodes.append(inp)
    prev = card.find_previous_sibling()
    if prev is not None and prev.name == "input":
        nodes.append(prev)

    toks, disabled = [], False
    for n in nodes:
        toks += [f"class={c}" for c in n.get("class", [])]
        for a in ("src", "alt", "title", "aria-label", "aria-disabled",
                  "xlink:href", "data-status", "data-state"):
            if n.get(a):
                toks.append(f"{a}={n[a]}")
        if n.name == "use" and n.get("href"):
            toks.append(f"use={n['href']}")
        if n.name == "a" and n.get("href"):
            toks.append("has-link")
        if n.has_attr("disabled") or n.get("aria-disabled") == "true":
            disabled = True
    if disabled:
        toks.append("disabled")
    return sorted(set(toks))


def classify(card, toks):
    text = norm(card.get_text(" ")) + " " + " ".join(
        t.split("=", 1)[1] for t in toks if t.startswith(("alt=", "title=", "aria-label=")))
    upper = text.upper()
    for status, marks in TEXT_MARKS:
        if any(m in upper for m in marks):
            return status, True
    if "disabled" in toks:
        return "full", True
    words = set(re.findall(r"[a-z]+", " ".join(toks).lower()))
    for status, kw in ATTR_WORDS:
        if words & kw:
            return status, False       # class 名などからの推測
    return "unknown", False


def extract_slots(html):
    """HTML から {"2026 11.23 mon. 1st": {"status":..., "sig":..., "toks":[...]}}"""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    slots = {}
    for node in soup.find_all(string=STAGE_RE):
        card = find_card(node.parent)
        date_node = card.find_previous(string=DATE_RE)
        date = norm(DATE_RE.search(str(date_node)).group(0)) if date_node else "日付不明"
        key = f"{date} {norm(node)}"
        if key in slots:
            continue
        toks = card_signals(card, soup)
        raw = " ".join(toks) + "|" + norm(card.get_text(" "))
        status, sure = classify(card, toks)
        slots[key] = {"status": status, "sure": sure,
                      "sig": hashlib.sha1(raw.encode()).hexdigest()[:12],
                      "toks": toks}
    return slots, norm(soup.get_text(" "))


def fetch(event_id):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    r = s.get(f"{BASE}/reserve/schedule/exec/{event_id}", timeout=30)
    r.raise_for_status()
    if "/error/" in r.url:
        raise RuntimeError(f"エラーページに転送されました: {r.url}")
    return r.url, r.text


def notify(title, body, url):
    print(f"\n🔔 {title}\n{body}\n{url}\n", flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post("https://ntfy.sh/", timeout=15, json={
                "topic": topic, "title": title, "message": body,
                "click": url, "priority": 5, "tags": ["musical_note"]})
        except requests.RequestException as e:
            print(f"ntfy 送信失敗: {e}", file=sys.stderr)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, timeout=15,
                          json={"content": f"**{title}**\n{body}\n{url}"})
        except requests.RequestException as e:
            print(f"Discord 送信失敗: {e}", file=sys.stderr)


def check(args, state_path):
    url, html = fetch(args.event_id)
    if args.dump:
        p = Path(f"bluenote_plan_{args.event_id}.html")
        p.write_text(html, encoding="utf-8")
        print(f"HTML を保存しました: {p.resolve()}")

    slots, page_text = extract_slots(html)
    prev = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    prev_slots = prev.get("slots", {})
    link = f"{BASE}/reserve/schedule/exec/{args.event_id}"
    now = datetime.now().strftime("%m/%d %H:%M:%S")

    if slots:
        watched = {k: v for k, v in slots.items()
                   if all(m in k for m in args.match)}
        summary = " ".join(f"{SYMBOL[s]}{sum(v['status'] == s for v in watched.values())}"
                           for s in ("open", "few", "full", "unknown"))
        print(f"[{now}] 監視対象 {len(watched)} 件  {summary}", flush=True)
        if args.dump or not prev_slots:
            for k, v in watched.items():
                print(f"   {SYMBOL[v['status']]}  {k}")
                if args.dump:
                    print(f"        判定材料: {' '.join(v['toks']) or '(なし)'}")
        if args.match and not watched:
            print("   ※ --match に合う枠がありません。--dump で名前を確認してください")

        changed = []
        for k, v in watched.items():
            old = prev_slots.get(k)
            if not isinstance(old, dict) or old.get("sig") == v["sig"]:
                continue                       # 初回、または変化なし
            if v["status"] == "full" and v["sure"]:
                continue                       # 確実に満席のまま/満席に戻った
            if v["status"] == "full":          # 推測では満席だが表示が変わった
                changed.append(f"・{k}:表示が変化(要確認)")
            else:
                changed.append(f"・{k}:{NAME[v['status']]}")
        if changed:
            notify("Blue Note 空席が出ました", "\n".join(changed), link)
    else:
        # カードを見つけられなかった場合の保険: ページ本文の変化だけを見る
        digest = hashlib.sha256(page_text.encode()).hexdigest()
        print(f"[{now}] 1st/2nd のカードを読み取れませんでした(本文の変化のみ監視)\n"
              f"   着地URL: {url}\n"
              f"   --dump で保存した HTML を確認してください", flush=True)
        if prev.get("digest") and prev["digest"] != digest:
            notify("Blue Note 予約ページに変化あり",
                   "空席かどうかは判定できていません。ページを確認してください。", link)
        prev["digest"] = digest

    prev["slots"] = {k: {"status": v["status"], "sig": v["sig"]}
                     for k, v in slots.items()}
    state_path.write_text(json.dumps(prev, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Blue Note Tokyo 空席ウォッチャー")
    ap.add_argument("event_id", help="公演ID(.../schedule/exec/<ID> の数字)")
    ap.add_argument("--match", action="append", default=[],
                    help='枠の名前で絞り込み。例: --match "11.23" --match "1st"(AND)')
    ap.add_argument("--interval", type=int, default=300,
                    help="確認間隔(秒)。既定 300、下限 60")
    ap.add_argument("--once", action="store_true", help="1回だけ確認して終了(cron 用)")
    ap.add_argument("--dump", action="store_true",
                    help="取得した HTML を保存し、各枠の判定材料も表示")
    ap.add_argument("--test-notify", action="store_true", help="通知のテスト送信")
    args = ap.parse_args()

    if args.test_notify:
        notify("Blue Note ウォッチャー テスト", "通知が届けば設定OKです。",
               f"{BASE}/reserve/schedule/exec/{args.event_id}")
        return

    interval = max(args.interval, MIN_INTERVAL)
    state_path = Path(f"bluenote_state_{args.event_id}.json")

    while True:
        try:
            check(args, state_path)
        except Exception as e:                      # 一時的な失敗では止めない
            print(f"[{datetime.now():%m/%d %H:%M:%S}] 取得失敗: {e}",
                  file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(interval * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    main()
