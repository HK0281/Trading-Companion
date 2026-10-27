"""Loopback-only FastAPI adapter; request DTOs never configure models or data sources."""

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures, load_development_market_context
from trading_companion.followup_demo import ConversationDemoModel
from trading_companion.web_ui import add_web_routes


MAX_BODY_BYTES = 16384
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
SessionPath = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(EmptyRequest):
    provider: Literal["deepseek", "openai"] | None = None


class MessageRequest(EmptyRequest):
    request_id: Identifier = Field(examples=["0123456789abcdef0123456789abcdef"])
    question: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value.strip()


class CancelRequest(EmptyRequest):
    request_id: Identifier


class PublicTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    code: str
    message: str
    stage: str
    answer: str | None
    presentation: dict | None = None
    conversation: dict | None = None
    research_workflow: dict | None = None
    company_research: dict | None = None
    knowledge: dict | None = None
    screening: dict | None = None
    report: dict | None
    parameters: dict | None
    chart: dict | None
    market_context: dict | None
    candidates: list[dict]
    applied_defaults: list[str]
    model_calls_attempted: int
    analyses_used: int
    runtime: dict | None
    validation_scope: str


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Identifier
    replayed: bool
    result: PublicTurn


def _problem(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class _LocalBoundary:
    """Bound upload size without buffering unbounded request bodies or echoing invalid input."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}

        async def safe_send(message):
            if message["type"] == "http.response.start":
                # Mounted development routes pass both boundaries; emit each fixed header once.
                fixed_headers = {b"cache-control", b"x-content-type-options", b"referrer-policy"}
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in fixed_headers]
                message = {**message, "headers": [*headers,
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer")]}
            await send(message)

        origin = headers.get(b"origin")
        host = headers.get(b"host", "")
        if (headers.get(b"sec-fetch-site") == "cross-site"
                or origin is not None and origin not in ("http://" + host, "https://" + host)):
            return await _problem(403, "origin_not_allowed", "仅允许同源本机请求。")(scope, receive, safe_send)
        if scope["method"] in ("POST", "PUT", "PATCH"):
            if headers.get(b"content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return await _problem(415, "json_required", "请发送application/json请求。")(scope, receive, safe_send)
            declared = headers.get(b"content-length")
            if declared is not None:
                if not declared.isascii() or not declared.isdigit():
                    return await _problem(400, "invalid_content_length", "请求长度无效。")(scope, receive, safe_send)
                if len(declared) > 6 or int(declared) > MAX_BODY_BYTES:
                    return await _problem(413, "request_too_large", "请求体最多16KiB。")(scope, receive, safe_send)
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                chunk = event.get("body", b"")
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    return await _problem(413, "request_too_large", "请求体最多16KiB。")(scope, receive, safe_send)
                body.extend(chunk)
                if not event.get("more_body", False):
                    break
            sent = False

            async def buffered_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, buffered_receive, safe_send)
        return await self.app(scope, receive, safe_send)


def create_demo_service() -> ResearchApiService:
    source, _ = load_development_fixtures()
    return ResearchApiService(model_factory=ConversationDemoModel,
        directory_loader=lambda _: load_development_fixtures()[1], development_source=source,
        reference_date=REFERENCE_DATE, market_context_loader=lambda _: load_development_market_context())


def create_app(*, service: ResearchApiService | None = None,
               development_service: ResearchApiService | None = None) -> FastAPI:
    """The product launcher injects normal services; the test factory remains offline."""
    service = create_demo_service() if service is None else service
    if type(service) is not ResearchApiService:
        raise ValueError("Use a trusted application service")
    if development_service is not None and (type(development_service) is not ResearchApiService
            or development_service is service or development_service.provider != "fake"
            or development_service.data_mode != "synthetic"):
        raise ValueError("Use a separate fixture-only development service")

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                service.close()
            finally:
                if development_service is not None:
                    development_service.close()

    app = FastAPI(title="Trading Companion Local API", version="0.1.0", lifespan=lifespan,
        description="Local single-process research only. No trading. Demo is a fixed fake script, not an LLM.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.add_middleware(_LocalBoundary)
    bearer = HTTPBearer(auto_error=False)

    def access_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or len(credentials.credentials) > 128:
            raise ApiProblem(401, "session_token_required", "请在Authorization头中提供会话Bearer令牌。")
        return credentials.credentials

    token_type = Annotated[str, Depends(access_token)]

    @app.exception_handler(ApiProblem)
    async def service_problem(request, error):
        return _problem(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request, error):
        # Default validation details may echo user-supplied secrets or unknown fields.
        return _problem(422, "invalid_request", "请求格式无效；请检查字段、32位小写十六进制编号及1–2000字符问题。")

    @app.get("/health")
    def health():
        return {"status": "ok", "provider": service.provider, "data_mode": service.data_mode,
                "available_providers": list(service.available_providers),
                "scope": "local_single_process_not_production", "trading_enabled": False,
                "development_mode_available": development_service is not None}

    @app.post("/v1/sessions", status_code=201)
    def create_session(body: SessionRequest):
        return service.create_session(provider=body.provider)

    @app.get("/v1/sessions/{session_id}")
    def inspect_session(session_id: SessionPath, token: token_type):
        return service.inspect(session_id, token)

    @app.post("/v1/sessions/{session_id}/messages", response_model=MessageResponse)
    def send_message(session_id: SessionPath, body: MessageRequest, token: token_type):
        # Sync endpoints run in the framework's worker pool, not on the ASGI event loop.
        return service.message(session_id, token, body.request_id, body.question)

    @app.get("/v1/sessions/{session_id}/result", response_model=MessageResponse)
    def view_result(session_id: SessionPath, token: token_type):
        return service.history(session_id, token)

    @app.post("/v1/sessions/{session_id}/cancel", status_code=202)
    def cancel_request(session_id: SessionPath, body: CancelRequest, token: token_type):
        return service.cancel(session_id, token, body.request_id)

    @app.post("/v1/sessions/{session_id}/reset")
    def reset_session(session_id: SessionPath, body: EmptyRequest, token: token_type):
        return service.reset(session_id, token)

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    def delete_session(session_id: SessionPath, token: token_type):
        service.delete(session_id, token)
        return Response(status_code=204)

    if development_service is not None:
        # Select a preconfigured, isolated service by path, never a browser-supplied provider or URL.
        app.mount("/dev-api", create_app(service=development_service), name="development")
    add_web_routes(app)
    return app
