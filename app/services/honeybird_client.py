"""Admin client for honeybird.frappe.cloud — RAI's own ERPNext where
customer + subscription records live.

This is a SEPARATE concern from the per-user ErpnextClient (which talks
to the customer's OWN ERPNext to fetch their data). This client uses a
fixed admin API key to read/write Honey Bird's internal billing system.

Scope (deliberately limited):
  - Create User (Website User) + Customer + Subscription on signup.
    The User record holds NO ERP credentials — those live on the
    customer's device only.
  - Verify the RAI account password (Frappe's login endpoint).
  - Look up subscription status + per-Plan limits on every login.
  - Track which devices have been registered against a Subscription
    (enforced for max_devices, the only real "single user" gate).
  - Record bound erp_url / company on first use (logged for now,
    enforced in a future phase).
"""
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()


class HoneyBirdError(Exception):
    """Anything the Honey Bird admin API rejects with a non-200."""


class DeviceLimitExceeded(Exception):
    """The user tried to register a new device past their plan's
    max_devices limit. Carries the limit so the API layer can show
    a useful error to the user."""

    def __init__(self, current: int, limit: int):
        self.current = current
        self.limit = limit
        super().__init__(
            f"Device limit reached: {current}/{limit} devices already "
            f"registered. Sign out from another device first, or upgrade "
            f"your plan."
        )


