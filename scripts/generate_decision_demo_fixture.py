"""Export the checked v3 company answer through the real offline demo HTTP entry."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from trading_companion.api import create_app, create_demo_service


OUTPUT = Path(__file__).resolve().parents[1] / "frontend/tests/decisionFixture.json"


def capture_demo_turn():
    from trading_companion.decision_demo import QUESTION
    with TestClient(create_app(service=create_demo_service()), base_url="http://localhost") as client:
        credentials = client.post("/v1/sessions", json={}).json()
        response = client.post(f'/v1/sessions/{credentials["session_id"]}/messages',
            headers={"Authorization": "Bearer " + credentials["access_token"]},
            json={"request_id": "d" * 32, "question": QUESTION})
        response.raise_for_status()
        if response.json()['result']['code'] != 'decision_horizon_required':
            raise ValueError('Demo must first ask for the holding horizon')
        response = client.post(f'/v1/sessions/{credentials["session_id"]}/messages',
            headers={"Authorization": "Bearer " + credentials["access_token"]},
            json={"request_id": "e" * 32, "question": "中线"})
        response.raise_for_status()
        return response.json()["result"]


def main():
    # Explicit fixture maintenance only; no model/data network or credential reads.
    from scripts.verify import clean_environment, offline_network
    with clean_environment(), offline_network():
        turn = capture_demo_turn()
    company = turn["company_research"]
    if (company["version"] != "product-research-answer-v3" or company["quality"]["state"] != "complete"
            or company["quality"]["deleted_sentences"] != 0):
        raise ValueError("Demo answer did not pass the production narrative checks")
    OUTPUT.write_text(json.dumps(company, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("Exported checked offline demo answer to frontend/tests/decisionFixture.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
