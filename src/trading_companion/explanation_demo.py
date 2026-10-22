"""Print facts and checked relations only, using synthetic CSV and a demo model."""

from trading_companion.agent_demo import run_demo


def main() -> int:
    payload = run_demo()
    result = payload["result"]
    print(result["display_answer"])
    return 0 if result["explanation_validation"]["status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
