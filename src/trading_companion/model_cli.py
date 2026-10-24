import argparse
import json

from trading_companion.deepseek_model import DeepSeekConfig, DeepSeekTextModel
from trading_companion.local_env import load_model_environment
from trading_companion.model import (
    ChatMessage,
    FakeTextModel,
    ModelCallError,
    ModelConfigurationError,
    ModelRequestError,
    TextModel,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test the text model layer; this is not the trading agent.")
    parser.add_argument("prompt", help="A short test prompt; do not include secrets or account information")
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake",
                        help="fake is offline; deepseek sends this prompt to a paid external API")
    args = parser.parse_args(argv)
    try:
        model: TextModel
        model = FakeTextModel() if args.provider == "fake" else DeepSeekTextModel(
            DeepSeekConfig.from_env(load_model_environment()))
        result = model.complete([
            ChatMessage("system", "This is a connectivity test, not market analysis. Do not claim to fetch data or execute actions."),
            ChatMessage("user", args.prompt),
        ])
    except (ModelConfigurationError, ModelRequestError) as error:
        print(json.dumps({"status": "invalid_request", "message": str(error)}, ensure_ascii=False))
        return 2
    except ModelCallError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", **result.to_dict()}, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
