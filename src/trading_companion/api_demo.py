"""Fixed in-process HTTP walkthrough; no listening socket, credential reads or real data."""

import json
from uuid import uuid4


def main() -> int:
    try:
        from fastapi.testclient import TestClient
        from trading_companion.api import create_app
        from trading_companion.followup_demo import QUESTION, FOLLOWUP
    except ImportError:
        print("Install .[api,dev] first.")
        return 2
    checks = {}
    with TestClient(create_app(), base_url="http://localhost") as client:
        created = client.post("/v1/sessions", json={})
        credentials = created.json()
        path = "/v1/sessions/" + credentials["session_id"]
        headers = {"Authorization": "Bearer " + credentials["access_token"]}
        key = uuid4().hex
        payload = {"question": QUESTION, "request_id": key}
        research = client.post(path + "/messages", headers=headers, json=payload)
        replay = client.post(path + "/messages", headers=headers, json=payload)
        followup = client.post(path + "/messages", headers=headers, json={"request_id": uuid4().hex, "question": FOLLOWUP})
        history = client.get(path + "/result", headers=headers)
        reset = client.post(path + "/reset", headers=headers, json={})
        retired = client.post(path + "/messages", headers=headers, json=payload)
        deleted = client.delete(path, headers=headers)
        checks = {
            "session_created": created.status_code == 201,
            "research_completed": research.status_code == 200 and research.json()["result"]["code"] == "company_research_completed",
            "duplicate_replayed": replay.status_code == 200 and replay.json()["replayed"],
            "followup_answered": followup.status_code == 200 and followup.json()["result"]["code"] == "product_followup_answered",
            "history_without_model": history.status_code == 200 and history.json()["result"]["model_calls_attempted"] == 0,
            "reset_without_refund": reset.status_code == 200 and reset.json()["session"]["model_calls_forwarded"] == 3,
            "old_id_retired": retired.status_code == 409,
            "session_deleted": deleted.status_code == 204,
        }
    passed = all(checks.values())
    print(json.dumps({"status": "passed" if passed else "failed", "is_mock": True,
        "scope": "in_process_http_contracts_not_live_network_or_model_quality", "market_data_requested": False,
        "model_calls_forwarded": 3, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
