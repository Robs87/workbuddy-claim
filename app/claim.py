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
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION = "1.2.1"
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

# CookieCloud 凭据源：每次运行都去拉一次最新的浏览器登录态，令牌因此能自动续期。
CC_URL = (os.environ.get("CC_URL") or "http://192.168.31.190:8089").strip().rstrip("/")
CC_UUID = (os.environ.get("CC_UUID") or "").strip()
CC_PASSWORD = os.environ.get("CC_PASSWORD") or ""
CC_DOMAIN = (os.environ.get("CC_DOMAIN") or "codebuddy.cn").strip().lstrip(".")


def _normalize_token(raw: str) -> str:
    """容错：粘贴时带了 "Bearer " 前缀、引号或换行都要能认。"""
    v = (raw or "").strip().strip('"').strip("'").replace("\n", "").replace("\r", "")
    for prefix in ("Bearer ", "bearer ", "BEARER "):
        if v.startswith(prefix):
            v = v[len(prefix):].strip()
            break
    return v


def _token_problem(v: str) -> str | None:
    """粗筛明显不是令牌的输入。

    2026-10-28 实况：有人把 README 里取令牌用的 JS 钩子整段贴进了 Access Token，
    结果 http.client 抛 `UnicodeEncodeError: latin-1`（中文注释无法进 header），
    报错指向太偏。这里提前把人话讲清楚。
    """
    if not v:
        return None
    for marker in ("console.log", "function", "=>", "()=>", "();"):
        if marker in v:
            return "看起来是 JS 代码而不是令牌"
    if "\n" in v or "\r" in v:
        return "含换行"
    if not v.isascii():
        bad = next(ch for ch in v if not ch.isascii())
        return f"含非 ASCII 字符 {bad!r}"
    if " " in v or "\t" in v:
        return "中间含空格"
    if len(v) < 40:
        return f"长度只有 {len(v)}，正常令牌超过 100 字符"
    return None


TOKEN = _normalize_token(os.environ.get("WB_TOKEN") or "")
TOKEN_PROBLEM = _token_problem(TOKEN)

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


