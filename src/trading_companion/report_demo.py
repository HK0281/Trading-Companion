"""Display only the application-generated facts from the offline Agent demo."""

from trading_companion.agent_demo import run_demo


def main() -> int:
    payload = run_demo()
    report = payload["result"]["safe_answer"]
    print(report["text"])
    return 0 if report["status"] in ("complete", "limited") else 1


if __name__ == "__main__":
    raise SystemExit(main())
