#!/usr/bin/env python3
import os, sys, json, time, re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
import yaml

STATE_DIR = Path(".state")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "seen.json"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
INIT_MODE = os.getenv("INIT_MODE", "").lower() in ("1", "true", "yes")  # 처음에는 알림 안 보내고 상태만 기록하고 싶을 때

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; KNU-Notice-Bot/1.0; +https://github.com/)",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
})
TIMEOUT = 25

def load_sites():
    with open("sites.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sites"]

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch(url, retries=3, backoff=2):
    last_err = None
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            raw = r.content  # bytes
            best = from_bytes(raw).best()
            html = str(best) if best is not None else raw.decode('utf-8', errors='replace')
            return html
        except Exception as e:
            last_err = e
            time.sleep(backoff * (i+1))
    raise last_err

def textnorm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def safe_text(s: str) -> str:
    # 제어문자/제로폭문자 제거
    return re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", s or "").strip()

def discord_post(title, url, site_name, date_text=None):
    if not DISCORD_WEBHOOK:
        print("WARN: DISCORD_WEBHOOK not set; skipping post", file=sys.stderr)
        return
    title = safe_text(title)
    site_name = safe_text(site_name)
    date_text = safe_text(date_text) if date_text else None
    content = f"**[{site_name}] 새 공지**\n{title}"
    if url:
        content += f"\n{url}"
    if date_text:
        content += f"\n게시일: {date_text}"
    resp = SESSION.post(DISCORD_WEBHOOK, json={"content": content}, timeout=TIMEOUT)
    if resp.status_code >= 300:
        print(f"Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)

def should_skip_row(row, site):
    # 고정공지/공지 스킵 규칙
    skip_rules = site.get("skip_if_selector", [])
    if not skip_rules:
        return False
    for sel in skip_rules:
        if ":contains(" in sel:
            base_sel, text = sel.split(":contains(", 1)
            text = text.rstrip(")").strip("'\"")
            for el in row.select(base_sel):
                if text in (el.get_text() or ""):
                    return True
        else:
            if row.select_one(sel):
                return True
    return False

def extract_date(row, site):
    date_text = None
    sel = site.get("date_selector")
    if not sel:
        return None
    dlist = row.select(sel)
    # KNUSEMI: span.hit 여러 개 중 "작성일" 포함된 것만
    for d in dlist:
        txt = textnorm(d.get_text())
        if "작성일" in txt:
            date_text = txt.replace("작성일", "").replace(":", "").strip()
            break
    # SEE는 td.date 하나라면 위 루프에서 그대로 date_text가 채워짐
    if not date_text and dlist:
        # fallback: 첫 번째 요소 텍스트
        date_text = textnorm(dlist[0].get_text())
    return date_text

def parse_and_notify(site, state):
    html = fetch(site["url"])
    soup = BeautifulSoup(html, "html.parser")

    items = soup.select(site["list_selector"])
    if not items:
        print(f"[WARN] No items for {site['name']} with selector {site['list_selector']}")
        return 0

    seen = set(state.get(site["name"], []))
    new_ids = []
    new_count = 0

    for row in items[: site.get("max_items", 20)]:
        # 고정공지 스킵
        if should_skip_row(row, site):
            continue

        a = row.select_one(site.get("title_selector", "a"))
        if not a:
            continue
        title = textnorm(a.get_text())

        # 링크
        href_el = row.select_one(site.get("link_selector", "a"))
        href = href_el.get("href") if href_el else ""
        link = urljoin(site.get("base_url", site["url"]), href) if href else None

        # 날짜
        date_text = extract_date(row, site)

        # 중복키
        key = link if (site.get("id_strategy", "link") == "link" and link) else f"{title}|{date_text or ''}"
        if key in seen:
            continue

        if not INIT_MODE:
            discord_post(title, link, site["name"], date_text=date_text)
        else:
            print(f"[INIT_MODE] would notify: {title} | {link}")

        new_ids.append(key)
        new_count += 1

    # 상태 업데이트
    keep = site.get("max_items", 20)
    state[site["name"]] = (list(seen) + new_ids)[-keep:]

    return new_count

def main():
    sites = load_sites()
    state = load_state()
    total_new = 0
    for site in sites:
        try:
            cnt = parse_and_notify(site, state)
            print(f"[{site['name']}] new posts: {cnt}")
            total_new += cnt
        except Exception as e:
            print(f"[ERROR] {site['name']}: {e}", file=sys.stderr)

    save_state(state)

    # GitHub Actions에서 새 글 있으면 상태 커밋
    if os.getenv("GITHUB_ACTIONS", "") and total_new > 0:
        os.system('git config user.name "github-actions[bot]"')
        os.system('git config user.email "41898282+github-actions[bot]@users.noreply.github.com"')
        os.system('git add .state/seen.json')
        os.system('git commit -m "chore: update seen.json ({} new)"'.format(total_new))
        os.system('git push')

if __name__ == "__main__":
    main()
