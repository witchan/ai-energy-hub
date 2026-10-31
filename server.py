#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理站后端 - Apple 礼品卡（修复版）
"""

import json, time, threading, urllib.request, urllib.parse, sqlite3, os, re, uuid, secrets, hashlib, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from contextlib import contextmanager
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import socket
import qrcode
import qrcode.image.svg

# ===== 配置 =====
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://likayi.com/api/v1").rstrip("/")
# 礼品卡与 AI 代充共用同一上游账户、钱包及支付密码。
UPSTREAM_USER = os.environ.get("UPSTREAM_USER", "")
UPSTREAM_PASS = os.environ.get("UPSTREAM_PASS", "")
UPSTREAM_PAY_PASS = os.environ.get("UPSTREAM_PAY_PASS", "")
ENABLE_GIFTCARD = os.environ.get("ENABLE_GIFTCARD", "1") == "1"
ENABLE_AI = os.environ.get("ENABLE_AI", "1") == "1"
# Percentage points: 3 adds 3%; -3 discounts 3%, without a separate opt-in.
PROFIT = Decimal(os.environ.get("PROFIT", "3"))
AI_PROFIT = Decimal(os.environ.get("AI_PROFIT", "3"))
DB_FILE = os.environ.get("DB_FILE", "shop.db")
PORT = int(os.environ.get("PORT", "9006"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
WORKER_CONCURRENCY = 5
WORKER_LEASE_SECONDS = 600
PAYMENT_CHECK_LEASE_SECONDS = 180
WORKER_SLOTS = threading.BoundedSemaphore(WORKER_CONCURRENCY)
MAX_BODY = 65536
# Session JSON can exceed the old 32 KiB character limit. Bound the credential
# in UTF-8 bytes and allow room for JSON escaping in the two AI request bodies.
MAX_AI_CREDENTIAL_BYTES = 256 * 1024
MAX_AI_BODY = 2 * 1024 * 1024
SECRET_TTL = 900
ASSET_DIR = Path(__file__).resolve().parent

def validate_config():
    if not (ENABLE_GIFTCARD or ENABLE_AI):
        raise RuntimeError("至少启用一种业务")
    required = ("UPSTREAM_USER", "UPSTREAM_PASS", "UPSTREAM_PAY_PASS")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("缺少必需环境变量: " + ", ".join(missing))
    for value in (PROFIT, AI_PROFIT):
        if not Decimal(str(value)).is_finite():
            raise RuntimeError("价格配置必须为有限数值")
        if value <= -100:
            raise RuntimeError("加价百分比必须大于 -100，优惠不能达到或超过 100%")

# ===== 全局状态 =====
upstream_token = None
token_expire = 0
token_lock = threading.Lock()
ORDER_SECRETS = {}
ORDER_SECRETS_LOCK = threading.Lock()
COUNTRY_RATES_TTL = 300
COUNTRY_RATES_RETRY = 30
country_rates_lock = threading.Lock()
country_rates_cache = {}
country_rates_expires = 0.0
country_rates_retry_after = 0.0


# ===== 数据库 =====
def init_db():
    Path(DB_FILE).resolve().parent.mkdir(parents=True, exist_ok=True)
    with database() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        # Migrate atomically; never delete or reassign conflicting old payments.
        conn.execute("BEGIN IMMEDIATE")
        migrate_orders(conn)


def migrate_orders(conn):
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE,
        order_type TEXT DEFAULT 'gift_card',
        recharge_id INTEGER,
        upstream_order_id INTEGER,
        product_id INTEGER,
        product_name TEXT,
        country TEXT,
        amount REAL,
        quantity INTEGER,
        channel_level TEXT,
        cost_price REAL,
        sell_price REAL,
        status INTEGER,
        status_text TEXT,
        progress INTEGER DEFAULT 0,
        recharged_amount REAL DEFAULT 0,
        need_2fa INTEGER DEFAULT 0,
        fail_reason TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    columns = {row[1] for row in c.execute("PRAGMA table_info(orders)")}
    if "access_token_hash" not in columns:
        c.execute("ALTER TABLE orders ADD COLUMN access_token_hash TEXT")
    if "ai_provider_code" not in columns:
        c.execute("ALTER TABLE orders ADD COLUMN ai_provider_code TEXT DEFAULT 'chatgpt'")
    additions = {
        "recharge_id": "INTEGER",
        "recharge_no": "TEXT",
        "fulfillment_state": "TEXT NOT NULL DEFAULT 'pending'",
        "payment_verified": "INTEGER NOT NULL DEFAULT 0",
        "credential_state": "TEXT NOT NULL DEFAULT 'pending'",
        "credential_attempt_id": "TEXT",
        "twofa_state": "TEXT NOT NULL DEFAULT 'idle'",
        "twofa_attempt_id": "TEXT",
        "payment_check_id": "TEXT",
        "payment_check_until": "REAL NOT NULL DEFAULT 0",
        "worker_lease_id": "TEXT",
        "worker_lease_until": "REAL NOT NULL DEFAULT 0",
        "dispatch_started_at": "REAL",
        "qr_payload": "TEXT",
        "next_check": "REAL NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in columns:
            c.execute(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")
    if "fulfillment_state" not in columns:
        # Legacy failures might already have dispatched an upstream request.
        c.execute("UPDATE orders SET fulfillment_state='review', status_text='旧订单待人工核对' WHERE COALESCE(upstream_order_id,0)=0 AND status NOT IN (0,5)")
        c.execute("UPDATE orders SET fulfillment_state='created', payment_verified=1, credential_state=CASE WHEN status=1 THEN 'pending' ELSE 'submitted' END WHERE upstream_order_id>0")
    c.execute("""CREATE TABLE IF NOT EXISTS checkout_requests (
        key_hash TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('creating','completed','review')),
        order_id INTEGER,
        created_at REAL NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_order_work ON orders(fulfillment_state, next_check)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_worker_lease ON orders(worker_lease_until)")
    if c.execute("SELECT recharge_id FROM orders WHERE recharge_id>0 GROUP BY recharge_id HAVING COUNT(*)>1 LIMIT 1").fetchone():
        raise RuntimeError("历史订单存在重复充值单绑定，请核对后再启动；原订单记录保持不变")
    # Both businesses share one upstream wallet. Keep the binding for the whole
    # order lifetime, including cancelled/expired/failed/completed orders.
    # NULL/0 legacy placeholders are not valid upstream recharge identities.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_recharge_unique ON orders(recharge_id) WHERE recharge_id>0")
    # Optional reconciliation reference; payment ownership is still recharge_id.
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_recharge_no ON orders(recharge_no) WHERE recharge_no IS NOT NULL")
    # 兼容旧数据库，并清除旧版本曾落盘的账号凭据。
    sensitive_columns = columns & {"account", "password", "ai_token"}
    if sensitive_columns:
        assignments = ", ".join(f"{name}=''" for name in sensitive_columns)
        conditions = " OR ".join(f"{name}!=''" for name in sensitive_columns)
        c.execute(f"UPDATE orders SET {assignments} WHERE {conditions}")


