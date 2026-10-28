#!/usr/bin/env python3
"""WorkBuddy 每日签到（Buddy 加油站）— 无人值守运行器。

只使用 Python 标准库，无第三方依赖。
设计要点：
  * 幂等：服务端对已签到返回 code=10001，重复调用不会多领也不会扣分。
  * 自愈：域名/路径可通过环境变量覆盖，无需重新构建镜像。
  * 静默失败可见：每次结果写入 /data/state.json，并由 HEALTHCHECK 反映。
  * 凭据安全：令牌只从环境变量读取，任何日志都不会输出它。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
HTTP_TIMEOUT = 20

TZ = ZoneInfo(os.environ.get("TZ_NAME") or os.environ.get("TZ") or "Asia/Shanghai")
DATA_DIR = os.environ.get("DATA_DIR") or "/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "last_success")

# 官方口径（2026-10 实测可用）。若上游改版，改这两个环境变量即可，不必重建镜像。
CLAIM_PATH = os.environ.get("WB_CLAIM_PATH") or "/v2/billing/meter/daily-checkin"

# 网页端有可能给 cookie 而不是 bearer，两种都收。
COOKIE = (os.environ.get("WB_COOKIE") or "").strip()
BASE_URL_OVERRIDE = (os.environ.get("WB_BASE_URL") or "").strip().rstrip("/")


def _normalize_token(raw: str) -> str:
    """容错：粘贴时带了 "Bearer " 前缀、引号或换行都要能认。"""
    v = (raw or "").strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
    for prefix in ("Bearer ", "bearer ", "BEARER "):
        if v.startswith(prefix):
            v = v[len(prefix):].strip()
            break
    return v


TOKEN = _normalize_token(os.environ.get("WB_TOKEN") or "")

CLAIM_AT = os.environ.get("CLAIM_AT") or "08:00"
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in {"1", "true", "yes", "on"}
RETRIES = int(os.environ.get("RETRIES") or "4")
JITTER_SECONDS = int(os.environ.get("JITTER_SECONDS") or "0")

# 登录态里的 domain 是 www.workbuddy.cn，公开教程多用 www.codebuddy.cn。
# 两个都是官方域名，按顺序探测，命中即缓存。
BASE_CANDIDATES = ["https://www.codebuddy.cn", "https://www.workbuddy.cn"]

CODE_OK = 0
CODE_ALREADY = 10001


class AuthError(Exception):
    """令牌无效或过期，重试无意义。"""


def log(event: str, **fields) -> None:
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    tail = " | " + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    print(f"[{ts}] {event}{tail}", flush=True)


def parse_claim_at(value: str) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:
        pass
    log("CONFIG_WARN", claim_at=value, fallback="08:00")
    return 8, 0


def bases() -> list[str]:
    if BASE_URL_OVERRIDE:
        return [BASE_URL_OVERRIDE]
    cached = read_state().get("working_base")
    if cached:
        return [cached] + [b for b in BASE_CANDIDATES if b != cached]
    return list(BASE_CANDIDATES)


def read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # 状态写不进去不应中断签到本身
        log("STATE_WRITE_FAILED", error=type(exc).__name__)


def post_json(base: str, path: str) -> dict:
    url = base + path
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if COOKIE:
        req.add_header("Cookie", COOKIE)
    req.add_header("User-Agent", f"workbuddy-claim/{VERSION}")
    # 与网页端保持同源特征，避免网关按来源拦截
    req.add_header("Origin", base)
    req.add_header("Referer", base + "/profile/plan")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(f"HTTP {exc.code}（网关拒绝，令牌无效或已过期）") from exc
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network {exc.reason}") from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"响应不是 JSON: {raw[:160]!r}") from exc


def collect(obj, keys: set[str], depth: int = 0) -> dict:
    """在嵌套响应里找业务字段（官方响应存在 data.data 双层结构）。"""
    found: dict = {}
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and key not in found:
                found[key] = value
            if isinstance(value, (dict, list)):
                found.update({k: v for k, v in collect(value, keys, depth + 1).items() if k not in found})
    elif isinstance(obj, list):
        for item in obj:
            found.update({k: v for k, v in collect(item, keys, depth + 1).items() if k not in found})
    return found


FIELDS = {
    "today_checked_in",
    "streak_days",
    "total_credits",
    "daily_credit",
    "today_credit",
    "season",
    "end_time",
}


def attempt(base: str) -> str:
    """返回 ok / already / auth。异常向上抛。"""
    if DRY_RUN:
        log("DRY_RUN", would_post=base + CLAIM_PATH)
        return "ok"

    payload = post_json(base, CLAIM_PATH)
    code = payload.get("code")
    msg = payload.get("msg") or payload.get("message")
    info = collect(payload, FIELDS)

    if code == CODE_OK:
        log(
            "CHECKIN_OK",
            base=base,
            msg=msg,
            today_credit=info.get("today_credit") or info.get("daily_credit"),
            total_credits=info.get("total_credits"),
            streak_days=info.get("streak_days"),
            season=info.get("season"),
            season_end=info.get("end_time"),
        )
        return "ok"
    if code == CODE_ALREADY:
        log(
            "ALREADY_CHECKED_IN",
            base=base,
            msg=msg,
            total_credits=info.get("total_credits"),
            streak_days=info.get("streak_days"),
        )
        return "already"

    # 未知业务码：当作失败处理，但保留原始响应便于排查
    raise RuntimeError(f"unexpected code={code} msg={msg}")


def run_claim() -> int:
    """执行一次领取。返回进程退出码语义：0 成功/已签，2 鉴权失败。"""
    if not TOKEN and not COOKIE:
        log("CONFIG_ERROR", hint="未设置 WB_TOKEN（或 WB_COOKIE）")
        return 2

    last_error = None
    for base in bases():
        for i in range(1, RETRIES + 1):
            try:
                outcome = attempt(base)
                state = read_state()
                state.update(
                    {
                        "working_base": base,
                        "last_attempt": datetime.now(TZ).isoformat(timespec="seconds"),
                        "last_outcome": outcome,
                        "version": VERSION,
                    }
                )
                today = datetime.now(TZ).strftime("%Y-%m-%d")
                state["last_success_date"] = today
                state["last_success_at"] = datetime.now(TZ).isoformat(timespec="seconds")
                state["last_error"] = None
                history = state.get("history") or []
                history.append({"date": today, "outcome": outcome, "at": state["last_success_at"]})
                state["history"] = history[-90:]
                write_state(state)
                try:
                    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as fh:
                        fh.write(state["last_success_at"])
                except Exception:
                    pass
                return 0
            except AuthError as exc:
                log(
                    "AUTH_FAILED",
                    base=base,
                    detail=str(exc),
                    hint="令牌已失效：打开 https://www.codebuddy.cn/profile/plan ，F12 → Network → 任一 checkin 请求的 Authorization 或 Cookie 头",
                )
                state = read_state()
                state.update(
                    {
                        "last_attempt": datetime.now(TZ).isoformat(timespec="seconds"),
                        "last_outcome": "auth_failed",
                        "last_error": str(exc),
                    }
                )
                write_state(state)
                return 2
            except Exception as exc:
                last_error = exc
                log("ATTEMPT_FAILED", base=base, try_no=i, error=f"{type(exc).__name__}: {exc}")
                if i < RETRIES:
                    time.sleep(min(5 * (3 ** (i - 1)), 60))

    state = read_state()
    state.update(
        {
            "last_attempt": datetime.now(TZ).isoformat(timespec="seconds"),
            "last_outcome": "failed",
            "last_error": f"{type(last_error).__name__}: {last_error}",
        }
    )
    write_state(state)
    log("CHECKIN_FAILED", error=str(last_error))
    return 1


def claimed_today() -> bool:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    return read_state().get("last_success_date") == today


def next_run_at(now: datetime) -> datetime:
    hh, mm = parse_claim_at(CLAIM_AT)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    if JITTER_SECONDS > 0:
        target += timedelta(seconds=int.from_bytes(os.urandom(1), "big") % JITTER_SECONDS)
    return target


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(TZ)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 300))


def main_loop() -> int:
    log(
        "START",
        version=VERSION,
        tz=str(TZ),
        claim_at=CLAIM_AT,
        base=BASE_URL_OVERRIDE or "auto",
        dry_run=DRY_RUN,
        token_present=bool(TOKEN),
        cookie_present=bool(COOKIE),
    )
    if claimed_today():
        log("SKIP_TODAY", reason="今日已成功领取")
    else:
        log("CATCH_UP", reason="启动补签")
        run_claim()

    while True:
        target = next_run_at(datetime.now(TZ))
        log("NEXT_RUN", at=target.isoformat(timespec="seconds"))
        sleep_until(target)
        try:
            run_claim()
        except Exception as exc:  # 任何异常都不应让容器退出
            log("CYCLE_ERROR", error=f"{type(exc).__name__}: {exc}")
            time.sleep(600)


def main(argv: list[str]) -> int:
    if "--once" in argv:
        return run_claim()
    if "--health" in argv:
        try:
            age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
        except OSError:
            return 1
        return 0 if age < 36 * 3600 else 1
    if "--print-config" in argv:
        log(
            "CONFIG",
            version=VERSION,
            tz=str(TZ),
            claim_at=CLAIM_AT,
            claim_path=CLAIM_PATH,
            base=BASE_URL_OVERRIDE or "auto",
            data_dir=DATA_DIR,
            dry_run=DRY_RUN,
            token_present=bool(TOKEN),
            token_length=len(TOKEN) or None,
            cookie_present=bool(COOKIE),
        )
        return 0
    return main_loop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
