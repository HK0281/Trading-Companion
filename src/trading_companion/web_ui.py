"""Serve only compiled, same-origin UI assets; never expose the source tree."""

from pathlib import Path
import re

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


ASSET_ROOT = Path(__file__).resolve().parent / "web_assets"
WEB_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
}


def _asset(root: Path, relative: str) -> Path | None:
    try:
        path = (root / relative).resolve(strict=True)
        return path if path.is_relative_to(root.resolve()) and path.is_file() else None
    except (OSError, RuntimeError):
        return None


def add_web_routes(app, *, asset_root: Path | None = None):
    """The optional root is trusted server configuration for packaging tests only."""
    root = ASSET_ROOT if asset_root is None else asset_root

    @app.get("/", include_in_schema=False)
    def workspace():
        index = _asset(root, "index.html")
        if index is None:
            return HTMLResponse(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<title>需要构建界面</title><h1>Web 界面尚未构建</h1>'
                '<p>在项目根目录执行 <code>pnpm.cmd --dir frontend install --frozen-lockfile</code>，'
                '再执行 <code>pnpm.cmd --dir frontend build</code>，然后刷新。</p>'
                '<p>API 仍可通过 <a href="/docs">接口文档</a>使用。</p></html>',
                status_code=503, headers=WEB_HEADERS,
            )
        return FileResponse(index, media_type="text/html", headers=WEB_HEADERS)

    @app.get("/assets/{filename}", include_in_schema=False)
    def compiled_asset(filename: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.(?:js|css)", filename):
            return JSONResponse({"error": {"code": "asset_not_found"}}, status_code=404, headers=WEB_HEADERS)
        path = _asset(root, "assets/" + filename)
        if path is None:
            return JSONResponse({"error": {"code": "asset_not_found"}}, status_code=404, headers=WEB_HEADERS)
        media_type = "text/javascript" if filename.endswith(".js") else "text/css"
        return FileResponse(path, media_type=media_type, headers=WEB_HEADERS)
