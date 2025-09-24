from __future__ import annotations
import argparse
import asyncio
import csv
import json
import os
import platform
import re
import sys
import time
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from time import gmtime, strftime
from typing import List

API_URL = "https://random-word-api.herokuapp.com/all"
CHECK_URL = "https://steamcommunity.com/id/{}/"
RESULTS_CSV = "results.csv"
AVAILABLE_TXT = "Available.txt"

ALLOWED_RE = re.compile(r"^[a-z0-9]+$")  # allowed (sanitized) username chars

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def ensure_files():
    if not os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, "w", encoding="utf8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["username", "status", "http_status", "timestamp", "note"])
    open(AVAILABLE_TXT, "a", encoding="utf8").close()
    open("Custom.txt", "a", encoding="utf8").close()  # ensure exists

def load_seen_from_csv(path: str) -> set:
    if not os.path.exists(path):
        return set()
    seen = set()
    try:
        with open(path, "r", encoding="utf8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                uname = (row.get("username") or "").strip()
                if uname:
                    seen.add(uname)
    except Exception:
        # fallback
        with open(path, "r", encoding="utf8") as f:
            for ln in f:
                if ln.strip():
                    first = ln.split(",")[0].strip()
                    if first and first != "username":
                        seen.add(first)
    return seen

class SteamChecker:
    def __init__(self, timeout_seconds: int = 12, retries: int = 2, delay: float = 0.0, concurrency: int = 1):
        self.timeout = ClientTimeout(total=timeout_seconds)
        self.retries = max(0, retries)
        self.delay = max(0.0, float(delay))
        self.concurrency = max(1, int(concurrency))  # default 1 -> sequential by default
        self.checked = 0
        self.available = 0
        self.start_time = None
        self.total_to_check = 0

    async def fetch_full_wordlist(self, session: ClientSession) -> List[str]:
        print(f"[+] Fetching wordlist from {API_URL} ...")
        async with session.get(API_URL) as resp:
            text = await resp.text()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    words = [str(x).strip() for x in parsed if str(x).strip()]
                else:
                    words = []
            except Exception:
                # fallback: splitlines/comma
                if "\n" in text:
                    words = [ln.strip() for ln in text.splitlines() if ln.strip()]
                elif "," in text:
                    words = [p.strip() for p in text.split(",") if p.strip()]
                else:
                    words = [text.strip()] if text.strip() else []
        print(f"[+] Fetched {len(words)} words from API.")
        return words

    async def _check_once(self, session: ClientSession, name: str):
        url = CHECK_URL.format(name)
        try:
            async with session.get(url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                status_code = resp.status
                # redirect -> taken
                if "/profiles/" in final_url:
                    return ("taken", status_code, "redirect_to_profiles")
                # read body
                try:
                    body = await resp.text()
                except Exception:
                    return ("unknown", status_code, "unicode_error")
                low = body.lower()
                if "could not be found" in low and len(name) > 2:
                    return ("available", status_code, "not_found_text")
                markers = ("avatar", "profile_header", "steamcommunity.com", "steamid")
                if any(m in low for m in markers):
                    return ("taken", status_code, "profile_markers")
                return ("taken", status_code, "fallback_taken")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ("unknown", None, f"exception:{type(e).__name__}")

    async def _check_with_retries(self, session: ClientSession, name: str):
        backoff = 0.6
        last = ("unknown", None, "no_attempt")
        for attempt in range(1, self.retries + 2):
            last = await self._check_once(session, name)
            if last and last[0] != "unknown":
                return last
            if attempt <= self.retries:
                await asyncio.sleep(backoff)
                backoff *= 2
        return last

    def write_result(self, username: str, status: str, http_status, note: str):
        ts = now_ts()
        with open(RESULTS_CSV, "a", encoding="utf8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([username, status, http_status if http_status is not None else "", ts, note])
        if status == "available":
            with open(AVAILABLE_TXT, "a", encoding="utf8") as f:
                f.write(username + "\n")

    def _update_title(self):
        elapsed = time.time() - self.start_time
        elapsed_s = strftime("%H:%M:%S", gmtime(elapsed))
        try:
                avg = elapsed / max(1, self.checked)
                remaining = avg * max(0, (self.total_to_check - self.checked))
                remaining_s = strftime("%H:%M:%S", gmtime(remaining))
        except Exception:
            remaining_s = "..."
            print(
                f"[Status] Checked {self.checked}/{self.total_to_check} | Available {self.available} | Elapsed {elapsed_s} | ETA {remaining_s}",
                end="\r",
                flush=True,
    )


    async def worker(self, queue: asyncio.Queue, session: ClientSession, sem: asyncio.Semaphore):
        while True:
            name = await queue.get()
            if name is None:
                queue.task_done()
                break
            async with sem:
                status, http_status, note = await self._check_with_retries(session, name)
            self.checked += 1
            if status == "available":
                self.available += 1
                print(f"[{self.checked}/{self.total_to_check}] AVAILABLE: {name}")
            elif status == "taken":
                print(f"[{self.checked}/{self.total_to_check}] taken: {name}")
            else:
                print(f"[{self.checked}/{self.total_to_check}] unknown/error: {name} ({note})")
            # write (sync file I/O) - ok since mostly I/O bound
            self.write_result(name, status, http_status, note)
            self._update_title()
            if self.delay:
                await asyncio.sleep(self.delay)
            queue.task_done()

    async def run_check_list(self, names: List[str], max_checks: int = 0, resume: bool = True):
        # sanitize and filter
        names = [n.lower().strip() for n in names if n and isinstance(n, str)]
        # drop non-alnum, keep a-z0-9 only
        names = [re.sub(r"[^a-z0-9]", "", n) for n in names]
        names = [n for n in names if n and ALLOWED_RE.match(n) and len(n) > 2]

        # resume: skip names already in results.csv
        if resume:
            seen = load_seen_from_csv(RESULTS_CSV)
            names = [n for n in names if n not in seen]

        if max_checks > 0:
            names = names[:max_checks]

        self.total_to_check = len(names)
        if self.total_to_check == 0:
            print("[!] No names to check (after filtering/resume).")
            return

        print(f"[+] {self.total_to_check} names queued for checking. concurrency={self.concurrency}, delay={self.delay}s")
        self.start_time = time.time()

        headers = {"User-Agent": "Mozilla/5.0 (compatible; steam-random-checker/1.0)"}
        connector = TCPConnector(limit_per_host=min(self.concurrency, 100), ttl_dns_cache=300)

        # sequential/simple mode (concurrency==1) or concurrent mode
        if self.concurrency == 1:
            async with ClientSession(timeout=self.timeout, connector=connector, headers=headers) as session:
                for name in names:
                    status, http_status, note = await self._check_with_retries(session, name)
                    self.checked += 1
                    if status == "available":
                        self.available += 1
                        print(f"[{self.checked}/{self.total_to_check}] AVAILABLE: {name}")
                    elif status == "taken":
                        print(f"[{self.checked}/{self.total_to_check}] taken: {name}")
                    else:
                        print(f"[{self.checked}/{self.total_to_check}] unknown/error: {name} ({note})")
                    self.write_result(name, status, http_status, note)
                    self._update_title()
                    if self.delay:
                        await asyncio.sleep(self.delay)
        else:
            q = asyncio.Queue()
            for n in names:
                q.put_nowait(n)
            # stop sentinels
            for _ in range(self.concurrency):
                q.put_nowait(None)
            sem = asyncio.Semaphore(self.concurrency)
            async with ClientSession(timeout=self.timeout, connector=connector, headers=headers) as session:
                workers = [asyncio.create_task(self.worker(q, session, sem)) for _ in range(self.concurrency)]
                await q.join()
                for w in workers:
                    w.cancel()

async def run_from_custom_file(max_checks: int = 0, concurrency: int = 1, delay: float = 0.0, resume: bool = True):
    if not os.path.exists("Custom.txt"):
        open("Custom.txt", "w", encoding="utf8").close()
        print("[!] Created Custom.txt - put one username per line and re-run.")
        return
    with open("Custom.txt", "r", encoding="utf8") as f:
        raw = [ln.strip() for ln in f if ln.strip()]
    if not raw:
        print("[!] No names in Custom.txt.")
        return
    checker = SteamChecker(delay=delay, concurrency=concurrency)
    await checker.run_check_list(raw, max_checks=max_checks, resume=resume)

async def run_from_api(max_checks: int = 0, concurrency: int = 1, delay: float = 0.0, resume: bool = True):
    checker = SteamChecker(delay=delay, concurrency=concurrency)
    connector = TCPConnector(limit_per_host=4)
    async with ClientSession(timeout=ClientTimeout(total=30), connector=connector) as session:
        try:
            words = await checker.fetch_full_wordlist(session)
        except Exception as e:
            print(f"[!] Failed to fetch wordlist: {e}")
            return
    # optional shuffle - uncomment if you want random order
    # import random; random.shuffle(words)
    await checker.run_check_list(words, max_checks=max_checks, resume=resume)

def main_menu():
    ensure_files()
    print()
    print("[1] Check from Custom.txt")
    print("[2] Fetch from random-word-api/all and check")
    print()
    try:
        opt = input("[>] Select an option: ").strip()
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        return

    # fixed defaults
    max_checks = 0       # always check all
    concurrency = 1      # sequential
    delay = 0.0          # no delay
    resume = True        # always skip already-seen

    if opt == "1":
        asyncio.run(run_from_custom_file(
            max_checks=max_checks,
            concurrency=concurrency,
            delay=delay,
            resume=resume
        ))
    elif opt == "2":
        asyncio.run(run_from_api(
            max_checks=max_checks,
            concurrency=concurrency,
            delay=delay,
            resume=resume
        ))
    else:
        print("[!] Invalid option.")


if __name__ == "__main__":
    main_menu()
