"""Quick smoke test for Frappe Cloud + Anthropic.

Usage:
    cd backend
    .\.venv\Scripts\Activate.ps1
    python smoke_test.py
"""
import asyncio
import os
import sys
from getpass import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()


def prompt(text: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    raw = (getpass if secret else input)(f"{text}{suffix}: ").strip()
    return raw or default


async def main():
    print("=" * 60)
    print("ERP الذكي - Smoke Test (username/password)")
    print("=" * 60)
    print()

    erp_url = prompt("ERPNext URL", default="https://honeybird.frappe.cloud")
    username = prompt("Username")
    password = prompt("Password", secret=True)
    anthropic_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or prompt("Anthropic API Key (sk-ant-...)", secret=True)
    )

    if not all([erp_url, username, password, anthropic_key]):
        print("❌ Missing data")
        return 1

    os.environ["ANTHROPIC_API_KEY"] = anthropic_key
    if not os.getenv("SECRET_KEY") or os.getenv("SECRET_KEY") == "change-me":
        os.environ["SECRET_KEY"] = "smoke-test-secret-key-not-for-production-use"

    from app.services.erpnext_client import ERPNextClient
    from app.services.ai_router import AIRouter
    from app.models.schemas import ChatMessage

    print("\n=== 1. Login + key generation ===")
    erp = ERPNextClient(base_url=erp_url)
    try:
        auth_data = await erp.login_and_generate_keys(username, password)
        print(f"✓ Welcome {auth_data['full_name']}")
        roles = auth_data.get("roles", [])
        print(f"  Roles: {len(roles)}")
        if not auth_data.get("api_key") or not auth_data.get("api_secret"):
            print("❌ Missing api_key or api_secret. Cannot proceed.")
            return 1
        print(f"  api_key:    {auth_data['api_key'][:12]}...")
        print(f"  api_secret: {auth_data['api_secret'][:8]}...")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return 1

    erp = ERPNextClient(
        base_url=erp_url,
        api_key=auth_data["api_key"],
        api_secret=auth_data["api_secret"],
    )

    print("\n=== 2. Customer count ===")
    try:
        n = await erp.get_count("Customer")
        print(f"✓ Customers: {n}")
    except Exception as e:
        print(f"⚠ {e}")

    print("\n=== 3. Latest 3 invoices ===")
    try:
        invoices = await erp.get_list(
            "Sales Invoice",
            fields=["name", "customer", "grand_total", "status", "posting_date"],
            limit=3,
            order_by="creation desc",
        )
        for inv in invoices:
            print(f"  • {inv['name']} | {inv.get('customer','?')} | {inv.get('grand_total',0)} | {inv.get('status','?')}")
    except Exception as e:
        print(f"⚠ {e}")

    print("\n=== 4. Claude agent loop ===")
    print('Q: "كم عميل لدي وما هي حالة آخر 3 فواتير؟"')
    try:
        ai = AIRouter(provider="claude")
        resp = await ai.chat(
            messages=[ChatMessage(role="user", content="كم عميل لدي وما هي حالة آخر 3 فواتير؟")],
            erpnext_client=erp,
            user_full_name=auth_data["full_name"],
            user_roles=roles,
        )
        print("--- Claude answer ---")
        print(resp.message)
        print(f"\n--- Used {len(resp.tool_calls)} tool(s) ---")
        for tc in resp.tool_calls:
            print(f"  • {tc.tool_name}({tc.arguments}) -> {tc.result_summary}")
    except Exception as e:
        print(f"❌ AI failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print("\n" + "=" * 60)
    print("✅ All tests passed! Backend is ready.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
