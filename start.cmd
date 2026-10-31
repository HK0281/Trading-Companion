@echo off
setlocal DisableDelayedExpansion
rem Use the project environment without PowerShell activation or policy changes.
pushd "%~dp0"
if errorlevel 1 goto root_error

set "TC_START_EXIT_CODE=1"
if not exist ".venv\Scripts\python.exe" goto missing_python

rem Help does not require a frontend build or model configuration.
if "%~1"=="--help" goto run_server
if "%~1"=="-h" goto run_server
if exist "src\trading_companion\web_assets\index.html" goto run_server

echo [Trading Companion] Frontend assets are missing. Checking local build tools...
where pnpm.cmd >nul 2>nul
if errorlevel 1 goto missing_pnpm
if not exist "frontend\node_modules\.bin\vite.cmd" goto missing_frontend_dependencies
call pnpm.cmd --dir frontend build
if errorlevel 1 goto build_error
if not exist "src\trading_companion\web_assets\index.html" goto build_error

:run_server
if "%~1"=="--help" echo [Trading Companion] Metadata trace defaults to data\traces; use --no-trace to disable or --trace-dir PATH to override.
if "%~1"=="-h" echo [Trading Companion] Metadata trace defaults to data\traces; use --no-trace to disable or --trace-dir PATH to override.
if "%~1"=="--help" echo [Trading Companion] Conversation history defaults to data\conversations.sqlite3; use --no-memory to disable.
if "%~1"=="-h" echo [Trading Companion] Conversation history defaults to data\conversations.sqlite3; use --no-memory to disable.
echo [Trading Companion] Starting the local server...
echo Your browser will open when the server is ready. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -X utf8 -m trading_companion.api_server --open-browser --refresh-stale-directory --trace-dir "data\traces" --memory-db "data\conversations.sqlite3" %*
set "TC_START_EXIT_CODE=%errorlevel%"
if not "%TC_START_EXIT_CODE%"=="0" echo [ERROR] Startup failed. Check the server message above.
goto finish

:missing_python
echo [ERROR] The project Python environment was not found.
echo Run these commands from the project folder first:
echo   py -3.11 -m venv .venv
echo   .\.venv\Scripts\python.exe -m pip install -e ".[api,model]"
goto finish

:missing_pnpm
echo [ERROR] The frontend has not been built and pnpm.cmd is not on PATH.
echo Install Node.js 22.12+ and pnpm 11.25.0, then run:
echo   pnpm.cmd --dir frontend install --frozen-lockfile
echo   pnpm.cmd --dir frontend build
goto finish

:missing_frontend_dependencies
echo [ERROR] Frontend dependencies are missing. Run:
echo   pnpm.cmd --dir frontend install --frozen-lockfile
echo Then run this script again.
goto finish

:build_error
echo [ERROR] The frontend build failed. See the build output above.
goto finish

:finish
popd
rem Keep a double-clicked window open on errors; automation can opt out.
if not "%TC_START_EXIT_CODE%"=="0" if not defined TRADING_COMPANION_NO_PAUSE pause
exit /b %TC_START_EXIT_CODE%

:root_error
echo [ERROR] The project folder could not be opened.
if not defined TRADING_COMPANION_NO_PAUSE pause
exit /b 1