def _admin_headers() -> dict[str, str]:
    """Standard auth header for admin calls. Built fresh per call so a
    rotated secret takes effect on the next request without restart."""
    if not (settings.honeybird_admin_key and settings.honeybird_admin_secret):
        raise HoneyBirdError(
            "Honey Bird admin credentials are not configured. Set "
            "HONEYBIRD_ADMIN_KEY and HONEYBIRD_ADMIN_SECRET in Cloud Run "
            "secrets, or via .env in local dev."
        )
    return {
        "Authorization": (
            f"token {settings.honeybird_admin_key}:"
            f"{settings.honeybird_admin_secret}"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.honeybird_url,
        timeout=httpx.Timeout(connect=15.0, read=30.0, write=10.0, pool=10.0),
    )


async def _post_method(method: str, **payload: Any) -> Any:
    """Call frappe's `/api/method/<method>` POST endpoint. Returns the
    `message` field from the JSON response (Frappe's RPC convention)."""
    async with _client() as c:
        resp = await c.post(
            f"/api/method/{method}",
            json=payload,
            headers=_admin_headers(),
        )
        if resp.status_code >= 400:
            raise HoneyBirdError(
                f"Honey Bird {method} failed ({resp.status_code}): "
                f"{resp.text[:300]}"
            )
        return resp.json().get("message")


async def _get_doc(doctype: str, name: str) -> dict[str, Any]:
    async with _client() as c:
        resp = await c.get(
            "/api/method/frappe.client.get",
            params={"doctype": doctype, "name": name},
            headers=_admin_headers(),
        )
        if resp.status_code != 200:
            raise HoneyBirdError(
                f"Get {doctype}/{name} failed: {resp.status_code}"
            )
        return resp.json().get("message", {})


async def _get_list(
    doctype: str,
    fields: list[str],
    filters: list[list[Any]] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "doctype": doctype,
        "fields": json.dumps(fields),
        "limit_page_length": limit,
    }
    if filters:
        params["filters"] = json.dumps(filters)
    async with _client() as c:
        resp = await c.get(
            "/api/method/frappe.client.get_list",
            params=params,
            headers=_admin_headers(),
        )
        if resp.status_code != 200:
            raise HoneyBirdError(
                f"List {doctype} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json().get("message") or []


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


async def user_exists(email: str) -> bool:
    """Check if a User record with this email already exists."""
    rows = await _get_list("User", ["name"], [["email", "=", email]], limit=1)
    return bool(rows)


async def create_signup(
    *,
    email: str,
    password: str,
    full_name: str,
    phone: Optional[str],
    trial_days: int,
) -> dict[str, Any]:
    """Atomic-ish signup: User → Customer → Subscription. We do
    cleanup-on-failure: if Customer or Subscription create fails, the
    earlier records are deleted so /signup can be retried with a clean
    slate. Without this, a half-failure leaves orphans (User exists
    but Subscription missing → user is signed up but always paywalled
    with no easy recovery path).

    NOTE: ERP credentials are *intentionally* not passed in. They live
    on the customer's device only. The caller (auth.py /signup) has
    already validated that the credentials work by test-logging-in
    against the user's ERP before calling this function.

    Returns the created Subscription dict.
    """
    user_created = False
    customer_name: Optional[str] = None

    try:
        # 1. User (Website User). Frappe rejects duplicate emails, so
        #    upstream callers must check user_exists first if they want a
        #    friendly error.
        logger.info(f"create_signup: creating User for {email}")
        await _post_method(
            "frappe.client.insert",
            doc={
                "doctype": "User",
                "email": email,
                "first_name": full_name.split(" ")[0],
                "last_name": " ".join(full_name.split(" ")[1:]) or None,
                "send_welcome_email": 0,
                "user_type": "Website User",
                "enabled": 1,
                "new_password": password,
                "mobile_no": phone,
            },
        )
        user_created = True

        # 2. Customer (RAI Subscription group). Linked to user via owner.
        #
        # default_currency=USD pins this Customer's billing to USD so the
        # USD-priced Subscription Plans (RAI Free Trial / RAI Solo / etc)
        # validate cleanly even when Honey Bird's company default is IQD.
        # Frappe's Subscription currency validator checks Customer.default
        # _currency first and only falls back to Company currency when
        # Customer.default_currency is empty.
        logger.info(f"create_signup: creating Customer for {email}")
        customer = await _post_method(
            "frappe.client.insert",
            doc={
                "doctype": "Customer",
                "customer_name": full_name,
                "customer_type": "Individual",
                "customer_group": "RAI Subscription",
                "territory": "All Territories",
                "mobile_no": phone,
                "email_id": email,
                "default_currency": "USD",
            },
        )
        customer_name = customer["name"]  # auto-generated CUST-XXXX id

        # 3. Subscription with a 30-day trial. Frappe's Subscription doctype
        #    has trial_period_start / trial_period_end fields; status stays
        #    "Active" through the trial. After trial_period_end, if no
        #    payment is recorded, ERPNext flips it to "Past Due Date" on
        #    the daily scheduler — we use that as the "expired" signal.
        today = date.today().isoformat()
        trial_end = (date.today() + timedelta(days=trial_days)).isoformat()
        logger.info(
            f"create_signup: creating Subscription "
            f"(party={customer_name}, trial_end={trial_end})"
        )
        subscription = await _post_method(
            "frappe.client.insert",
            doc={
                "doctype": "Subscription",
                "party_type": "Customer",
                "party": customer_name,
                "start_date": today,
                "trial_period_start": today,
                "trial_period_end": trial_end,
                "plans": [{"plan": "RAI Free Trial", "qty": 1}],
                "status": "Active",
            },
        )
        return subscription

    except Exception as e:
        # Best-effort rollback. Delete in reverse-create order so child
        # records are gone before parents.
        logger.exception(
            f"create_signup: failed mid-flight for {email} — rolling back "
            f"(user_created={user_created}, customer={customer_name!r})"
        )
        if customer_name:
            try:
                await _delete_doc("Customer", customer_name)
                logger.info(f"create_signup: rolled back Customer {customer_name}")
            except Exception:
                logger.exception(
                    f"create_signup: rollback FAILED for Customer "
                    f"{customer_name} — manual cleanup needed"
                )
        if user_created:
            try:
                await _delete_doc("User", email)
                logger.info(f"create_signup: rolled back User {email}")
            except Exception:
                logger.exception(
                    f"create_signup: rollback FAILED for User {email} — "
                    f"manual cleanup needed"
                )
        # Re-raise the original error so the API layer surfaces the
        # underlying Frappe message (e.g. currency mismatch) to the user.
        raise


async def get_user(email: str) -> Optional[dict[str, Any]]:
    """Returns the User doc or None."""
    if not await user_exists(email):
        return None
    return await _get_doc("User", email)


async def verify_password(email: str, password: str) -> bool:
    """Verify the user's RAI password by hitting Frappe's /api/method/login.
    Returns 200 on success / 401 on failure. We don't keep the session
    cookie — only the success/fail signal."""
    async with _client() as c:
        resp = await c.post(
            "/api/method/login",
            data={"usr": email, "pwd": password},
        )
        return resp.status_code == 200


async def _find_subscription(email: str) -> Optional[dict[str, Any]]:
    """Find the most recent Subscription for the user's Customer record.
    Returns the Subscription dict or None. Customer is linked by
    email_id, not by User.

    Logs the specific step that came up empty (no Customer vs no
    Subscription on Customer) so support cases like "user logged in
    but says trial ended" can be diagnosed from the Cloud Run logs
    without needing desk access.

    Note: child-table fields (`rai_bound_urls`, `rai_bound_companies`,
    `rai_registered_devices`) are NOT included here — `get_list`
    doesn't return child rows. Callers that need the bindings should
    use `_read_subscription_children` (read) or
    `_modify_subscription_children` (read-modify-save).
    """
    customers = await _get_list(
        "Customer",
        fields=["name"],
        filters=[["email_id", "=", email]],
        limit=1,
    )
    if not customers:
        logger.info(
            f"_find_subscription: no Customer with email_id={email!r}. "
            f"User exists in Honey Bird but never went through /signup "
            f"(or Customer was deleted)."
        )
        return None
    customer_name = customers[0]["name"]

    subs = await _get_list(
        "Subscription",
        fields=[
            "name", "status", "start_date",
            "trial_period_start", "trial_period_end",
            "current_invoice_end", "cancelation_date",
        ],
        filters=[
            ["party_type", "=", "Customer"],
            ["party", "=", customer_name],
        ],
        limit=1,
    )
    if not subs:
        logger.info(
            f"_find_subscription: Customer {customer_name!r} (email={email!r}) "
            f"exists but has no Subscription. Manual create_signup recovery "
            f"needed, or create one in the desk with plan=RAI Free Trial."
        )
        return None
    return subs[0]


async def _delete_doc(doctype: str, name: str) -> None:
    """Hard-delete a doc by name. Used by signup rollback when
    create_signup fails partway through. Not used for child-table
    rows — those are managed by saving the parent (see below)."""
    await _post_method("frappe.client.delete", doctype=doctype, name=name)


async def _modify_subscription_children(
    subscription_name: str,
    parentfield: str,
    mutate: "Any",  # Callable[[list[dict]], list[dict]]
) -> list[dict[str, Any]]:
    """Read-mutate-save pattern for a Subscription's child table. We
    fetch the parent doc (which includes all child tables inline),
    pass the children list through the caller's `mutate` function,
    write the result back to the parent's field, and save the whole
    Subscription via `frappe.client.save`.

    Why parent-save rather than direct child-doctype API calls:
    Frappe's permission system on child doctypes is independent of
    the parent — `frappe.client.get_list("RAI Device", ...)` requires
    Read on `RAI Device`, which isn't auto-granted to System Manager
    on custom child doctypes. By going through the parent we only
    need Subscription permissions, which the admin user already has.

    Bonus: it's also one round trip per change instead of two (fetch
    parent / mutate / save) vs (list children / insert child / list
    again to verify), and atomic at the parent level — if the save
    fails the children stay consistent with whatever else is on the
    Subscription.

    Returns the new children list (post-mutation).
    """
    sub = await _get_doc("Subscription", subscription_name)
    rows: list[dict[str, Any]] = list(sub.get(parentfield) or [])
    new_rows = mutate(rows)
    sub[parentfield] = new_rows
    await _post_method("frappe.client.save", doc=sub)
    return new_rows


async def _read_subscription_children(
    subscription_name: str,
    parentfield: str,
) -> list[dict[str, Any]]:
    """Read-only counterpart of `_modify_subscription_children`. Same
    rationale: avoid direct child-doctype permissions by going via
    the parent."""
    sub = await _get_doc("Subscription", subscription_name)
    return list(sub.get(parentfield) or [])


async def _get_plan_limits(subscription_name: str) -> dict[str, int]:
    """Read the linked Subscription Plan's max_* fields. Returns sane
    defaults (1/1/1 — the free-trial shape) if the plan or its custom
    fields aren't found, so a misconfigured Plan never accidentally
    grants infinite access."""
    plans_resp = await _get_list(
        "Subscription Plan Detail",
        fields=["plan"],
        filters=[["parent", "=", subscription_name]],
        limit=1,
    )
    if not plans_resp:
        return {"max_devices": 1, "max_erp_urls": 1, "max_companies": 1,
                "plan_name": None}
    plan_name = plans_resp[0]["plan"]
    try:
        plan = await _get_doc("Subscription Plan", plan_name)
    except HoneyBirdError:
        return {"max_devices": 1, "max_erp_urls": 1, "max_companies": 1,
                "plan_name": plan_name}
    return {
        "max_devices": int(plan.get("rai_max_devices") or 1),
        "max_erp_urls": int(plan.get("rai_max_erp_urls") or 1),
        "max_companies": int(plan.get("rai_max_companies") or 1),
        "plan_name": plan_name,
    }


async def get_subscription_status(email: str) -> dict[str, Any]:
    """Return the user's current subscription status. Reads the most
    recent Subscription for the email's Customer record.

    Output keys mirror the SubscriptionStatus pydantic model:
      status: "trial" | "active" | "expired" | "cancelled" | "none"
      days_remaining: int
      end_date: str | None  (YYYY-MM-DD)
      plan_name: str | None
    """
    sub = await _find_subscription(email)
    if not sub:
        return {"status": "none", "days_remaining": 0,
                "end_date": None, "plan_name": None}

    limits = await _get_plan_limits(sub["name"])
    plan_name = limits["plan_name"]
    today = date.today()

    def _days_to(d_str: str | None) -> int:
        if not d_str:
            return 0
        try:
            return (date.fromisoformat(str(d_str)[:10]) - today).days
        except ValueError:
            return 0

    # Status priority: cancelled > trial-active > active > expired
    # Frappe's fieldname is `cancelation_date` (single l) — note the
    # American spelling. Our internal status string is "cancelled"
    # (UK/double-l) since that's what Flutter expects.
    if sub.get("status") == "Cancelled" or sub.get("cancelation_date"):
        return {"status": "cancelled", "days_remaining": 0,
                "end_date": str(sub.get("cancelation_date") or "")[:10] or None,
                "plan_name": plan_name}

    trial_end = sub.get("trial_period_end")
    if trial_end:
        days = _days_to(trial_end)
        if days >= 0:
            return {"status": "trial", "days_remaining": days,
                    "end_date": str(trial_end)[:10], "plan_name": plan_name}
        invoice_end = sub.get("current_invoice_end")
        if invoice_end:
            paid_days = _days_to(invoice_end)
            if paid_days >= 0:
                return {"status": "active", "days_remaining": paid_days,
                        "end_date": str(invoice_end)[:10],
                        "plan_name": plan_name}
        return {"status": "expired", "days_remaining": 0,
                "end_date": str(trial_end)[:10], "plan_name": plan_name}

    invoice_end = sub.get("current_invoice_end")
    if invoice_end:
        paid_days = _days_to(invoice_end)
        return {
            "status": "active" if paid_days >= 0 else "expired",
            "days_remaining": max(paid_days, 0),
            "end_date": str(invoice_end)[:10],
            "plan_name": plan_name,
        }

    return {"status": "none", "days_remaining": 0,
            "end_date": None, "plan_name": plan_name}


# ────────────────────────────────────────────────────────────────────
# Subscription enforcement helpers
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def register_device(
    email: str,
    device_id: str,
    device_label: Optional[str] = None,
) -> dict[str, Any]:
    """Insert/update a row in the Subscription's RAI Device child table.
    Existing devices are touched (last_seen bumped) — no double-
    registration. New devices that push past the plan's max_devices
    trigger LRU eviction (oldest last_seen wins) rather than a hard
    block, so reinstall scenarios don't lock users out of their own
    subscription.

    No-op (returns ok=False, reason="no_subscription") when the user
    has no Subscription yet — caller should not call this before
    create_signup.
    """
    if not device_id:
        logger.info(f"register_device: no device_id for {email} — skipping")
        return {"ok": True, "skipped": True}

    sub = await _find_subscription(email)
    if not sub:
        logger.warning(f"register_device: no subscription for {email}")
        return {"ok": False, "reason": "no_subscription"}

    limits = await _get_plan_limits(sub["name"])
    max_devices = limits["max_devices"]
    now = _now_iso()
    outcome: dict[str, Any] = {"ok": True, "registered": False}

    def _mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Existing device? Touch last_seen and label.
        for r in rows:
            if r.get("device_id") == device_id:
                r["last_seen"] = now
                if device_label:
                    r["label"] = device_label
                return rows

        # New device — LRU evict if at cap. Spotify-style sliding
        # window so reinstalls don't permanently consume a slot.
        if len(rows) >= max_devices:
            rows.sort(key=lambda r: r.get("last_seen") or "")
            evicted = rows.pop(0)
            logger.info(
                f"register_device: LRU-evicting {evicted.get('device_id')} "
                f"({evicted.get('label')}) for {email} to make room"
            )

        rows.append({
            "device_id": device_id,
            "label": device_label or "Unknown",
            "first_seen": now,
            "last_seen": now,
        })
        outcome["registered"] = True
        return rows

    new_rows = await _modify_subscription_children(
        sub["name"], "rai_registered_devices", _mutate,
    )
    if outcome["registered"]:
        logger.info(
            f"register_device: {email} → {device_id} "
            f"(now {len(new_rows)}/{max_devices})"
        )
    return outcome


def _normalize_url(u: str) -> str:
    """Canonicalize an ERP URL so trailing-slash and case differences
    don't cause spurious binding mismatches."""
    return u.strip().rstrip("/").lower()


async def record_erp_url_binding(email: str, erp_url: str) -> None:
    """Find-or-append a row in the Subscription's RAI URL Binding
    child table. No-op if the URL is already bound. Logs (but does
    not block) when adding the row would exceed the plan's
    max_erp_urls — graduates to a hard 403 in a future phase."""
    if not erp_url:
        return
    sub = await _find_subscription(email)
    if not sub:
        return

    incoming = _normalize_url(erp_url)
    limits = await _get_plan_limits(sub["name"])
    now = _now_iso()
    canonical_url = erp_url.strip().rstrip("/")

    def _mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if any(_normalize_url(r.get("url") or "") == incoming for r in rows):
            return rows  # already bound — no-op
        if len(rows) >= limits["max_erp_urls"]:
            logger.warning(
                f"record_erp_url_binding: {email} adding URL "
                f"{incoming!r} exceeds plan limit "
                f"({len(rows)}/{limits['max_erp_urls']}). "
                f"Not enforced yet (logged-only phase)."
            )
        rows.append({"url": canonical_url, "first_seen": now})
        return rows

    await _modify_subscription_children(
        sub["name"], "rai_bound_urls", _mutate,
    )
    logger.info(f"record_erp_url_binding: {email} → {incoming!r}")


async def record_company_binding(
    email: str, erp_url: str, company: str,
) -> None:
    """Find-or-append a row in the Subscription's RAI Company Binding
    child table. The (erp_url, company) pair uniquely identifies a
    company across multi-ERP plans — a customer with two ERPs can
    have a Company named "Acme Iraq" on each, and they're treated as
    distinct bindings.

    Called from /erp/bind_company after the user picks a company in
    the Flutter app (or auto-fires on single-company ERPs).
    """
    if not (erp_url and company):
        return
    sub = await _find_subscription(email)
    if not sub:
        return

    norm_url = _normalize_url(erp_url)
    incoming_company = company.strip()
    canonical_url = erp_url.strip().rstrip("/")
    limits = await _get_plan_limits(sub["name"])
    now = _now_iso()

    def _mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for r in rows:
            if (
                _normalize_url(r.get("erp_url") or "") == norm_url
                and (r.get("company") or "").strip() == incoming_company
            ):
                return rows  # already bound — no-op
        if len(rows) >= limits["max_companies"]:
            logger.warning(
                f"record_company_binding: {email} adding "
                f"({norm_url!r}, {incoming_company!r}) exceeds plan limit "
                f"({len(rows)}/{limits['max_companies']}). "
                f"Not enforced yet (logged-only phase)."
            )
        rows.append({
            "erp_url": canonical_url,
            "company": incoming_company,
            "first_seen": now,
        })
        return rows

    await _modify_subscription_children(
        sub["name"], "rai_bound_companies", _mutate,
    )
    logger.info(
        f"record_company_binding: {email} → "
        f"({norm_url!r}, {incoming_company!r})"
    )
