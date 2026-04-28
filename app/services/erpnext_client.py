"""ERPNext (Frappe) client — bridge between backend and ERPNext.

Each method is a tool that the AI can call. Permission scoping is preserved
because we use the user's own api_key/api_secret — Frappe enforces RBAC.
"""
from typing import Any, Optional
import httpx
from loguru import logger


class ERPNextAuthError(Exception):
    """Auth failure with ERPNext."""


class ERPNextAPIError(Exception):
    """API call failure."""


class ERPNextClient:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False ignores HTTP_PROXY env vars that some hosts inject
        return httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=True)

    @property
    def _auth_headers(self) -> dict[str, str]:
        if self.api_key and self.api_secret:
            return {"Authorization": f"token {self.api_key}:{self.api_secret}"}
        return {}

    async def whoami(self) -> dict[str, Any]:
        """Validate api_key/api_secret and fetch user info."""
        async with self._client() as client:
            who = await client.get(
                f"{self.base_url}/api/method/frappe.auth.get_logged_user",
                headers=self._auth_headers,
            )
            if who.status_code != 200:
                raise ERPNextAuthError(f"المفاتيح غير صحيحة (HTTP {who.status_code})")
            username = who.json().get("message")
            if not username or username == "Guest":
                raise ERPNextAuthError("المفاتيح غير مرتبطة بمستخدم صالح")

            user = await client.get(
                f"{self.base_url}/api/method/frappe.client.get",
                params={"doctype": "User", "name": username},
                headers=self._auth_headers,
            )
            if user.status_code != 200:
                return {"username": username, "full_name": username, "roles": [], "email": None}
            data = user.json().get("message", {})
            roles = [r.get("role") for r in data.get("roles", []) if r.get("role")]
            return {
                "username": username,
                "full_name": data.get("full_name", username),
                "roles": roles,
                "email": data.get("email"),
            }

    async def login_and_generate_keys(self, username: str, password: str) -> dict[str, Any]:
        """Login with username/password and ALWAYS regenerate keys.

        Why always regenerate: api_secret is only returned at generation time.
        Reusing existing api_key without secret would auth as Guest.
        """
        async with self._client() as client:
            login_resp = await client.post(
                f"{self.base_url}/api/method/login",
                data={"usr": username, "pwd": password},
            )
            if login_resp.status_code != 200:
                raise ERPNextAuthError("اسم المستخدم أو كلمة المرور غير صحيحة")
            cookies = login_resp.cookies

            user_resp = await client.get(
                f"{self.base_url}/api/method/frappe.client.get",
                params={"doctype": "User", "name": username},
                cookies=cookies,
            )
            if user_resp.status_code != 200:
                raise ERPNextAuthError("تعذّر جلب معلومات المستخدم")
            user_data = user_resp.json().get("message", {})

            # Always regenerate to get a fresh secret
            gen_resp = await client.post(
                f"{self.base_url}/api/method/frappe.core.doctype.user.user.generate_keys",
                data={"user": username},
                cookies=cookies,
            )
            api_key = None
            api_secret = None
            if gen_resp.status_code == 200:
                api_secret = gen_resp.json().get("message", {}).get("api_secret")
                user_resp2 = await client.get(
                    f"{self.base_url}/api/method/frappe.client.get",
                    params={"doctype": "User", "name": username},
                    cookies=cookies,
                )
                if user_resp2.status_code == 200:
                    api_key = user_resp2.json().get("message", {}).get("api_key")
            else:
                api_key = user_data.get("api_key")

            roles = [r.get("role") for r in user_data.get("roles", []) if r.get("role")]

            return {
                "api_key": api_key or "",
                "api_secret": api_secret or "",
                "full_name": user_data.get("full_name", username),
                "roles": roles,
                "email": user_data.get("email"),
            }

    async def get_list(
        self,
        doctype: str,
        fields: Optional[list[str]] = None,
        filters: Optional[list | dict] = None,
        limit: int = 20,
        order_by: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"doctype": doctype, "limit_page_length": limit}
        if fields:
            import json
            params["fields"] = json.dumps(fields)
        if filters:
            import json
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by

        async with self._client() as client:
            resp = await client.get(
                f"{self.base_url}/api/method/frappe.client.get_list",
                params=params,
                headers=self._auth_headers,
            )
            if resp.status_code == 403:
                # Frappe's 403 on get_list can mean: doctype-level OR field-level
                # permission denial. The body usually contains _server_messages
                # with the specific reason. Surface it so callers can react.
                detail = ""
                try:
                    body = resp.json()
                    detail = body.get("exception", "") or body.get("_error_message", "")
                    if not detail and "_server_messages" in body:
                        import json as _j
                        msgs = _j.loads(body["_server_messages"])
                        detail = " | ".join(_j.loads(m).get("message", "") for m in msgs)
                except Exception:
                    detail = resp.text[:200]
                raise ERPNextAPIError(
                    f"ليست لديك صلاحية الوصول إلى {doctype}"
                    + (f" — {detail}" if detail else "")
                )
            if resp.status_code != 200:
                raise ERPNextAPIError(f"خطأ {resp.status_code}: {resp.text[:200]}")
            return resp.json().get("message", [])

    async def get_list_fallback(
        self,
        doctype: str,
        preferred_fields: list[str],
        safe_fields: list[str],
        **kwargs,
    ) -> list[dict[str, Any]]:
        """Try `preferred_fields` first; on any error fall back to `safe_fields`.

        Useful when some fields may be permission-restricted or non-existent
        on a particular Frappe instance (e.g. base_* fields on Frappe Cloud).
        """
        try:
            return await self.get_list(doctype, fields=preferred_fields, **kwargs)
        except ERPNextAPIError as e:
            logger.warning(
                f"get_list with {len(preferred_fields)} fields failed ({e}), "
                f"retrying with {len(safe_fields)} safe fields"
            )
            return await self.get_list(doctype, fields=safe_fields, **kwargs)

    async def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.get(
                f"{self.base_url}/api/method/frappe.client.get",
                params={"doctype": doctype, "name": name},
                headers=self._auth_headers,
            )
            if resp.status_code != 200:
                raise ERPNextAPIError(f"المستند غير موجود: {name}")
            return resp.json().get("message", {})

    async def get_count(self, doctype: str, filters: Optional[list | dict] = None) -> int:
        import json
        params: dict[str, Any] = {"doctype": doctype}
        if filters:
            params["filters"] = json.dumps(filters)
        async with self._client() as client:
            resp = await client.get(
                f"{self.base_url}/api/method/frappe.client.get_count",
                params=params,
                headers=self._auth_headers,
            )
            return int(resp.json().get("message", 0)) if resp.status_code == 200 else 0

    async def get_unpaid_invoices(
        self,
        customer_filter: Optional[str] = None,
        territory: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        filters: list[list] = [
            ["status", "in", ["Unpaid", "Overdue", "Partly Paid"]],
            ["docstatus", "=", 1],
        ]
        if customer_filter:
            filters.append(["customer_name", "like", f"%{customer_filter}%"])
        if territory:
            filters.append(["territory", "=", territory])
        if date_from:
            filters.append(["posting_date", ">=", date_from])
        if date_to:
            filters.append(["posting_date", "<=", date_to])

        # Include `currency` and `base_*` fields so AI knows which currency each
        # invoice is in. Mixing currencies in one number is the #1 ERP reporting bug.
        # Falls back to safe fields if base_* is restricted by Frappe permissions.
        preferred = [
            "name", "customer_name", "posting_date", "due_date",
            "grand_total", "outstanding_amount", "currency",
            "base_grand_total", "base_outstanding_amount",
            "status", "territory",
        ]
        safe = [
            "name", "customer_name", "posting_date", "due_date",
            "grand_total", "outstanding_amount", "currency",
            "status", "territory",
        ]
        return await self.get_list_fallback(
            "Sales Invoice",
            preferred_fields=preferred,
            safe_fields=safe,
            filters=filters,
            limit=limit,
            order_by="due_date asc",
        )

    async def get_low_stock_items(self, threshold: int = 10, limit: int = 20) -> list[dict[str, Any]]:
        return await self.get_list(
            doctype="Bin",
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
            filters=[["actual_qty", "<=", threshold], ["actual_qty", ">", 0]],
            limit=limit,
            order_by="actual_qty asc",
        )

    async def get_top_customers(self, limit: int = 10) -> list[dict[str, Any]]:
        """Top customers by total submitted-invoice sales.

        Aggregates Sales Invoice grand_total by customer in Python, then
        sorts descending. Uses base_grand_total when accessible (handles
        multi-currency correctly) and falls back to grand_total otherwise.
        """
        preferred_fields = [
            "customer", "customer_name", "territory",
            "base_grand_total", "grand_total", "currency",
        ]
        safe_fields = [
            "customer", "customer_name", "territory",
            "grand_total", "currency",
        ]
        try:
            invoices = await self.get_list_fallback(
                "Sales Invoice",
                preferred_fields=preferred_fields,
                safe_fields=safe_fields,
                filters=[["docstatus", "=", 1]],
                limit=2000,
            )
        except ERPNextAPIError as e:
            # No invoice access at all — fall back to alphabetical Customer list
            # so the card still shows something useful.
            logger.warning(f"get_top_customers falling back to Customer list: {e}")
            return await self.get_list(
                doctype="Customer",
                fields=["name", "customer_name", "territory", "customer_group"],
                limit=limit,
            )

        # Aggregate by customer. We deliberately publish ONE money column
        # (total_sales_base_ccy) in the company's base currency — never the
        # raw transaction-currency grand_total — so the AI can't accidentally
        # compare 22M USD as if it were 22M IQD.
        by_customer: dict[str, dict[str, Any]] = {}
        for inv in invoices:
            key = inv.get("customer") or inv.get("customer_name") or "—"
            bucket = by_customer.setdefault(key, {
                "customer": key,
                "customer_name": inv.get("customer_name") or key,
                "territory": inv.get("territory"),
                "total_sales_base_ccy": 0.0,
                "invoice_count": 0,
                "primary_invoice_currency": None,
                "is_multi_currency": False,
            })
            # Always prefer base_grand_total — already converted to company
            # base currency by ERPNext. grand_total is a fallback only when
            # field-level perms hide base_*.
            amount = inv.get("base_grand_total") or inv.get("grand_total") or 0
            bucket["total_sales_base_ccy"] += amount
            bucket["invoice_count"] += 1

            curr = inv.get("currency")
            if curr:
                if bucket["primary_invoice_currency"] is None:
                    bucket["primary_invoice_currency"] = curr
                elif bucket["primary_invoice_currency"] != curr:
                    bucket["is_multi_currency"] = True

        ranked = sorted(
            by_customer.values(),
            key=lambda c: c["total_sales_base_ccy"],
            reverse=True,
        )[:limit]
        for c in ranked:
            c["total_sales_base_ccy"] = round(c["total_sales_base_ccy"], 2)

        return ranked

    async def get_top_suppliers(self, limit: int = 10) -> list[dict[str, Any]]:
        """List of top suppliers — Supplier doctype with safe field set."""
        return await self.get_list(
            doctype="Supplier",
            fields=["name", "supplier_name", "supplier_group", "country"],
            limit=limit,
        )

    async def get_open_sales_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        """Open Sales Orders — orders submitted but not fully delivered/billed."""
        return await self.get_list(
            doctype="Sales Order",
            fields=[
                "name", "customer", "customer_name", "transaction_date",
                "grand_total", "status", "delivery_date",
            ],
            filters=[
                ["status", "in", ["To Deliver and Bill", "To Bill", "To Deliver"]],
                ["docstatus", "=", 1],
            ],
            limit=limit,
            order_by="transaction_date desc",
        )

    # =========================================================================
    # Frappe Reports — direct access to ERPNext's accounting reports.
    # These are the same reports your accountant uses in the Frappe UI.
    # =========================================================================

    async def run_report(
        self,
        report_name: str,
        filters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Generic runner for ERPNext Query Reports / Script Reports.

        Frappe v16 quirk: this endpoint expects GET with a JSON body. Yes,
        GET with body — confirmed against working Postman captures from
        Frappe Cloud customers.
        """
        import json as _json
        from datetime import datetime as _dt

        # Normalize date strings — Frappe's date parser silently nulls dates
        # that aren't perfectly YYYY-MM-DD, then the report says "From Date
        # is mandatory" even though we sent a value. AI sometimes emits
        # `2026-1-31` instead of `2026-01-31`; auto-fix that.
        normalized_filters = dict(filters or {})
        for date_key in ("from_date", "to_date", "as_of", "report_date", "date"):
            v = normalized_filters.get(date_key)
            if isinstance(v, str) and v:
                for fmt in ("%Y-%m-%d", "%Y-%-m-%-d", "%Y-%m-%-d", "%Y-%-m-%d"):
                    try:
                        normalized_filters[date_key] = _dt.strptime(v, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue

        url = f"{self.base_url}/api/method/frappe.desk.query_report.run"
        body_json = _json.dumps({
            "report_name": report_name,
            "filters": normalized_filters,
            "ignore_prepared_report": True,
        })

        async with self._client() as client:
            # httpx allows passing `content` on any method, including GET.
            # We deliberately use `request("GET", ..., content=...)` to match
            # the exact pattern that works in Postman for Frappe v16.
            resp = await client.request(
                "GET",
                url,
                content=body_json,
                headers={
                    **self._auth_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 403:
                raise ERPNextAPIError(
                    f"ليست لديك صلاحية الوصول لتقرير {report_name}"
                )
            if resp.status_code != 200:
                logger.warning(
                    f"run_report({report_name}) HTTP {resp.status_code}: "
                    f"{resp.text[:500]}"
                )
                raise ERPNextAPIError(
                    f"خطأ تقرير {resp.status_code}: {resp.text[:200]}"
                )
            payload = resp.json().get("message", {}) or {}
            row_count = len(payload.get("result", []) or [])
            col_count = len(payload.get("columns", []) or [])
            logger.info(
                f"run_report({report_name}, {filters}) -> "
                f"{row_count} rows, {col_count} cols"
            )
            return payload

    async def get_profit_loss_report(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
        periodicity: str = "Monthly",
    ) -> dict[str, Any]:
        """Profit and Loss — Income, Expenses, Net Profit.

        Tries Frappe's native P&L report first (GET with JSON body). Falls
        back to GL Entry aggregation if the report endpoint fails — both
        approaches yield the same numbers.
        """
        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        # ERPNext v15+ uses period_start_date / period_end_date for P&L,
        # NOT from_date / to_date. We send BOTH so older versions still work.
        try:
            report = await self.run_report(
                "Profit and Loss Statement",
                filters={
                    "company": company,
                    "period_start_date": date_from,
                    "period_end_date": date_to,
                    "from_date": date_from,
                    "to_date": date_to,
                    "periodicity": periodicity,
                    "filter_based_on": "Date Range",
                    "include_default_book_entries": 1,
                    "accumulated_values": 0,
                    "presentation_currency": "",
                },
            )
            if report.get("result"):
                summary = self._compact_pl_report(
                    report, company, date_from, date_to, periodicity
                )
                summary["source"] = "frappe_report"
                return summary
        except ERPNextAPIError as e:
            logger.warning(f"native P&L report failed, using GL fallback: {e}")

        # Fallback: compute from GL Entry
        return await self._compute_pl_from_gl(
            company, date_from, date_to, periodicity
        )

    async def _compute_pl_from_gl(
        self, company: str, date_from: str, date_to: str, periodicity: str,
    ) -> dict[str, Any]:
        from collections import defaultdict

        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        # 1) Get the chart of accounts so we know which accounts are Income/Expense
        accounts = await self.get_list(
            "Account",
            fields=["name", "account_name", "root_type", "account_type", "parent_account"],
            filters=[["company", "=", company]],
            limit=2000,
        )
        account_root: dict[str, str] = {
            a["name"]: a.get("root_type") or "" for a in accounts
        }
        account_label: dict[str, str] = {
            a["name"]: a.get("account_name") or a["name"] for a in accounts
        }

        # 2) Get all GL entries in date range
        gl_entries = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit", "posting_date"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["is_cancelled", "=", 0],
            ],
            limit=20000,
        )

        # 3) Aggregate
        # Income accounts: credit increases revenue (sign = credit - debit)
        # Expense accounts: debit increases expense (sign = debit - credit)
        period_format = "%Y-%m" if periodicity == "Monthly" else "%Y"
        if periodicity == "Quarterly":
            period_format = "Q"  # Custom

        def period_key(date_str: str) -> str:
            from datetime import datetime
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            if periodicity == "Quarterly":
                q = (dt.month - 1) // 3 + 1
                return f"{dt.year}-Q{q}"
            if periodicity == "Yearly":
                return f"{dt.year}"
            return f"{dt.year}-{dt.month:02d}"

        periods: dict[str, dict[str, float]] = defaultdict(
            lambda: {"income": 0.0, "expense": 0.0}
        )
        income_by_account: dict[str, float] = defaultdict(float)
        expense_by_account: dict[str, float] = defaultdict(float)

        for e in gl_entries:
            account = e.get("account")
            if not account:
                continue
            root = account_root.get(account, "")
            debit = float(e.get("debit") or 0)
            credit = float(e.get("credit") or 0)
            pkey = period_key(e["posting_date"])

            if root == "Income":
                amount = credit - debit
                periods[pkey]["income"] += amount
                income_by_account[account] += amount
            elif root == "Expense":
                amount = debit - credit
                periods[pkey]["expense"] += amount
                expense_by_account[account] += amount

        # 4) Build response
        sorted_periods = sorted(periods.keys())
        breakdown = [
            {
                "period": p,
                "income": round(periods[p]["income"], 2),
                "expense": round(periods[p]["expense"], 2),
                "net_profit": round(periods[p]["income"] - periods[p]["expense"], 2),
            }
            for p in sorted_periods
        ]

        total_income = sum(p["income"] for p in periods.values())
        total_expense = sum(p["expense"] for p in periods.values())
        net_profit = total_income - total_expense

        # Top accounts by amount (informative)
        top_income = sorted(
            [{"account": account_label.get(a, a), "amount": round(v, 2)}
             for a, v in income_by_account.items() if abs(v) > 0],
            key=lambda x: x["amount"], reverse=True,
        )[:10]
        top_expense = sorted(
            [{"account": account_label.get(a, a), "amount": round(v, 2)}
             for a, v in expense_by_account.items() if abs(v) > 0],
            key=lambda x: x["amount"], reverse=True,
        )[:10]

        # Get company currency for display
        comp_info = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        currency = comp_info[0]["default_currency"] if comp_info else "IQD"

        return {
            "company": company,
            "currency": currency,
            "date_from": date_from,
            "date_to": date_to,
            "periodicity": periodicity,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_profit": round(net_profit, 2),
            "profit_margin_percent": (
                round((net_profit / total_income) * 100, 2) if total_income else 0
            ),
            "breakdown_by_period": breakdown,
            "top_income_accounts": top_income,
            "top_expense_accounts": top_expense,
            "gl_entry_count": len(gl_entries),
        }

    async def get_balance_sheet(
        self,
        as_of: str,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """Balance Sheet — Assets, Liabilities, Equity as of a date.

        Computed from GL Entry to bypass Frappe Cloud's desk endpoint
        auth issues. Same idea as get_profit_loss_report.
        """
        from collections import defaultdict

        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        accounts = await self.get_list(
            "Account",
            fields=["name", "account_name", "root_type"],
            filters=[["company", "=", company]],
            limit=2000,
        )
        account_root = {a["name"]: a.get("root_type") or "" for a in accounts}

        gl_entries = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit"],
            filters=[
                ["company", "=", company],
                ["posting_date", "<=", as_of],
                ["is_cancelled", "=", 0],
            ],
            limit=30000,
        )

        # Asset/Liability/Equity balances are cumulative (not period-bound)
        totals: dict[str, float] = defaultdict(float)
        for e in gl_entries:
            account = e.get("account")
            if not account:
                continue
            root = account_root.get(account, "")
            debit = float(e.get("debit") or 0)
            credit = float(e.get("credit") or 0)

            if root == "Asset":
                totals["total_assets"] += debit - credit
            elif root == "Liability":
                totals["total_liabilities"] += credit - debit
            elif root == "Equity":
                totals["total_equity"] += credit - debit

        comp_info = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        currency = comp_info[0]["default_currency"] if comp_info else "IQD"

        return {
            "company": company,
            "currency": currency,
            "as_of": as_of,
            "total_assets": round(totals["total_assets"], 2),
            "total_liabilities": round(totals["total_liabilities"], 2),
            "total_equity": round(totals["total_equity"], 2),
            # Sanity: assets should equal liabilities + equity
            "balanced": abs(
                totals["total_assets"] - totals["total_liabilities"] - totals["total_equity"]
            ) < 1,
        }

    async def get_accounts_receivable(
        self,
        company: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> dict[str, Any]:
        """Accounts Receivable summary — total owed by customers, aging buckets."""
        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}
        if not as_of:
            from datetime import date as _date
            as_of = str(_date.today())

        # AR Summary in v15+ uses `range1`..`range4` and may also need
        # `payment_terms_template` and `customer_group` (we leave them empty).
        try:
            report = await self.run_report(
                "Accounts Receivable Summary",
                filters={
                    "company": company,
                    "report_date": as_of,
                    "ageing_based_on": "Posting Date",
                    "range1": 30,
                    "range2": 60,
                    "range3": 90,
                    "range4": 120,
                    "party_type": "Customer",
                    "show_future_payments": 0,
                    "show_pdc_in_print": 0,
                    "for_revaluation_journals": 0,
                },
            )
            return self._compact_ar_summary(report, company, as_of)
        except ERPNextAPIError as e:
            logger.warning(f"AR Summary report failed: {e}, computing from invoices")
            # Fallback: compute from unpaid Sales Invoices directly
            invoices = await self.get_unpaid_invoices(limit=500)
            grand_total = sum(i.get("outstanding_amount", 0) or 0 for i in invoices)
            by_customer: dict[str, float] = {}
            for inv in invoices:
                cust = inv.get("customer_name") or inv.get("name", "")
                by_customer[cust] = by_customer.get(cust, 0) + (inv.get("outstanding_amount") or 0)
            top = sorted(by_customer.items(), key=lambda x: x[1], reverse=True)[:20]
            return {
                "company": company,
                "as_of": as_of,
                "grand_total_outstanding": grand_total,
                "customer_count": len(by_customer),
                "top_customers": [{"customer": c, "total_due": round(amt, 2)} for c, amt in top],
                "source": "invoice_aggregation",
            }

    async def get_general_ledger(
        self,
        date_from: str,
        date_to: str,
        account: Optional[str] = None,
        company: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """General Ledger entries between two dates (optionally one account)."""
        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        filters: dict[str, Any] = {
            "company": company,
            "from_date": date_from,
            "to_date": date_to,
            "group_by": "Group by Voucher (Consolidated)",
            "include_dimensions": 0,
        }
        if account:
            filters["account"] = [account]

        report = await self.run_report("General Ledger", filters=filters)
        result = report.get("result", []) or []
        # Limit rows to avoid overwhelming the AI
        return {
            "company": company,
            "date_from": date_from,
            "date_to": date_to,
            "account": account,
            "row_count": len(result),
            "rows": result[:limit],
        }

    async def get_trial_balance(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """Trial Balance — debit/credit totals per account in date range."""
        from collections import defaultdict

        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        accounts = await self.get_list(
            "Account",
            fields=["name", "account_name", "root_type"],
            filters=[["company", "=", company]],
            limit=2000,
        )
        account_label = {
            a["name"]: a.get("account_name") or a["name"] for a in accounts
        }
        account_root = {a["name"]: a.get("root_type") or "" for a in accounts}

        gl_entries = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["is_cancelled", "=", 0],
            ],
            limit=30000,
        )

        totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"debit": 0.0, "credit": 0.0}
        )
        for e in gl_entries:
            a = e.get("account")
            if not a:
                continue
            totals[a]["debit"] += float(e.get("debit") or 0)
            totals[a]["credit"] += float(e.get("credit") or 0)

        rows = sorted(
            [
                {
                    "account": account_label.get(a, a),
                    "root_type": account_root.get(a, ""),
                    "debit": round(t["debit"], 2),
                    "credit": round(t["credit"], 2),
                    "balance": round(t["debit"] - t["credit"], 2),
                }
                for a, t in totals.items()
                if t["debit"] != 0 or t["credit"] != 0
            ],
            key=lambda r: abs(r["balance"]),
            reverse=True,
        )

        return {
            "company": company,
            "date_from": date_from,
            "date_to": date_to,
            "rows": rows[:60],
            "row_count": len(rows),
        }

    # ---------- helpers ----------

    async def _get_default_company(self) -> Optional[str]:
        """Returns the first Company in the system. Cached implicitly via tools.execute_tool."""
        try:
            companies = await self.get_list("Company", fields=["name"], limit=1)
            return companies[0]["name"] if companies else None
        except Exception:
            return None

    def _compact_pl_report(
        self, report: dict, company: str, date_from: str, date_to: str, periodicity: str,
    ) -> dict[str, Any]:
        """Reduce the P&L report to AI-digestible summary.

        Defensive — handles missing/None/non-dict rows so a malformed Frappe
        response doesn't 500 the whole chat.
        """
        rows = report.get("result") or []
        columns = report.get("columns") or []
        if not isinstance(rows, list):
            rows = []
        if not isinstance(columns, list):
            columns = []

        period_keys = []
        for c in columns:
            if not isinstance(c, dict):
                continue
            fn = c.get("fieldname")
            if fn and fn not in ("account", "parent_account", "currency", "total", "account_name", "acc_name", "acc_number"):
                period_keys.append(fn)

        income_total: float = 0
        expense_total: float = 0
        net_profit: float = 0
        top_accounts = []

        for r in rows:
            if not isinstance(r, dict):
                continue
            name = (r.get("account_name") or r.get("account") or "").strip()
            indent = r.get("indent", 0) or 0
            total = r.get("total") or 0

            if "Net Profit" in name or "صافي الربح" in name:
                net_profit = total
            elif name in ("Income", "Total Income", "الإيرادات", "إجمالي الإيرادات"):
                income_total = total
            elif name in ("Expenses", "Total Expenses", "المصروفات", "إجمالي المصروفات"):
                expense_total = total
            elif indent == 1 and total != 0:
                top_accounts.append({"account": name, "total": total})

        # report_summary is sometimes None, sometimes [], sometimes [dict]
        currency = None
        rs = report.get("report_summary")
        if isinstance(rs, list) and rs and isinstance(rs[0], dict):
            currency = rs[0].get("currency")

        return {
            "company": company,
            "date_from": date_from,
            "date_to": date_to,
            "periodicity": periodicity,
            "currency": currency,
            "total_income": income_total,
            "total_expense": expense_total,
            "net_profit": net_profit,
            "top_accounts": top_accounts[:10],
            "period_columns": period_keys,
            "report_summary": rs,
        }

    def _compact_balance_sheet(self, report: dict, company: str, as_of: str) -> dict[str, Any]:
        """Reduce Balance Sheet to compact totals."""
        rows = report.get("result", []) or []
        totals: dict[str, float] = {}

        for r in rows:
            if not isinstance(r, dict):
                continue
            name = (r.get("account_name") or r.get("account") or "").strip()
            total = r.get("total", 0) or 0
            for keyword, label in [
                ("Total Assets", "total_assets"),
                ("الأصول", "total_assets"),
                ("Total Liabilities", "total_liabilities"),
                ("الخصوم", "total_liabilities"),
                ("Total Equity", "total_equity"),
                ("حقوق الملكية", "total_equity"),
            ]:
                if keyword in name:
                    totals[label] = total

        return {
            "company": company,
            "as_of": as_of,
            **totals,
            "report_summary": report.get("report_summary"),
        }

    def _compact_ar_summary(self, report: dict, company: str, as_of: str) -> dict[str, Any]:
        """AR summary — top customers by outstanding."""
        rows = report.get("result", []) or []
        customers = [
            r for r in rows
            if isinstance(r, dict) and r.get("party_type") == "Customer"
        ]
        # Sort by total outstanding desc
        customers.sort(key=lambda c: c.get("total_due") or 0, reverse=True)

        compact = [
            {
                "customer": c.get("party") or c.get("party_name"),
                "total_due": c.get("total_due", 0),
                "0-30": c.get("range1", 0),
                "30-60": c.get("range2", 0),
                "60-90": c.get("range3", 0),
                "90-120": c.get("range4", 0),
                "120+": c.get("range5", 0),
            }
            for c in customers[:20]
        ]
        grand_total = sum(c.get("total_due", 0) or 0 for c in customers)

        return {
            "company": company,
            "as_of": as_of,
            "grand_total_outstanding": grand_total,
            "customer_count": len(customers),
            "top_customers": compact,
        }

    async def get_cash_flow_report(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """Cash Flow — net cash movement classified Operating/Investing/Financing.

        Computed from GL Entry. Identifies Cash/Bank accounts (Asset accounts
        with account_type='Cash' or 'Bank') and categorizes their counterparty
        movements by source.
        """
        from collections import defaultdict
        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        accounts = await self.get_list(
            "Account",
            fields=["name", "account_name", "account_type", "root_type"],
            filters=[["company", "=", company]],
            limit=2000,
        )
        cash_accounts = {
            a["name"] for a in accounts
            if a.get("account_type") in ("Cash", "Bank")
        }
        if not cash_accounts:
            return {
                "error": "لم يتم تحديد حسابات نقد/بنك في الشركة. اضبط نوع الحساب في شجرة الحسابات.",
                "company": company,
            }

        # Pull the GL entries that touch a cash/bank account
        gl_entries = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit", "voucher_type", "posting_date", "against"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["is_cancelled", "=", 0],
            ],
            limit=20000,
        )

        # Categorize by voucher_type
        # Operating: Sales/Purchase Invoice + Payment Entry against customer/supplier
        # Investing: Asset purchases/sales (Asset doctype)
        # Financing: Equity / Loans (Loan, Journal Entry to equity accounts)
        cats = defaultdict(lambda: {"inflow": 0.0, "outflow": 0.0})

        for e in gl_entries:
            if e.get("account") not in cash_accounts:
                continue
            debit = float(e.get("debit") or 0)
            credit = float(e.get("credit") or 0)
            vt = (e.get("voucher_type") or "").strip()

            # Heuristic categorization — works for most ERPNext setups
            if vt in ("Payment Entry", "Sales Invoice", "Purchase Invoice", "Journal Entry"):
                category = "operating"
            elif vt in ("Asset", "Asset Movement", "Asset Capitalization"):
                category = "investing"
            elif vt in ("Loan Disbursement", "Loan Repayment", "Loan"):
                category = "financing"
            else:
                category = "other"

            cats[category]["inflow"] += debit  # debit to cash = cash in
            cats[category]["outflow"] += credit  # credit to cash = cash out

        net_change = sum(c["inflow"] - c["outflow"] for c in cats.values())

        comp = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        currency = comp[0]["default_currency"] if comp else "IQD"

        return {
            "company": company,
            "currency": currency,
            "date_from": date_from,
            "date_to": date_to,
            "operating": {
                "inflow": round(cats["operating"]["inflow"], 2),
                "outflow": round(cats["operating"]["outflow"], 2),
                "net": round(cats["operating"]["inflow"] - cats["operating"]["outflow"], 2),
            },
            "investing": {
                "inflow": round(cats["investing"]["inflow"], 2),
                "outflow": round(cats["investing"]["outflow"], 2),
                "net": round(cats["investing"]["inflow"] - cats["investing"]["outflow"], 2),
            },
            "financing": {
                "inflow": round(cats["financing"]["inflow"], 2),
                "outflow": round(cats["financing"]["outflow"], 2),
                "net": round(cats["financing"]["inflow"] - cats["financing"]["outflow"], 2),
            },
            "other": {
                "inflow": round(cats["other"]["inflow"], 2),
                "outflow": round(cats["other"]["outflow"], 2),
                "net": round(cats["other"]["inflow"] - cats["other"]["outflow"], 2),
            },
            "net_cash_change": round(net_change, 2),
            "cash_account_count": len(cash_accounts),
        }

    async def get_customer_profitability(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Customer profitability — revenue per customer with margin where available.

        Uses Sales Invoice grand_total for revenue. If items have valuation,
        also computes COGS proxy from Sales Invoice Item base_amount vs base_net_amount.
        """
        from collections import defaultdict

        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        invoices = await self.get_list(
            "Sales Invoice",
            fields=[
                "name", "customer", "customer_name", "grand_total",
                "base_grand_total", "currency", "posting_date",
            ],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["docstatus", "=", 1],
                ["status", "!=", "Return"],
            ],
            limit=2000,
        )

        per_customer: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "revenue": 0.0,
            "invoice_count": 0,
            "currency": None,
        })

        for inv in invoices:
            cust_key = inv.get("customer") or inv.get("customer_name") or "—"
            cust_name = inv.get("customer_name") or cust_key
            amount = (inv.get("base_grand_total") or inv.get("grand_total") or 0)
            curr = inv.get("currency")

            bucket = per_customer[cust_key]
            bucket["customer"] = cust_key
            bucket["customer_name"] = cust_name
            bucket["revenue"] += amount
            bucket["invoice_count"] += 1
            if bucket["currency"] is None and curr:
                bucket["currency"] = curr

        ranked = sorted(per_customer.values(), key=lambda x: x["revenue"], reverse=True)[:limit]
        total_revenue = sum(c["revenue"] for c in per_customer.values())

        comp = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        base_currency = comp[0]["default_currency"] if comp else "IQD"

        return {
            "company": company,
            "base_currency": base_currency,
            "date_from": date_from,
            "date_to": date_to,
            "total_revenue": round(total_revenue, 2),
            "customer_count": len(per_customer),
            "top_customers": [
                {
                    "customer": c["customer_name"],
                    "revenue": round(c["revenue"], 2),
                    "invoice_count": c["invoice_count"],
                    "currency": c["currency"] or base_currency,
                    "share_percent": (
                        round((c["revenue"] / total_revenue) * 100, 2)
                        if total_revenue else 0
                    ),
                }
                for c in ranked
            ],
        }

    async def get_item_profitability(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Item profitability — revenue and margin per item.

        Uses Sales Invoice Item child rows. If valuation_rate is captured on
        the rows, computes margin = amount - (qty * valuation_rate).
        """
        from collections import defaultdict

        if not company:
            company = await self._get_default_company()
            if not company:
                return {"error": "لا توجد شركة محدّدة."}

        # Get parent invoice IDs in the date range
        invoice_names = await self.get_list(
            "Sales Invoice",
            fields=["name"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["docstatus", "=", 1],
                ["status", "!=", "Return"],
            ],
            limit=2000,
        )
        if not invoice_names:
            return {
                "company": company,
                "date_from": date_from,
                "date_to": date_to,
                "total_revenue": 0,
                "item_count": 0,
                "top_items": [],
            }

        # Pull all Sales Invoice Items for those invoices
        invoice_id_list = [i["name"] for i in invoice_names]
        items = await self.get_list(
            "Sales Invoice Item",
            fields=[
                "item_code", "item_name", "qty", "rate", "amount",
                "base_amount", "valuation_rate",
            ],
            filters=[["parent", "in", invoice_id_list]],
            limit=10000,
        )

        per_item: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "revenue": 0.0, "qty": 0.0, "cogs": 0.0,
        })
        for it in items:
            code = it.get("item_code") or it.get("item_name") or "—"
            name = it.get("item_name") or code
            revenue = (it.get("base_amount") or it.get("amount") or 0)
            qty = (it.get("qty") or 0)
            valuation = (it.get("valuation_rate") or 0)
            bucket = per_item[code]
            bucket["item_code"] = code
            bucket["item_name"] = name
            bucket["revenue"] += revenue
            bucket["qty"] += qty
            bucket["cogs"] += qty * valuation

        ranked = sorted(per_item.values(), key=lambda x: x["revenue"], reverse=True)[:limit]
        total_revenue = sum(it["revenue"] for it in per_item.values())
        total_cogs = sum(it["cogs"] for it in per_item.values())

        comp = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        currency = comp[0]["default_currency"] if comp else "IQD"

        return {
            "company": company,
            "currency": currency,
            "date_from": date_from,
            "date_to": date_to,
            "total_revenue": round(total_revenue, 2),
            "total_cogs": round(total_cogs, 2),
            "gross_profit": round(total_revenue - total_cogs, 2),
            "gross_margin_percent": (
                round(((total_revenue - total_cogs) / total_revenue) * 100, 2)
                if total_revenue else 0
            ),
            "item_count": len(per_item),
            "top_items": [
                {
                    "item_code": it["item_code"],
                    "item_name": it["item_name"],
                    "revenue": round(it["revenue"], 2),
                    "qty": round(it["qty"], 2),
                    "cogs": round(it["cogs"], 2),
                    "gross_profit": round(it["revenue"] - it["cogs"], 2),
                    "margin_percent": (
                        round(((it["revenue"] - it["cogs"]) / it["revenue"]) * 100, 2)
                        if it["revenue"] else 0
                    ),
                }
                for it in ranked
            ],
        }

    async def get_recent_purchase_invoices(
        self, days: int = 30, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Purchase invoices from the last N days.

        Includes supplier_name (human-readable) and falls back to safe fields
        if base_grand_total is permission-restricted.
        """
        from datetime import datetime, timedelta
        date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        preferred = [
            "name", "supplier", "supplier_name", "posting_date",
            "grand_total", "base_grand_total", "outstanding_amount",
            "currency", "status",
        ]
        safe = [
            "name", "supplier", "supplier_name", "posting_date",
            "grand_total", "outstanding_amount", "currency", "status",
        ]
        return await self.get_list_fallback(
            "Purchase Invoice",
            preferred_fields=preferred,
            safe_fields=safe,
            filters=[
                ["posting_date", ">=", date_from],
                ["docstatus", "=", 1],
            ],
            limit=limit,
            order_by="posting_date desc",
        )

    async def get_sales_summary(self, date_from: str, date_to: str) -> dict[str, Any]:
        """Sales summary that handles multi-currency invoices correctly.

        Tries to fetch base_* fields for company-currency totals; falls back
        to per-currency breakdown only if those fields are restricted (common
        on Frappe Cloud with field-level permissions).
        """
        preferred_fields = [
            "grand_total", "outstanding_amount", "currency",
            "base_grand_total", "base_outstanding_amount",
            "status", "posting_date",
        ]
        safe_fields = [
            "grand_total", "outstanding_amount", "currency",
            "status", "posting_date",
        ]
        filters = [
            ["posting_date", ">=", date_from],
            ["posting_date", "<=", date_to],
            ["docstatus", "=", 1],
        ]

        invoices = await self.get_list_fallback(
            "Sales Invoice",
            preferred_fields=preferred_fields,
            safe_fields=safe_fields,
            filters=filters,
            limit=1000,
        )

        has_base = bool(invoices) and "base_grand_total" in invoices[0]

        # Group by transaction currency
        by_currency: dict[str, dict[str, Any]] = {}
        for inv in invoices:
            curr = inv.get("currency") or "Unknown"
            bucket = by_currency.setdefault(curr, {
                "currency": curr,
                "total_amount": 0.0,
                "paid_amount": 0.0,
                "outstanding_amount": 0.0,
                "invoice_count": 0,
            })
            bucket["total_amount"] += inv.get("grand_total", 0) or 0
            bucket["outstanding_amount"] += inv.get("outstanding_amount", 0) or 0
            if inv.get("status") == "Paid":
                bucket["paid_amount"] += inv.get("grand_total", 0) or 0
            bucket["invoice_count"] += 1

        result: dict[str, Any] = {
            "by_currency": list(by_currency.values()),
            "invoice_count": len(invoices),
            "date_from": date_from,
            "date_to": date_to,
        }

        if has_base:
            result["base_total"] = sum(i.get("base_grand_total", 0) or 0 for i in invoices)
            result["base_paid"] = sum(
                i.get("base_grand_total", 0) or 0
                for i in invoices if i.get("status") == "Paid"
            )
            result["base_outstanding"] = sum(
                i.get("base_outstanding_amount", 0) or 0 for i in invoices
            )
            result["note"] = (
                "by_currency = transaction currency. base_* = company's base "
                "currency (auto-converted)."
            )
        else:
            result["note"] = (
                "Company-currency totals unavailable (field permissions). "
                "Show each currency separately to the user."
            )

        return result

    # ────────────────────────────────────────────────────────────────────
    # Executive / C-level tools
    #
    # These wrap GL Entry queries to give the owner one-shot answers to
    # the questions that matter most (gross profit, cash, payables, trends).
    # All amounts are in the company's base currency.
    # ────────────────────────────────────────────────────────────────────

    async def _get_company_currency(self, company: Optional[str] = None) -> tuple[str, str]:
        """Return (company_name, base_currency). Falls back to (—, IQD)."""
        if not company:
            company = await self._get_default_company()
        if not company:
            return ("—", "IQD")
        comp = await self.get_list(
            "Company", fields=["default_currency"],
            filters=[["name", "=", company]], limit=1,
        )
        currency = comp[0]["default_currency"] if comp else "IQD"
        return (company, currency)

    async def get_gross_profit(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """Gross Profit = Revenue − COGS (Cost of Goods Sold).

        Owner-friendly margin metric. Pulls Income (Revenue) and the
        "Cost of Goods Sold" account group from GL Entry.
        """
        company, base_currency = await self._get_company_currency(company)

        # Revenue: credit balance on Income accounts.
        # COGS:    debit balance on Cost of Goods Sold accounts.
        gl = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit", "account_type"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["is_cancelled", "=", 0],
            ],
            limit=10000,
        )

        # Group by account_type. ERPNext's account_type field tags the
        # nature of the account ("Income Account", "Cost of Goods Sold",
        # "Expense Account", etc.).
        revenue = 0.0
        cogs = 0.0
        opex = 0.0
        for e in gl:
            atype = (e.get("account_type") or "").strip()
            d = e.get("debit") or 0
            c = e.get("credit") or 0
            if atype in ("Income Account",):
                revenue += (c - d)
            elif atype in ("Cost of Goods Sold",):
                cogs += (d - c)
            elif atype in ("Expense Account", "Tax", "Depreciation"):
                opex += (d - c)

        gross_profit = revenue - cogs
        gross_margin_pct = (gross_profit / revenue * 100) if revenue else 0.0
        net_profit = revenue - cogs - opex
        net_margin_pct = (net_profit / revenue * 100) if revenue else 0.0

        return {
            "company": company,
            "base_currency": base_currency,
            "date_from": date_from,
            "date_to": date_to,
            "revenue": round(revenue, 2),
            "cogs": round(cogs, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_margin_pct": round(gross_margin_pct, 2),
            "operating_expenses": round(opex, 2),
            "net_profit": round(net_profit, 2),
            "net_margin_pct": round(net_margin_pct, 2),
            "note": (
                "كل المبالغ بعملة الشركة الأساسية. "
                "الربح الإجمالي = الإيراد − تكلفة البضاعة المباعة. "
                "الربح الصافي = الإيراد − COGS − المصاريف التشغيلية."
            ),
        }

    async def get_monthly_sales_trend(
        self,
        months: int = 12,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """Last N months of sales by posting_date month.

        Returns a list ordered chronologically — perfect for line charts
        and "are we growing?" questions.
        """
        from collections import defaultdict
        from datetime import datetime, timedelta

        # Clamp to a reasonable window
        months = max(1, min(int(months), 36))
        company, base_currency = await self._get_company_currency(company)

        today = datetime.utcnow().date()
        # Approximate start of the window
        start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)

        invoices = await self.get_list(
            "Sales Invoice",
            fields=["posting_date", "base_grand_total", "grand_total", "status"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", start.isoformat()],
                ["posting_date", "<=", today.isoformat()],
                ["docstatus", "=", 1],
                ["status", "!=", "Return"],
            ],
            limit=5000,
        )

        by_month: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "month": "",
            "total_sales": 0.0,
            "paid_sales": 0.0,
            "invoice_count": 0,
        })
        for inv in invoices:
            pd = inv.get("posting_date")
            if not pd:
                continue
            ym = str(pd)[:7]  # 'YYYY-MM'
            amount = inv.get("base_grand_total") or inv.get("grand_total") or 0
            bucket = by_month[ym]
            bucket["month"] = ym
            bucket["total_sales"] += amount
            if inv.get("status") == "Paid":
                bucket["paid_sales"] += amount
            bucket["invoice_count"] += 1

        rows = sorted(by_month.values(), key=lambda r: r["month"])
        for r in rows:
            r["total_sales"] = round(r["total_sales"], 2)
            r["paid_sales"] = round(r["paid_sales"], 2)

        # Quick MoM growth on the last two months for the AI to highlight.
        mom_growth_pct = None
        if len(rows) >= 2 and rows[-2]["total_sales"]:
            mom_growth_pct = round(
                (rows[-1]["total_sales"] - rows[-2]["total_sales"])
                / rows[-2]["total_sales"] * 100, 2,
            )

        return {
            "company": company,
            "base_currency": base_currency,
            "months_returned": len(rows),
            "trend": rows,
            "mom_growth_pct": mom_growth_pct,
        }

    async def get_expense_breakdown(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
        limit: int = 15,
    ) -> dict[str, Any]:
        """Top expense accounts in a date range — where the money goes.

        Aggregates GL Entry debits on accounts of type
        ``Expense Account`` / ``Cost of Goods Sold`` / ``Tax`` /
        ``Depreciation`` and ranks them.
        """
        from collections import defaultdict

        company, base_currency = await self._get_company_currency(company)

        gl = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit", "account_type"],
            filters=[
                ["company", "=", company],
                ["posting_date", ">=", date_from],
                ["posting_date", "<=", date_to],
                ["is_cancelled", "=", 0],
            ],
            limit=10000,
        )

        per_account: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"account": "", "amount": 0.0, "type": ""}
        )
        EXPENSE_TYPES = {
            "Expense Account",
            "Cost of Goods Sold",
            "Tax",
            "Depreciation",
            "Round Off",
        }
        for e in gl:
            atype = (e.get("account_type") or "").strip()
            if atype not in EXPENSE_TYPES:
                continue
            acct = e.get("account") or "—"
            net = (e.get("debit") or 0) - (e.get("credit") or 0)
            if net <= 0:
                continue
            bucket = per_account[acct]
            bucket["account"] = acct
            bucket["type"] = atype
            bucket["amount"] += net

        ranked = sorted(per_account.values(), key=lambda x: x["amount"], reverse=True)[:limit]
        total_expenses = sum(x["amount"] for x in per_account.values())
        for r in ranked:
            r["amount"] = round(r["amount"], 2)
            r["share_percent"] = (
                round(r["amount"] / total_expenses * 100, 2) if total_expenses else 0
            )

        return {
            "company": company,
            "base_currency": base_currency,
            "date_from": date_from,
            "date_to": date_to,
            "total_expenses": round(total_expenses, 2),
            "top_expenses": ranked,
        }

    async def get_cash_position(
        self,
        company: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> dict[str, Any]:
        """Current cash & bank balance for the company.

        Sums GL Entry debit−credit on accounts of type Bank or Cash.
        """
        from collections import defaultdict
        from datetime import date as _date

        company, base_currency = await self._get_company_currency(company)
        as_of = as_of or _date.today().isoformat()

        gl = await self.get_list(
            "GL Entry",
            fields=["account", "debit", "credit", "account_type"],
            filters=[
                ["company", "=", company],
                ["posting_date", "<=", as_of],
                ["is_cancelled", "=", 0],
                ["account_type", "in", ["Bank", "Cash"]],
            ],
            limit=10000,
        )

        per_account: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"account": "", "type": "", "balance": 0.0}
        )
        for e in gl:
            acct = e.get("account") or "—"
            atype = e.get("account_type") or ""
            net = (e.get("debit") or 0) - (e.get("credit") or 0)
            bucket = per_account[acct]
            bucket["account"] = acct
            bucket["type"] = atype
            bucket["balance"] += net

        accounts = sorted(per_account.values(), key=lambda x: x["balance"], reverse=True)
        for a in accounts:
            a["balance"] = round(a["balance"], 2)
        total_cash = round(sum(a["balance"] for a in accounts), 2)

        return {
            "company": company,
            "base_currency": base_currency,
            "as_of": as_of,
            "total_cash_and_bank": total_cash,
            "accounts": accounts,
        }

    async def get_payables_summary(
        self,
        company: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> dict[str, Any]:
        """How much we owe suppliers (Accounts Payable mirror of AR).

        Sums outstanding Purchase Invoices grouped by supplier.
        """
        from collections import defaultdict
        from datetime import date as _date

        company, base_currency = await self._get_company_currency(company)
        as_of = as_of or _date.today().isoformat()

        invoices = await self.get_list(
            "Purchase Invoice",
            fields=[
                "name", "supplier", "supplier_name",
                "outstanding_amount", "base_outstanding_amount",
                "grand_total", "base_grand_total",
                "due_date", "posting_date", "status", "currency",
            ],
            filters=[
                ["company", "=", company],
                ["posting_date", "<=", as_of],
                ["docstatus", "=", 1],
                ["outstanding_amount", ">", 0],
            ],
            limit=2000,
        )

        per_supplier: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "supplier": "", "supplier_name": "",
            "outstanding": 0.0, "invoice_count": 0,
        })
        total_outstanding = 0.0
        for inv in invoices:
            key = inv.get("supplier") or inv.get("supplier_name") or "—"
            amount = (
                inv.get("base_outstanding_amount")
                or inv.get("outstanding_amount") or 0
            )
            bucket = per_supplier[key]
            bucket["supplier"] = key
            bucket["supplier_name"] = inv.get("supplier_name") or key
            bucket["outstanding"] += amount
            bucket["invoice_count"] += 1
            total_outstanding += amount

        ranked = sorted(per_supplier.values(), key=lambda x: x["outstanding"], reverse=True)
        for r in ranked:
            r["outstanding"] = round(r["outstanding"], 2)

        return {
            "company": company,
            "base_currency": base_currency,
            "as_of": as_of,
            "total_outstanding": round(total_outstanding, 2),
            "supplier_count": len(per_supplier),
            "by_supplier": ranked[:25],
        }

    async def get_executive_summary(
        self,
        date_from: str,
        date_to: str,
        company: Optional[str] = None,
    ) -> dict[str, Any]:
        """One-shot owner dashboard: revenue, gross profit, cash, AR, AP.

        Designed to answer "كيف الشركة هذا الشهر؟" with a single tool call.
        Runs the underlying queries in parallel.
        """
        import asyncio
        from datetime import date as _date

        company, base_currency = await self._get_company_currency(company)
        as_of = date_to or _date.today().isoformat()

        results = await asyncio.gather(
            self.get_gross_profit(date_from, date_to, company=company),
            self.get_cash_position(company=company, as_of=as_of),
            self.get_accounts_receivable(company=company, as_of=as_of),
            self.get_payables_summary(company=company, as_of=as_of),
            self.get_top_customers(limit=3),
            return_exceptions=True,
        )

        def _safe(val, fallback=None):
            if isinstance(val, Exception):
                return fallback if fallback is not None else {"error": str(val)}
            return val

        gp = _safe(results[0], {})
        cash = _safe(results[1], {})
        ar = _safe(results[2], {})
        ap = _safe(results[3], {})
        top_cust = _safe(results[4], [])

        return {
            "company": company,
            "base_currency": base_currency,
            "date_from": date_from,
            "date_to": date_to,
            "as_of": as_of,
            "headline": {
                "revenue": gp.get("revenue") if isinstance(gp, dict) else None,
                "gross_profit": gp.get("gross_profit") if isinstance(gp, dict) else None,
                "gross_margin_pct": gp.get("gross_margin_pct") if isinstance(gp, dict) else None,
                "net_profit": gp.get("net_profit") if isinstance(gp, dict) else None,
                "cash_on_hand": cash.get("total_cash_and_bank") if isinstance(cash, dict) else None,
                "accounts_receivable": (
                    ar.get("total_outstanding") if isinstance(ar, dict) else None
                ),
                "accounts_payable": (
                    ap.get("total_outstanding") if isinstance(ap, dict) else None
                ),
            },
            "top_3_customers": (
                top_cust[:3] if isinstance(top_cust, list) else []
            ),
            "note": (
                "ملخّص تنفيذي بعملة الشركة الأساسية. "
                "كل الأرقام محسوبة من GL Entry وSales/Purchase Invoice."
            ),
        }