# ===== 输入校验 =====
def public_order(row):
    """Exclude credentials and session tokens from public order responses."""
    order = dict(row)
    for key in ("account", "password", "ai_token", "access_token_hash", "recharge_id", "recharge_no", "qr_payload", "cost_price", "dispatch_started_at", "next_check", "credential_attempt_id", "twofa_attempt_id", "payment_check_id", "payment_check_until", "worker_lease_id", "worker_lease_until"):
        order.pop(key, None)
    order.update(order_flags(dict(row)))
    return order


def new_order_access_token():
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def order_access_allowed(order, token):
    expected = order.get("access_token_hash")
    if not expected or not isinstance(token, str):
        return False
    actual = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(expected, actual)


def parse_amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 < amount <= 100000:
        return None
    return amount


def is_valid_email(email):
    return (
        re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email) is not None
    )


def validate_ai_credential(token, provider):
    if not isinstance(token, str):
        raise ValueError("账号信息必须为字符串")
    if not token.strip():
        raise ValueError("请粘贴账号信息")
    try:
        size = len(token.encode("utf-8"))
    except UnicodeError:
        raise ValueError("账号信息包含无效字符，请重新复制完整 JSON") from None
    if size > MAX_AI_CREDENTIAL_BYTES:
        raise ValueError("账号信息过长，完整 JSON 不能超过 256 KiB")
    if provider != "claude":
        try:
            if not isinstance(json.loads(token), dict):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise ValueError("Session 必须是完整 JSON 对象")
        return token
    try:
        data = json.loads(token)
        first = data["organization_id"].strip().lower()
        second = data["organization_id_confirm"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", first) or not uuid.UUID(first).int or first != second:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("请输入两次一致且有效的 Organization ID")
    return json.dumps({"organization_id": first, "organization_id_confirm": second})


# ===== 上游 API =====
def upstream_api(path, method="GET", body=None, auth=True, timeout=None):
    global upstream_token, token_expire
    url = UPSTREAM_BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        with token_lock:
            if not upstream_token or time.time() > token_expire - 60:
                login()
            request_token = upstream_token
        req.add_header("Authorization", f"Bearer {request_token}")
    try:
        request_timeout = timeout if timeout is not None else (120 if path.endswith('/submit-ai-token') else 60)
        with urllib.request.urlopen(req, timeout=request_timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401 and auth:
            # Refresh on the NEXT request, never replay a charge-producing POST.
            with token_lock:
                # A delayed 401 must not discard a newer shared session.
                if upstream_token == request_token:
                    upstream_token, token_expire = None, 0
        print(f"上游 API HTTP 错误: {e.code}")
        try:
            result = json.loads(e.read().decode())
            if not isinstance(result, dict):
                raise ValueError("invalid error response")
        except Exception:
            result = {"code": -1, "msg": "上游响应异常", "ambiguous": True}
        # A valid JSON body does not prove a failed write was never executed.
        # Never replay the request here or reopen submission after a 5xx.
        if 500 <= e.code <= 599:
            result["code"] = e.code
            result["ambiguous"] = True
        elif result.get("code") == 0:
            result["code"] = e.code
        return result
    except Exception as e:
        print(f"上游请求异常: {type(e).__name__}")
        return {"code": -1, "msg": "上游暂时未响应", "ambiguous": True}


def login():
    global upstream_token, token_expire
    d = upstream_api(
        "/auth/login",
        "POST",
        {"username": UPSTREAM_USER, "password": UPSTREAM_PASS},
        auth=False,
    )
    if d.get("code") == 0:
        upstream_token = d["data"]["token"]
        token_expire = time.time() + 23 * 3600
        print("上游账户登录成功（礼品卡与 AI 代充共用）")
    else:
        raise RuntimeError("上游账户登录失败")


def get_products():
    d = upstream_api("/brands/apple/products")
    if d.get("code") == 0:
        return d["data"]["list"]
    return []


def get_product_detail(pid):
    d = upstream_api(f"/products/{pid}")
    if d.get("code") == 0:
        return d["data"]
    return None


def giftcard_visible(product):
    """Hide Turkey from the storefront without changing existing orders or pricing."""
    fields = ('name', 'country', 'country_name', 'country_code', 'group_tab')
    text = ' '.join(str(product.get(key) or '') for key in fields).casefold()
    if re.search(r'土耳其|🇹🇷|\b(?:turkey|türkiye|turkiye)\b', text):
        return False
    return not any(str(product.get(key) or '').strip().upper() == 'TR'
                   for key in ('country', 'country_name', 'country_code', 'group_tab'))


def reference_rate(value):
    """Only accept a finite, positive reference rate; no procurement-rate fallback."""
    try:
        rate = Decimal(str(value))
        return rate if rate.is_finite() and 0 < rate <= 10000000 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def get_country_rates():
    """Cache the public reference feed; expired/failed data must not masquerade as current."""
    global country_rates_cache, country_rates_expires, country_rates_retry_after
    with country_rates_lock:
        now = time.monotonic()
        if now < country_rates_expires:
            return dict(country_rates_cache)
        if now < country_rates_retry_after:
            return {}
        try:
            result = upstream_api('/site/country-rates', auth=False, timeout=5)
            if result.get('code') != 0:
                raise ValueError('rate feed unavailable')
            rows = result.get('data', {}).get('rates')
            if not isinstance(rows, list):
                raise ValueError('invalid rate feed')
            rates, conflicts = {}, set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                country, rate = row.get('country'), reference_rate(row.get('rate'))
                if not isinstance(country, str) or not country.strip() or len(country) > 80 or rate is None:
                    continue
                country = country.strip()
                if country in rates and rates[country] != rate:
                    conflicts.add(country)
                rates[country] = rate
            rates = {country: rate for country, rate in rates.items() if country not in conflicts}
            if not rates:
                raise ValueError('empty rate feed')
        except Exception:
            country_rates_retry_after = time.monotonic() + COUNTRY_RATES_RETRY
            return {}
        country_rates_cache = rates
        country_rates_expires = time.monotonic() + COUNTRY_RATES_TTL
        country_rates_retry_after = 0.0
        return dict(rates)


def product_reference_rate(detail, rates):
    """Match a named country, not symbols like $, euro, or the generic sales group."""
    name = str(detail.get('name', ''))
    matches = {value.strip() for value in re.findall(r'[（(]([^()（）]+)[)）]', name) if value.strip() in rates}
    if len(matches) > 1:
        return '', None
    if not matches:
        for key in ('country', 'country_name'):
            value = detail.get(key)
            if isinstance(value, str) and value.strip() in rates:
                matches.add(value.strip())
        if len(matches) > 1:
            return '', None
    if not matches:
        matches = {country for country in rates if country in name}
        matches = {country for country in matches if not any(country != other and country in other for other in matches)}
    if not matches:
        group = detail.get('group_tab')
        if isinstance(group, str) and group.strip() in rates:
            matches.add(group.strip())
    if len(matches) != 1:
        return '', None
    country = next(iter(matches))
    return country, reference_rate(rates[country])


def pick_channel_level(detail):
    levels = detail.get("brand_levels", {})
    for lv in ["level_2", "level_3", "level_4", "level_5"]:
        if lv in levels:
            return lv
    return None


def create_order(product_id, amount, quantity, channel_level):
    return upstream_api(
        "/orders",
        "POST",
        {
            "product_id": product_id,
            "recharge_amount": amount,
            "channel_level": channel_level,
            "quantity": quantity,
            "payment_password": UPSTREAM_PAY_PASS,
        },
    )


def submit_account(oid, account, password):
    return upstream_api(
        f"/orders/{oid}/submit-account",
        "POST",
        {"account": account, "password": password},
    )


def submit_2fa(oid, code):
    return upstream_api(f"/orders/{oid}/submit-2fa", "POST", {"code": code})


def create_recharge(amount):
    return upstream_api(
        "/wallet/recharge",
        "POST",
        {"channel": "alipay", "amount": amount},
    )


def get_recharge_status(rid):
    return upstream_api(f"/wallet/recharge/{rid}")


def get_order_status(oid):
    return upstream_api(f"/orders/{oid}?refresh=1")


def get_ai_plans():
    """获取 AI 代充套餐列表"""
    return upstream_api("/site/ai-membership-plans")


def create_ai_order(plan_id):
    """创建 AI 代充订单"""
    return upstream_api(
        "/orders/ai-membership",
        "POST",
        {"plan_id": plan_id, "payment_password": UPSTREAM_PAY_PASS},
    )


def submit_ai_token(oid, token):
    """提交 AI 账号 Session"""
    return upstream_api(
        f"/orders/{oid}/submit-ai-token", "POST", {"token": token}
    )


def get_ai_order_status(oid):
    """AI 订单状态轮询"""
    return upstream_api(f"/orders/{oid}/ai-status")


# ===== Durable order lifecycle =====
@contextmanager
def database():
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def read_order(oid):
    with database() as db:
        row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return dict(row) if row else None


def money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > 10000000:
            raise ValueError()
        return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("金额格式错误")


def sale_price(cost, profit):
    """Apply a signed percentage adjustment, ceiling the adjustment to whole yuan."""
    cost = money(cost)
    if cost <= 0:
        raise ValueError("成本必须大于 0")
    try:
        rate = Decimal(str(profit))
        if not rate.is_finite():
            raise ValueError("加价率必须为有限数值")
        if rate <= -100:
            raise ValueError("加价百分比必须大于 -100，优惠不能达到或超过 100%")
        markup = (cost * rate / Decimal("100")).to_integral_value(rounding=ROUND_CEILING)
        price = money(cost + markup)
    except DecimalException:
        raise ValueError("加价率格式错误")
    if price <= 0:
        raise ValueError("售价配置错误，请联系客服")
    return float(price)


def giftcard_cost(detail, amount):
    """The markup base includes the upstream service fee, rounded to cents."""
    return money(Decimal(str(amount)) * Decimal(str(detail["display_buy_rate"]))
                 + money(detail.get("service_fee_rmb", 0)))


def giftcard_quote(detail, amount, country_rate=None):
    """Reference face value and procurement-based payable are independent prices."""
    rate = reference_rate(country_rate)
    original = money(Decimal(str(amount)) * rate) if rate is not None else None
    cost = giftcard_cost(detail, amount)
    return {"amount": amount, "original_price": float(original) if original is not None else None, "sell_price": sale_price(cost, PROFIT)}


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,17}", str(value)):
        raise ValueError("编号格式错误")
    return int(value)


def order_flags(order):
    state = order.get("fulfillment_state", "pending")
    cred = order.get("credential_state", "pending")
    paid = bool(order.get("payment_verified"))
    waiting = state == "created" and cred == "pending" and order["status"] == 1
    review = state == "review" or cred == "review" or order.get("twofa_state") == "review"
    return {
        "paid": paid, "processing": state == "dispatching" or (paid and state == "pending") or cred == "submitting",
        "review_required": review,
        "need_account": waiting and order["order_type"] == "gift_card",
        "need_token": waiting and order["order_type"] == "ai_membership",
        "need_2fa": bool(order.get("need_2fa")) and order.get("twofa_state", "idle") == "idle" and order["status"] not in (4,5,6,7) and not review,
        "order_id": order["id"], "provider": order.get("ai_provider_code", "chatgpt"),
        "status": order["status"], "status_text": order.get("status_text", ""),
    }


def keep_secret(oid, credentials):
    if not credentials:
        return
    with ORDER_SECRETS_LOCK:
        ORDER_SECRETS[oid] = dict(credentials, expires_at=time.time() + SECRET_TTL)


def pop_secret(oid):
    with ORDER_SECRETS_LOCK:
        credentials = ORDER_SECRETS.pop(oid, None)
    if credentials and credentials.pop("expires_at", time.time() + 1) > time.time():
        return credentials
    return None


def cleanup_secrets():
    with ORDER_SECRETS_LOCK:
        for oid in list(ORDER_SECRETS):
            if ORDER_SECRETS[oid].get("expires_at", 0) <= time.time():
                ORDER_SECRETS.pop(oid, None)


def mark_review(oid):
    with database() as db:
        db.execute("UPDATE orders SET fulfillment_state='review', status_text='下单结果待核对，请联系客服', updated_at=? WHERE id=? AND fulfillment_state='dispatching'",
                   (datetime.now().isoformat(), oid))
    pop_secret(oid)


def verify_payment(order, check_id=None):
    """Never use a browser-supplied recharge ID or aggregate wallet balance as payment proof."""
    result = get_recharge_status(order["recharge_id"])
    if result.get("code") != 0:
        return False
    recharge = result.get("data", {})
    if recharge.get("status") != 1:
        return False
    try:
        valid = (positive_id(recharge.get("id")) == order["recharge_id"]
                 and money(recharge.get("amount")) == money(order["sell_price"]))
    except ValueError:
        valid = False
    if not valid:
        with database() as db:
            db.execute("UPDATE orders SET status_text='支付记录或金额待核对' WHERE id=? AND fulfillment_state='pending' AND payment_verified=0 AND (? IS NULL OR payment_check_id=?)", (order["id"], check_id, check_id))
        return False
    with database() as db:
        db.execute("UPDATE orders SET payment_verified=1, status=1, status_text='已付款，待处理', updated_at=? WHERE id=? AND fulfillment_state='pending' AND (? IS NULL OR payment_check_id=?)",
                   (datetime.now().isoformat(), order["id"], check_id, check_id))
    return bool(read_order(order["id"])["payment_verified"])


def check_payment_once(order):
    """A durable per-order lease coalesces overlapping reads across processes.

    Losers return the persisted state immediately; no blocked HTTP waiter or
    in-memory Future. A crashed reader's lease expires; reads do not charge.
    """
    attempt = uuid.uuid4().hex
    now = time.time()
    with database() as db:
        claimed = db.execute("UPDATE orders SET payment_check_id=?, payment_check_until=? WHERE id=? AND fulfillment_state='pending' AND payment_verified=0 AND payment_check_until<=?",
                             (attempt, now + PAYMENT_CHECK_LEASE_SECONDS, order["id"], now)).rowcount
    if not claimed:
        current = read_order(order["id"])
        return bool(current and current["payment_verified"])
    try:
        return verify_payment(order, attempt)
    finally:
        with database() as db:
            db.execute("UPDATE orders SET payment_check_id=NULL, payment_check_until=0 WHERE id=? AND payment_check_id=?", (order["id"], attempt))


def fulfill_order(oid):
    order = read_order(oid)
    if not order or order["fulfillment_state"] != "pending":
        return order
    if not order["payment_verified"] and not check_payment_once(order):
        return read_order(oid)
    order = read_order(oid)
    ai = order["order_type"] == "ai_membership"
    wallet = upstream_api("/wallet")
    if wallet.get("code") != 0:
        return order
    try:
        enough = money(wallet["data"]["balance"]) >= money(order["cost_price"])
    except (KeyError, TypeError, ValueError):
        enough = False
    if not enough:
        with database() as db:
            db.execute("UPDATE orders SET status_text='已付款，等待商户补足余额' WHERE id=? AND fulfillment_state='pending'", (oid,))
        return read_order(oid)
    # Commit the claim BEFORE any charge-producing request. SQLite arbitrates
    # across threads AND processes. A crash/timeout never resets this claim.
    with database() as db:
        claimed = db.execute("UPDATE orders SET fulfillment_state='dispatching', dispatch_started_at=?, status_text='正在创建上游订单', updated_at=? WHERE id=? AND fulfillment_state='pending' AND payment_verified=1 AND COALESCE(upstream_order_id,0)=0",
                             (time.time(), datetime.now().isoformat(), oid)).rowcount
    if not claimed:
        return read_order(oid)
    try:
        result = create_ai_order(order["product_id"]) if ai else create_order(order["product_id"], order["amount"], order["quantity"], order["channel_level"])
        if result.get("code") != 0:
            mark_review(oid)
            return read_order(oid)
        upstream_oid = positive_id(result["data"]["id"])
        # Persist the upstream identity before submitting any credential.
        with database() as db:
            db.execute("UPDATE orders SET upstream_order_id=?, fulfillment_state='created', status=1, status_text='待提交账号', credential_state='pending', updated_at=? WHERE id=? AND fulfillment_state='dispatching'",
                       (upstream_oid, datetime.now().isoformat(), oid))
    except Exception as exc:
        print(f"订单 {oid} 下单结果待核对: {type(exc).__name__}")
        mark_review(oid)
        return read_order(oid)
    credentials = pop_secret(oid)
    if credentials:
        deliver_credentials(oid, credentials)
    return read_order(oid)


def validate_account(account, password):
    if not isinstance(account, str) or len(account) > 254 or not is_valid_email(account):
        raise ValueError("Apple ID 邮箱格式不正确")
    if not isinstance(password, str) or not 6 <= len(password) <= 1024:
        raise ValueError("密码长度应为 6 至 1024 个字符")
    return {"account": account, "password": password}


def finish_credential_attempt(oid, attempt_id, state, status_text, status=None, need_2fa=None):
    """Only the still-active attempt may finish a waiting credential submission."""
    assignments = "credential_state=?, status_text=?, updated_at=?"
    values = [state, status_text, datetime.now().isoformat()]
    if status is not None:
        assignments += ", status=?, need_2fa=?"
        values.extend((status, int(bool(need_2fa))))
    with database() as db:
        return db.execute(f"UPDATE orders SET {assignments} WHERE id=? AND credential_attempt_id=? AND credential_state='submitting' AND fulfillment_state='created' AND payment_verified=1 AND status=1",
                          (*values, oid, attempt_id)).rowcount


def deliver_credentials(oid, credentials):
    order = read_order(oid)
    if not order or order["fulfillment_state"] != "created" or not order["payment_verified"] or not order["upstream_order_id"]:
        raise ValueError("请先完成付款并等待订单创建")
    ai = order["order_type"] == "ai_membership"
    if ai:
        token = validate_ai_credential(credentials.get("ai_token"), order["ai_provider_code"])
    else:
        credentials = validate_account(credentials.get("account"), credentials.get("password"))
    attempt_id = uuid.uuid4().hex
    with database() as db:
        claimed = db.execute("UPDATE orders SET credential_state='submitting', credential_attempt_id=?, updated_at=? WHERE id=? AND credential_state='pending' AND status=1 AND fulfillment_state='created' AND payment_verified=1 AND upstream_order_id=?",
                             (attempt_id, datetime.now().isoformat(), oid, order["upstream_order_id"])).rowcount
    if not claimed:
        return read_order(oid)
    try:
        result = submit_ai_token(order["upstream_order_id"], token) if ai else submit_account(order["upstream_order_id"], credentials["account"], credentials["password"])
        if result.get("code") != 0:
            state = "review" if result.get("ambiguous") else "pending"
            finish_credential_attempt(oid, attempt_id, state,
                                      "账号提交结果待核对" if state == "review" else "账号提交未通过，请重新提交")
            return read_order(oid)
        data = result["data"]
        need_2fa = bool(data.get("allow_submit_2fa", False))
        status = data.get("status", 2)
        if status not in (2, 3, 4, 5, 6, 7):
            status = 2
        finish_credential_attempt(oid, attempt_id, "submitted",
                                  "待验证码" if need_2fa else "充值处理中", status, need_2fa)
    except Exception as exc:
        print(f"订单 {oid} 账号提交结果待核对: {type(exc).__name__}")
        finish_credential_attempt(oid, attempt_id, "review", "账号提交结果待核对，请联系客服")
    finally:
        pop_secret(oid)
    return read_order(oid)


def finish_twofa_attempt(oid, attempt, state, need_2fa):
    with database() as db:
        return db.execute("UPDATE orders SET twofa_state=?, need_2fa=?, updated_at=? WHERE id=? AND twofa_attempt_id=? AND twofa_state='submitting' AND fulfillment_state='created' AND payment_verified=1 AND status NOT IN (4,5,6,7)",
                          (state, int(need_2fa), datetime.now().isoformat(), oid, attempt)).rowcount


def deliver_2fa(order, code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("验证码必须是 6 位数字")
    attempt = uuid.uuid4().hex
    with database() as db:
        claimed = db.execute("UPDATE orders SET twofa_state='submitting', twofa_attempt_id=?, updated_at=? WHERE id=? AND order_type='gift_card' AND fulfillment_state='created' AND payment_verified=1 AND upstream_order_id>0 AND need_2fa=1 AND twofa_state='idle' AND status NOT IN (4,5,6,7)",
                             (attempt, datetime.now().isoformat(), order["id"])).rowcount
    if not claimed:
        raise ValueError("当前订单未等待验证码，或验证码正在处理")
    try:
        result = submit_2fa(order["upstream_order_id"], code)
        success = result.get("code") == 0
        state = "submitted" if success else ("review" if result.get("ambiguous") else "idle")
        finish_twofa_attempt(order["id"], attempt, state, not success and state == "idle")
    except Exception:
        finish_twofa_attempt(order["id"], attempt, "review", False)
        raise ValueError("验证码提交结果待核对，请稍后查询订单") from None
    if not success:
        raise ValueError("验证码提交未确认，请稍后查询订单")
    return read_order(order["id"])


def refresh_order(order):
    ai = order["order_type"] == "ai_membership"
    result = get_ai_order_status(order["upstream_order_id"]) if ai else get_order_status(order["upstream_order_id"])
    if result.get("code") != 0:
        return
    data = result.get("data", {})
    status = data.get("status")
    if status not in (1, 2, 3, 4, 5, 6, 7):
        return
    # A stale upstream 'waiting' response must not undo a concurrent submission.
    if status == 1 and order["credential_state"] != "pending":
        return
    # The upstream exposes no verifiable challenge ID. Neither repeated true
    # nor a false -> true edge proves a new challenge (responses may be stale).
    # Accepted submissions stay closed; a genuinely new challenge needs
    # merchant reconciliation, not automatic reopening based on this boolean.
    twofa_state = order.get("twofa_state", "idle")
    allow = data.get("allow_submit_2fa") is True
    need_2fa = int(allow and twofa_state == "idle" and status not in (4,5,6,7))
    text = str(data.get("status_text", "处理中"))[:200]
    if twofa_state == "submitted" and allow and status not in (4,5,6,7):
        text = "验证码已提交，等待平台更新"
    with database() as db:
        db.execute("UPDATE orders SET status=?, status_text=?, progress=?, fail_reason=?, recharged_amount=?, need_2fa=?, twofa_state=?, credential_state=CASE WHEN ? IN (2,3,4,5,6,7) THEN 'submitted' ELSE credential_state END, updated_at=? WHERE id=? AND fulfillment_state='created' AND credential_state=? AND updated_at IS ? AND status NOT IN (4,5,6,7)",
                   (status, text, data.get("progress", 0),
                    str(data.get("fail_reason", ""))[:500], data.get("recharged_amount", 0),
                    need_2fa, twofa_state, status, datetime.now().isoformat(), order["id"], order["credential_state"], order["updated_at"]))


def recover_stale_operations():
    # More than the 60/120-second upstream timeouts. Never retry dispatching.
    with database() as db:
        db.execute("UPDATE orders SET fulfillment_state='review', status_text='下单结果待核对，请联系客服' WHERE fulfillment_state='dispatching' AND dispatch_started_at<?", (time.time() - 300,))
        for column in ("credential_state", "twofa_state"):
            db.execute(f"UPDATE orders SET {column}='review', status_text='提交结果待核对，请联系客服' WHERE {column}='submitting' AND datetime(updated_at)<datetime('now','localtime','-5 minutes')")


def process_maintenance_order(oid, lease):
    try:
        order = read_order(oid)
        if not order or order["worker_lease_id"] != lease:
            return
        if order["fulfillment_state"] == "created":
            refresh_order(order)
        else:
            fulfill_order(oid)
            with database() as db:
                cancelled = db.execute("UPDATE orders SET status=5, status_text='支付窗口已超时，可重新检查付款' WHERE id=? AND fulfillment_state='pending' AND payment_verified=0 AND status=0 AND datetime(created_at)<datetime('now','localtime','-5 minutes')", (oid,)).rowcount
            if cancelled:
                pop_secret(oid)
    except Exception as exc:
        print(f"订单 {oid} 后台检查异常: {type(exc).__name__}")
    finally:
        try:
            with database() as db:
                db.execute("UPDATE orders SET next_check=?, worker_lease_id=NULL, worker_lease_until=0 WHERE id=? AND worker_lease_id=?", (time.time() + 15, oid, lease))
        except Exception as exc:
            print(f"订单 {oid} 后台租约清理异常: {type(exc).__name__}")
        finally:
            WORKER_SLOTS.release()


def maintenance_tick(executor=None):
    # The production loop owns a long-lived executor and never waits for the
    # slowest task before scheduling the next tick. No unbounded work queue.
    if executor is None:
        with ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY) as pool:
            return maintenance_tick(pool)
    cleanup_secrets()
    recover_stale_operations()
    now = time.time()
    with database() as db:
        rows = db.execute("SELECT id FROM orders WHERE worker_lease_until<=? AND next_check<=? AND ((fulfillment_state='pending' AND (status IN (0,1) OR (status=5 AND datetime(created_at)>datetime('now','localtime','-2 days')))) OR (fulfillment_state='created' AND status NOT IN (4,5,6,7))) ORDER BY next_check,id LIMIT 40", (now, now)).fetchall()
    for row in rows:
        if not WORKER_SLOTS.acquire(blocking=False):
            break
        oid, lease = row["id"], uuid.uuid4().hex
        submitted = False
        try:
            now = time.time()
            with database() as db:
                claimed = db.execute("UPDATE orders SET worker_lease_id=?, worker_lease_until=? WHERE id=? AND next_check<=? AND worker_lease_until<=? AND (SELECT COUNT(*) FROM orders WHERE worker_lease_until>?)<?",
                                     (lease, now + WORKER_LEASE_SECONDS, oid, now, now, now, WORKER_CONCURRENCY)).rowcount
            if claimed:
                executor.submit(process_maintenance_order, oid, lease)
                submitted = True
        finally:
            if not submitted:
                try:
                    with database() as db:
                        db.execute("UPDATE orders SET worker_lease_id=NULL, worker_lease_until=0 WHERE id=? AND worker_lease_id=?", (oid, lease))
                finally:
                    WORKER_SLOTS.release()


def worker_loop():
    with ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY) as executor:
        while True:
            try:
                maintenance_tick(executor)
            except Exception as exc:
                print(f"后台任务异常: {type(exc).__name__}")
            time.sleep(5)


