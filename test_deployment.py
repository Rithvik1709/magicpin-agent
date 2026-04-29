#!/usr/bin/env python3
"""
Test Script for Vera Bot Deployment
Run this after deploying to Railway to verify all endpoints work
"""

import requests
import json
from datetime import datetime
import os

# If running under pytest in CI, provide a base_url fixture that reads TEST_BASE_URL.
try:
    import pytest

    @pytest.fixture
    def base_url():
        url = os.environ.get("TEST_BASE_URL")
        if not url:
            pytest.skip("TEST_BASE_URL not set; skipping deployment integration test")
        return url
except Exception:
    # Not running under pytest or fixtures not available; continue for CLI usage
    pass

def test_bot(base_url):
    """Test all 5 required endpoints"""
    
    print("\n" + "="*70)
    print(f"Testing Vera Bot at: {base_url}")
    print("="*70)
    
    # TEST 1: healthz
    print("\n[1/5] Testing GET /v1/healthz")
    try:
        resp = requests.get(f"{base_url}/v1/healthz", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        print(f"✅ Status: {resp.status_code}")
        print(f"   Response: {json.dumps(data, indent=6)}")
        assert data.get("status") == "ok", "Status should be 'ok'"
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False
    
    # TEST 2: metadata
    print("\n[2/5] Testing GET /v1/metadata")
    try:
        resp = requests.get(f"{base_url}/v1/metadata", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        print(f"✅ Status: {resp.status_code}")
        print(f"   Team: {data.get('team_name')}")
        print(f"   Approach: {data.get('approach')}")
        assert data.get("team_name") == "Vera Prime", "Should be Vera Prime"
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False
    
    # TEST 3: context push
    print("\n[3/5] Testing POST /v1/context")
    try:
        contexts = [
            {
                "scope": "category",
                "context_id": "test_restaurants",
                "version": 1,
                "payload": {
                    "slug": "restaurants",
                    "voice": {"tone": "friendly"},
                    "peer_stats": {"avg_rating": 4.3}
                }
            },
            {
                "scope": "merchant",
                "context_id": "test_merchant_1",
                "version": 1,
                "payload": {
                    "category_slug": "test_restaurants",
                    "identity": {
                        "name": "Test Restaurant",
                        "owner_first_name": "Test Owner"
                    }
                }
            },
            {
                "scope": "trigger",
                "context_id": "test_trigger_1",
                "version": 1,
                "payload": {
                    "kind": "perf_dip",
                    "scope": "merchant",
                    "merchant_id": "test_merchant_1",
                    "payload": {"metric": "calls", "delta_pct": "-10"}
                }
            }
        ]
        
        for ctx in contexts:
            resp = requests.post(
                f"{base_url}/v1/context",
                json={**ctx, "delivered_at": datetime.utcnow().isoformat()},
                timeout=5
            )
            resp.raise_for_status()
            assert resp.json().get("accepted"), f"Context {ctx['scope']} not accepted"
            print(f"✅ {ctx['scope']:12} stored (v{ctx['version']})")
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False
    
    # TEST 4: tick (compose)
    print("\n[4/5] Testing POST /v1/tick")
    try:
        resp = requests.post(
            f"{base_url}/v1/tick",
            json={
                "now": datetime.utcnow().isoformat(),
                "available_triggers": ["test_trigger_1"]
            },
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"✅ Status: {resp.status_code}")
        
        actions = data.get("actions", [])
        print(f"   Triggers processed: 1")
        print(f"   Actions generated: {len(actions)}")
        
        if actions:
            action = actions[0]
            print(f"   Merchant: {action.get('merchant_id')}")
            print(f"   Trigger: {action.get('trigger_id')}")
            print(f"   CTA: {action.get('cta')}")
            print(f"   Body: {action.get('body', '')[:60]}...")
            assert action.get("body"), "Action should have body"
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False
    
    # TEST 5: reply
    print("\n[5/5] Testing POST /v1/reply")
    try:
        resp = requests.post(
            f"{base_url}/v1/reply",
            json={
                "conversation_id": "test_conv_1",
                "merchant_id": "test_merchant_1",
                "customer_id": None,
                "from_role": "merchant",
                "message": "Sounds good, let's do it!",
                "turn_number": 1,
                "received_at": datetime.utcnow().isoformat()
            },
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"✅ Status: {resp.status_code}")
        print(f"   Action: {data.get('action')}")
        print(f"   CTA: {data.get('cta')}")
        print(f"   Body: {data.get('body', '')[:60]}...")
        assert data.get("action") in ["send", "end"], "Action should be send or end"
    except Exception as e:
        print(f"❌ FAILED: {e}")
        return False
    
    # SUMMARY
    print("\n" + "="*70)
    print("✅ ALL TESTS PASSED - BOT IS WORKING PERFECTLY")
    print("="*70)
    print("""
Your bot is ready for the judge!

Next steps:
1. Share this URL with the judge
2. The judge will test all 5 endpoints
3. Your bot will handle triggers → compose messages → process replies

Public URL: """ + base_url)
    
    return True

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter your Railway bot URL (e.g., https://mybot.up.railway.app): ").strip()
    
    if not url.startswith("http"):
        url = f"https://{url}"
    
    success = test_bot(url)
    sys.exit(0 if success else 1)