def cookiecloud_fetch() -> dict | None:
    """从 CookieCloud 拉取已解密的浏览器数据。

    该服务端在 POST body 里带 password 时会直接返回明文，
    因此容器侧不需要内置 AES 实现。
    """
    if not (CC_UUID and CC_PASSWORD):
        log("CC_SKIP", reason="未配置 CC_UUID / CC_PASSWORD")
        return None
    url = f"{CC_URL}/get/{CC_UUID}"
    body = json.dumps({"password": CC_PASSWORD}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            log("CC_FAIL", reason="返回结构异常")
            return None
        return payload
    except urllib.error.HTTPError as exc:
        hint = "密码错误或 CookieCloud 拒绝了解密" if exc.code >= 500 else "端点返回错误"
        log("CC_FAIL", status=exc.code, hint=hint)
        return None
    except Exception as exc:
        log("CC_FAIL", error=f"{type(exc).__name__}: {exc}")
        return None


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
TOKEN_KEY_RE = re.compile(r"(access[_-]?token|id[_-]?token|^token$|authorization|jwt)", re.I)


def domain_match(host: str, want: str) -> bool:
    """同时接受 'www.codebuddy.cn'、'.codebuddy.cn'、'https://www.codebuddy.cn/path' 三种写法。"""
    h = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", (host or "").strip().lower())
    h = h.split("/")[0].split(":")[0].lstrip(".")
    return bool(h) and (h == want or h.endswith("." + want))


def find_token(obj, depth: int = 0) -> str | None:
    """在 localStorage 快照里找形似令牌的值。优先 JWT 形态。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                m = JWT_RE.search(value)
                if m:
                    return m.group(0)
                if TOKEN_KEY_RE.search(str(key)) and 20 <= len(value) <= 4096 and " " not in value:
                    return value
            else:
                found = find_token(value, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_token(item, depth + 1)
            if found:
                return found
    elif isinstance(obj, str):
        m = JWT_RE.search(obj)
        if m:
            return m.group(0)
    return None


def collect_cookies(payload: dict) -> dict:
    """按 CC_DOMAIN 过滤出未过期 cookie，返回 {host: [cookie,...]}。"""
    now = time.time()
    out: dict[str, list] = {}
    data = payload.get("cookie_data") or {}
    if isinstance(data, list):  # 少数版本是扁平列表
        data = {"_": data}
    for host, items in data.items():
        if not domain_match(str(host), CC_DOMAIN):
            continue
        keep = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            exp = item.get("expirationDate")
            if isinstance(exp, (int, float)) and exp > 0 and exp < now:
                continue
            if not item.get("name"):
                continue
            keep.append(item)
        if keep:
            out[str(host)] = keep
    return out


def credentials_from_cookiecloud() -> dict | None:
    payload = cookiecloud_fetch()
    if payload is None:
        return None

    cookies = collect_cookies(payload)
    storage = payload.get("local_storage_data") or {}
    token = None
    token_host = None
    if isinstance(storage, dict):
        for host, values in storage.items():
            if domain_match(str(host), CC_DOMAIN):
                token = find_token(values)
                if token:
                    token_host = str(host)
                    break
        if not token and len(storage) == 1:
            # 存储键可能不是域名（例如完整 URL 或站点 ID），单一主机时允许兜底
            only_host, only_values = next(iter(storage.items()))
            token = find_token(only_values)
            if token:
                token_host = f"{only_host}(唯一主机兜底)"

    cookie_host = None
    cookie_header = None
    if cookies:
        # 优先用最贴近 API 的主机
        cookie_host = sorted(cookies, key=len, reverse=True)[0]
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies[cookie_host])

    log(
        "CC_OK",
        hosts=",".join(sorted(cookies)) or None,
        cookie_count=sum(len(v) for v in cookies.values()) or 0,
        token_present=bool(token),
        token_host=token_host,
        cookie_host=cookie_host,
    )
    if not token and not cookie_header:
        log("CC_EMPTY", reason=f"没有匹配 {CC_DOMAIN} 的登录态，浏览器需先访问一次该站点并同步")
        return None
    return {"source": "cookiecloud", "token": token, "cookie": cookie_header}


def env_credentials() -> dict | None:
    if not TOKEN and not COOKIE:
        return None
    return {"source": "env", "token": TOKEN or None, "cookie": COOKIE or None}


def post_json(base: str, path: str, cred: dict) -> dict:
    url = base + path
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if cred.get("token"):
        req.add_header("Authorization", f"Bearer {cred['token']}")
    if cred.get("cookie"):
        req.add_header("Cookie", cred["cookie"])
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


def attempt(base: str, cred: dict) -> str:
    """返回 ok / already / auth。异常向上抛。"""
    if DRY_RUN:
        log("DRY_RUN", would_post=base + CLAIM_PATH, source=cred.get("source"))
        return "ok"

    payload = post_json(base, CLAIM_PATH, cred)
    code = payload.get("code")
    msg = payload.get("msg") or payload.get("message")
    info = collect(payload, FIELDS)

    if code == CODE_OK:
        log(
            "CHECKIN_OK",
            base=base,
            source=cred.get("source"),
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


def resolve_credentials() -> list[dict]:
    """凭据源优先级：手填 WB_TOKEN → CookieCloud。

    2026-10-28 取证结论：签到网关只接受 `Authorization: Bearer`，浏览器 cookie
    （Keycloak 会话）在任何情况下都返回同一个 nginx 401；且浏览器把令牌存成
    `v2:` 信封密文，密钥为 IndexedDB 内不可导出，任何同步机制都取不到。
    因此 CookieCloud 只保留为兜底，有令牌时连探测请求都不发，避免每天先撞一次 401。
    """
    env = env_credentials()
    if env:
        return [env]
    cc = credentials_from_cookiecloud()
    if cc:
        log("CC_FALLBACK", note="无 WB_TOKEN，改用 CookieCloud；此路径已被证明无法通过签到鉴权，仅作诊断")
        return [cc]
    return []


def _mark(state_key: str, **fields) -> None:
    state = read_state()
    state.update({"last_attempt": datetime.now(TZ).isoformat(timespec="seconds"), **fields})
    write_state(state)


def run_claim() -> int:
    """执行一次领取。返回进程退出码语义：0 成功/已签，2 鉴权失败，1 其它失败。"""
    if TOKEN_PROBLEM:
        log(
            "TOKEN_INVALID",
            reason=TOKEN_PROBLEM,
            hint="Access Token 该填 eyJ 开头的长串（带不带 Bearer 都行），不是取令牌用的 JS 脚本；见 README 第一节",
        )
        _mark("last_attempt", last_outcome="config_error", last_error=f"WB_TOKEN 无效: {TOKEN_PROBLEM}")
        return 2

    creds = resolve_credentials()
    if not creds:
        log("CONFIG_ERROR", hint="缺少 WB_TOKEN：在 Docker → workbuddy-claim → Edit → Access Token 里粘贴登录令牌")
        return 2

    last_error = None
    rejected: list[str] = []
    for base in bases():
        for cred in creds:
            source = cred.get("source")
            for i in range(1, RETRIES + 1):
                try:
                    outcome = attempt(base, cred)
                    now_iso = datetime.now(TZ).isoformat(timespec="seconds")
                    today = datetime.now(TZ).strftime("%Y-%m-%d")
                    state = read_state()
                    state.update(
                        {
                            "working_base": base,
                            "working_source": source,
                            "last_attempt": now_iso,
                            "last_outcome": outcome,
                            "last_success_date": today,
                            "last_success_at": now_iso,
                            "last_error": None,
                            "version": VERSION,
                        }
                    )
                    history = state.get("history") or []
                    history.append({"date": today, "outcome": outcome, "source": source, "at": now_iso})
                    state["history"] = history[-90:]
                    write_state(state)
                    try:
                        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as fh:
                            fh.write(now_iso)
                    except Exception:
                        pass
                    return 0
                except AuthError as exc:
                    rejected.append(f"{source}@{base}")
                    log(
                        "AUTH_FAILED",
                        base=base,
                        source=source,
                        detail=str(exc),
                        hint="该凭据被服务端拒绝，换下一个来源",
                    )
                    break  # 同一凭据重试无意义，直接换源
                except Exception as exc:
                    last_error = exc
                    log("ATTEMPT_FAILED", base=base, source=source, try_no=i, error=f"{type(exc).__name__}: {exc}")
                    if i < RETRIES:
                        time.sleep(min(5 * (3 ** (i - 1)), 60))

    if rejected and last_error is None:
        _mark("auth_failed", last_outcome="auth_failed", last_error=f"全部凭据被拒: {', '.join(rejected)}")
        log(
            "AUTH_FINAL",
            rejected=",".join(rejected),
            hint="令牌失效或被拒：打开 https://www.codebuddy.cn/profile/plan ，F12 → Console 跑 README 第一节的钩子取 Bearer，填进 Docker → Edit → Access Token",
        )
        return 2

    _mark("failed", last_outcome="failed", last_error=f"{type(last_error).__name__}: {last_error}")
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
        token_problem=TOKEN_PROBLEM,
        cookie_present=bool(COOKIE),
        cookiecloud=bool(CC_UUID and CC_PASSWORD),
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


def probe_cookiecloud() -> int:
    """诊断模式：只打印结构与键名，绝不输出凭据值。"""
    payload = cookiecloud_fetch()
    if payload is None:
        return 1
    print("== CookieCloud 结构摘要（不含任何凭据值）==")
    print("顶层键:", ", ".join(sorted(payload.keys())))

    cd = payload.get("cookie_data") or {}
    if isinstance(cd, dict):
        for host in sorted(cd):
            items = cd[host] or []
            names = sorted({str(i.get("name")) for i in items if isinstance(i, dict)})
            print(f"  cookie  {host}: {len(items)} 条 -> {','.join(names)[:140]}")

    ls = payload.get("local_storage_data") or {}
    if isinstance(ls, dict):
        for host in sorted(ls):
            kv = ls[host]
            if not isinstance(kv, dict):
                print(f"  storage {host}: 非字典({type(kv).__name__})")
                continue
            marks = []
            for k, v in kv.items():
                sv = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                marks.append(f"{k}(len={len(sv)}{',JWT' if JWT_RE.search(sv) else ''})")
            print(f"  storage {host}: {', '.join(marks)[:200]}")

    matched = collect_cookies(payload)
    print(f"匹配 {CC_DOMAIN} 的 cookie 域:", ", ".join(sorted(matched)) or "无")
    print("可提取令牌:", "是" if any(find_token(v) for v in ls.values()) else "否")
    return 0


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
            token_problem=TOKEN_PROBLEM,
            cookie_present=bool(COOKIE),
            cc_url=CC_URL,
            cc_uuid=bool(CC_UUID),
            cc_password=bool(CC_PASSWORD),
            cc_domain=CC_DOMAIN,
        )
        return 0
    if "--cc-probe" in argv:
        return probe_cookiecloud()
    return main_loop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