def payment_qr(payload):
    if not isinstance(payload, str) or not payload or len(payload) > 4096:
        raise ValueError("支付二维码格式错误")
    svg = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathFillImage).to_string().decode()
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg, safe="")


class RechargeAlreadyBoundError(ValueError):
    """The upstream payment belongs to an existing local order."""


class CheckoutConflictError(ValueError):
    pass


def checkout_identity(body, ai, key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("缺少有效下单凭证，请刷新页面后重试")
    pid = positive_id(body.get("plan_id" if ai else "product_id"))
    amount = None if ai else parse_amount(body.get("amount"))
    if not ai and amount is None:
        raise ValueError("面值格式错误")
    # Credentials are intentionally excluded: retries never overwrite the first
    # accepted credentials. They may be resubmitted through authenticated APIs.
    fingerprint = json.dumps([bool(ai), pid, amount], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    token = hashlib.sha256(("order-access-v1:" + key).encode()).hexdigest()
    return digest, fingerprint, token


def replay_checkout(request, fingerprint, token):
    if request["fingerprint"] != fingerprint:
        raise CheckoutConflictError("同一下单凭证对应的商品或面值已改变")
    if request["state"] != "completed":
        raise CheckoutConflictError("付款单正在创建或结果待核对，请保留页面稍后重试；请勿重复创建付款单")
    order = read_order(request["order_id"])
    if not order or not order_access_allowed(order, token):
        raise CheckoutConflictError("订单记录待核对，请联系客服")
    return {"id": order["id"], "order_no": order["order_no"], "sell_price": order["sell_price"],
            "qr_code": payment_qr(order["qr_payload"]), "recharge_id": order["recharge_id"], "access_token": token,
            "paid": bool(order["payment_verified"])}


def create_local_order(body, ai=False, request_key=None):
    identity = checkout_identity(body, ai, request_key) if request_key is not None else None
    if identity:
        key_hash, fingerprint, token = identity
        with database() as db:
            previous = db.execute("SELECT * FROM checkout_requests WHERE key_hash=?", (key_hash,)).fetchone()
        if previous:
            return replay_checkout(previous, fingerprint, token)
    pid = positive_id(body.get("plan_id" if ai else "product_id"))
    credentials = None
    amount, channel = None, None
    if ai:
        result = get_ai_plans()
        plans = result.get("data", {}).get("plans", []) if result.get("code") == 0 else []
        # Stock is informational: enabled AI plans can be replenished upstream.
        detail = next((p for p in plans if p["id"] == pid and p.get("enabled")), None)
        if not detail:
            raise ValueError("套餐已下架或不存在")
        cost = money(detail["price_rmb"])
        sell = sale_price(cost, AI_PROFIT)
        if body.get("ai_token"):
            credentials = {"ai_token": validate_ai_credential(body["ai_token"], detail.get("provider_code", "chatgpt"))}
    else:
        detail = get_product_detail(pid)
        if not detail:
            raise ValueError("商品不存在")
        amount = parse_amount(body.get("amount"))
        if amount is None:
            raise ValueError("面值格式错误")
        fixed = any(money(amount) == money(a) for a in detail.get("amounts", []))
        custom = bool(detail.get("is_custom")) and float(detail.get("min_amount", 0)) <= amount <= float(detail.get("max_amount", 0))
        if not (fixed or custom):
            raise ValueError("请选择商品支持的面值")
        channel = pick_channel_level(detail)
        if not channel:
            raise ValueError("通道不可用")
        cost = giftcard_cost(detail, amount)
        sell = sale_price(cost, PROFIT)
        if body.get("account") or body.get("password"):
            credentials = validate_account(body.get("account"), body.get("password"))
    if identity:
        with database() as db:
            claimed = db.execute("INSERT OR IGNORE INTO checkout_requests(key_hash,fingerprint,state,created_at) VALUES(?,?,'creating',?)",
                                 (key_hash, fingerprint, time.time())).rowcount
            previous = db.execute("SELECT * FROM checkout_requests WHERE key_hash=?", (key_hash,)).fetchone()
        if not claimed:
            return replay_checkout(previous, fingerprint, token)
    try:
        return commit_checkout(detail, pid, amount, channel, cost, sell, credentials, ai, identity)
    except Exception:
        if identity:
            with database() as db:
                db.execute("UPDATE checkout_requests SET state='review' WHERE key_hash=? AND state='creating'", (key_hash,))
        raise


def commit_checkout(detail, pid, amount, channel, cost, sell, credentials, ai, identity):
    result = create_recharge(sell)
    if result.get("code") != 0:
        raise ValueError("创建付款单未成功，请稍后重试")
    recharge = result["data"]
    rid = positive_id(recharge["id"])
    # Preserve the upstream reference verbatim for matching wallet ref_no later.
    # It is not a payment credential and never comes from the browser request.
    recharge_no = recharge.get("recharge_no")
    if not isinstance(recharge_no, str) or not recharge_no.strip():
        recharge_no = None
    payload = recharge["pay_data"]["qr_code"]
    qr = payment_qr(payload)
    token, digest = new_order_access_token() if identity is None else (identity[2], hashlib.sha256(identity[2].encode()).hexdigest())
    order_no = ("AI" if ai else "MY") + uuid.uuid4().hex.upper()
    now = datetime.now().isoformat()
    try:
        with database() as db:
            cursor = db.execute("""INSERT INTO orders
                (order_no,order_type,recharge_id,recharge_no,upstream_order_id,product_id,product_name,country,amount,quantity,channel_level,cost_price,sell_price,status,status_text,created_at,updated_at,access_token_hash,ai_provider_code,qr_payload)
                VALUES (?,?,?,?,0,?,?,?,?,1,?,?,?,0,'待付款',?,?,?,?,?)""",
                (order_no, "ai_membership" if ai else "gift_card", rid, recharge_no, pid, str(detail.get("name", ""))[:200], str(detail.get("group_tab", ""))[:100], amount, channel, float(cost), sell, now, now, digest, detail.get("provider_code", "chatgpt"), payload))
            oid = cursor.lastrowid
            if identity:
                changed = db.execute("UPDATE checkout_requests SET state='completed', order_id=? WHERE key_hash=? AND state='creating'", (oid, identity[0])).rowcount
                if changed != 1:
                    raise RuntimeError("Checkout claim was lost")
    except sqlite3.IntegrityError as exc:
        # The UNIQUE index arbitrates concurrent requests across processes.
        # Do not return the existing order, its token or QR to another caller;
        # do not retry recharge creation or change the payable amount.
        if str(exc) == "UNIQUE constraint failed: orders.recharge_id":
            raise RechargeAlreadyBoundError("付款单已关联其他订单，本次未创建订单，请联系客服核对") from exc
        raise
    keep_secret(oid, credentials)
    return {"id": oid, "order_no": order_no, "sell_price": sell, "qr_code": qr, "recharge_id": rid, "access_token": token, "paid": False}


class RateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def allow(self, key, limit):
        now = time.monotonic()
        with self.lock:
            count, until = self.entries.get(key, (0, now + 60))
            if until <= now:
                count, until = 0, now + 60
            self.entries[key] = (count + 1, until)
            self.entries.move_to_end(key)
            while len(self.entries) > 10000:
                self.entries.popitem(last=False)
            return count < limit


RATE_LIMITER = RateLimiter()


class Handler(BaseHTTPRequestHandler):
    server_version = "Shop"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # No tokens, request bodies, URLs, or upstream error bodies in logs.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    def send_json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def limit(self, group, count):
        ip = self.client_address[0]
        if TRUST_PROXY:
            ip = self.headers.get("X-Real-IP", ip)
        if not RATE_LIMITER.allow((ip, group), count):
            self.send_json({"code": 429, "msg": "请求频繁，请稍后再试"}, 429)
            return False
        return True

    def authorized_order(self, oid, token, kind=None):
        oid = positive_id(oid)
        order = read_order(oid)
        if not order or not order_access_allowed(order, token):
            raise PermissionError()
        if kind and order["order_type"] != kind:
            raise ValueError("订单类型与接口不匹配")
        return order

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/healthz":
                self.send_json({"code": 0})
                return
            if not self.limit("get", 600):
                return
            if path in ("/", "/index.html", "/contact-qrcode.jpg"):
                filename = "contact-qrcode.jpg" if path.endswith(".jpg") else "index.html"
                data = (ASSET_DIR / filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg" if filename.endswith(".jpg") else "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/config":
                self.send_json({"code": 0, "data": {"giftcard": ENABLE_GIFTCARD, "ai": ENABLE_AI}})
            elif path == "/api/products":
                products = [p for p in get_products() if giftcard_visible(p)] if ENABLE_GIFTCARD else []
                country_rates = get_country_rates() if products else {}
                result = []
                for p in products:
                    amounts = [float(a) for a in p.get("amounts", [])]
                    rate_country, rate = product_reference_rate(p, country_rates)
                    result.append({"id": positive_id(p["id"]), "name": str(p["name"]), "country": str(p.get("group_tab", "")), "symbol": str(p.get("symbol", "")),
                                   "amounts": amounts, "is_custom": bool(p.get("is_custom")), "min_amount": p.get("min_amount", 0), "max_amount": p.get("max_amount", 0),
                                   "buy_rate": float(p.get("display_buy_rate", 0)), "service_fee": float(p.get("service_fee_rmb", 0)), "profit_percent": float(PROFIT),
                                   "reference_country": rate_country, "reference_rate": float(rate) if rate is not None else None,
                                   "quotes": [giftcard_quote(p, amount, rate) for amount in amounts]})
                self.send_json({"code": 0, "data": result})
            elif path == "/api/ai-plans":
                result = get_ai_plans() if ENABLE_AI else {"code": 0, "data": {"plans": []}}
                if result.get("code") != 0:
                    raise ValueError("套餐暂时未加载，请稍后重试")
                # Show all enabled plans; upstream handles stock replenishment.
                plans = [dict(p, sell_price=sale_price(p["price_rmb"], AI_PROFIT)) for p in result["data"]["plans"] if p.get("enabled")]
                self.send_json({"code": 0, "data": plans})
            elif path.startswith("/api/order/"):
                order = self.authorized_order(path.rsplit("/", 1)[-1], self.headers.get("Authorization", "").removeprefix("Bearer "))
                self.send_json({"code": 0, "data": public_order(order)})
            else:
                self.send_json({"code": 404, "msg": "Not Found"}, 404)
        except PermissionError:
            self.send_json({"code": 404, "msg": "订单不存在"}, 404)
        except ValueError as exc:
            self.send_json({"code": 1, "msg": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"GET 异常: {type(exc).__name__}")
            self.send_json({"code": 500, "msg": "服务暂时异常"}, 500)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            origin = self.headers.get("Origin")
            expected_origin = PUBLIC_ORIGIN or ("http://" + self.headers.get("Host", ""))
            if origin and origin != expected_origin:
                self.send_json({"code": 403, "msg": "来源校验失败"}, 403)
                return
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("请求编码格式错误")
            length_text = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,9}", length_text):
                raise ValueError("请求长度格式错误")
            length = int(length_text)
            body_limit = MAX_AI_BODY if path in ("/api/create-ai-order", "/api/submit-ai-token") else MAX_BODY
            if length > body_limit:
                self.send_json({"code": 413, "msg": "请求内容过长"}, 413)
                return
            if self.headers.get_content_type() != "application/json":
                self.send_json({"code": 415, "msg": "请使用 JSON 请求"}, 415)
                return
            group = "create" if path in ("/api/create-order", "/api/create-ai-order") else ("credential" if path.startswith("/api/submit-") else "check")
            if not self.limit(group, {"create": 6, "credential": 12, "check": 120}[group]):
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求不完整")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            if path in ("/api/create-order", "/api/create-ai-order"):
                ai = path == "/api/create-ai-order"
                if not (ENABLE_AI if ai else ENABLE_GIFTCARD):
                    raise ValueError("该业务尚未启用")
                request_key = self.headers.get("Idempotency-Key")
                checkout_identity(body, ai, request_key)
                self.send_json({"code": 0, "data": create_local_order(body, ai, request_key=request_key)})
                return
            kinds = {"/api/check-recharge": "gift_card", "/api/submit-account": "gift_card", "/api/submit-2fa": "gift_card", "/api/check-ai-recharge": "ai_membership", "/api/submit-ai-token": "ai_membership"}
            if path not in kinds and path != "/api/payment-info":
                self.send_json({"code": 404, "msg": "Not Found"}, 404)
                return
            order = self.authorized_order(body.get("order_id"), body.get("access_token"), kinds.get(path))
            if path in ("/api/check-recharge", "/api/check-ai-recharge"):
                # A mismatched legacy field is rejected; the trusted ID is
                # always read from SQLite. New clients need not send it.
                if "recharge_id" in body and positive_id(body["recharge_id"]) != order["recharge_id"]:
                    raise ValueError("支付单与订单不匹配")
                order = fulfill_order(order["id"])
            elif path == "/api/submit-account":
                order = deliver_credentials(order["id"], validate_account(body.get("account"), body.get("password")))
            elif path == "/api/submit-ai-token":
                order = deliver_credentials(order["id"], {"ai_token": body.get("token")})
            elif path == "/api/submit-2fa":
                order = deliver_2fa(order, body.get("code"))
            elif path == "/api/payment-info":
                if order["payment_verified"]:
                    raise ValueError("订单已付款，请查看处理状态")
                if not order.get("qr_payload"):
                    raise ValueError("该历史订单没有保存支付信息，请联系客服")
                self.send_json({"code": 0, "data": {"id": order["id"], "order_no": order["order_no"], "sell_price": order["sell_price"], "qr_code": payment_qr(order["qr_payload"]), "type": "ai" if order["order_type"] == "ai_membership" else "giftcard"}})
                return
            self.send_json({"code": 0, "data": order_flags(order)})
        except PermissionError:
            self.send_json({"code": 404, "msg": "订单不存在"}, 404)
        except CheckoutConflictError as exc:
            self.send_json({"code": 409, "msg": str(exc)}, 409)
        except RechargeAlreadyBoundError as exc:
            self.send_json({"code": 409, "msg": str(exc)}, 409)
        except (ValueError, UnicodeError) as exc:
            msg = "请求格式错误" if isinstance(exc, json.JSONDecodeError) else str(exc)
            self.send_json({"code": 1, "msg": msg}, 400)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as exc:
            print(f"POST 异常: {type(exc).__name__}")
            self.send_json({"code": 500, "msg": "服务暂时异常，请稍后查询订单"}, 500)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == "__main__":
    validate_config()
    init_db()
    recover_stale_operations()
    login()
    threading.Thread(target=worker_loop, daemon=True).start()
    print(f"代理站启动: {BIND_HOST}:{PORT}")
    BoundedHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
