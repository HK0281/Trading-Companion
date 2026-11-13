@echo off
setlocal DisableDelayedExpansion
rem Use the project environment without PowerShell activation or policy changes.
pushd "%~dp0"
if errorlevel 1 goto root_error

set "TC_START_EXIT_CODE=1"
if not exist ".venv\Scripts\python.exe" goto missing_python

rem SHIFT scans user options while %%* retains the original arguments for Python.
set "TC_START_HELP="
set "TC_START_DEMO="
set "TC_START_DATA="
:scan_arguments
if [%1]==[] goto configure_defaults
if "%~1"=="--help" set "TC_START_HELP=1"
if "%~1"=="-h" set "TC_START_HELP=1"
rem Include the unambiguous abbreviations accepted by argparse.
for %%D in (--demo --dem --de) do if "%~1"=="%%D" set "TC_START_DEMO=1"
for %%D in (--data --dat --da) do if "%~1"=="%%D" goto scan_data
for %%D in (--data --dat --da) do if "%~1"=="%%D=synthetic" set "TC_START_DATA=synthetic"
for %%D in (--data --dat --da) do if "%~1"=="%%D=tencent" set "TC_START_DATA=tencent"
for %%D in (--data --dat --da) do if "%~1"=="%%D=akshare_eastmoney" set "TC_START_DATA=akshare_eastmoney"
shift
goto scan_arguments

:scan_data
shift
set "TC_START_DATA=%~1"
if not [%1]==[] shift
goto scan_arguments

:configure_defaults
set TC_START_MEMORY_ARGS=--memory-db "data\conversations.sqlite3"
set TC_START_PERSONAL_ARGS=--research-documents events --personal-memory-db "data\personal_memory.sqlite3"
rem Offline modes omit only inherited defaults; explicit options still reach Python.
if "%TC_START_DEMO%"=="1" set "TC_START_MEMORY_ARGS="
if "%TC_START_DEMO%"=="1" set "TC_START_PERSONAL_ARGS="
if "%TC_START_DATA%"=="synthetic" set "TC_START_PERSONAL_ARGS="

rem Help does not require a frontend build or model configuration.
if defined TC_START_HELP goto run_server
if exist "src\trading_companion\web_assets\index.html" goto run_server

echo [Trading Companion] Frontend assets are missing. Checking local build tools...
where pnpm.cmd >nul 2>nul
if errorlevel 1 goto missing_pnpm
if not exist "frontend\node_modules\.bin\vite.cmd" goto missing_frontend_dependencies
call pnpm.cmd --dir frontend build
if errorlevel 1 goto build_error
if not exist "src\trading_companion\web_assets\index.html" goto build_error

:run_server
if defined TC_START_HELP echo [Trading Companion] Metadata trace defaults to data\traces; use --no-trace to disable or --trace-dir PATH to override.
if defined TC_START_HELP echo [Trading Companion] Conversation history defaults to data\conversations.sqlite3; use --no-memory to disable all memory.
if defined TC_START_HELP echo [Trading Companion] Web excerpts default to events; use --research-documents off to disable.
if defined TC_START_HELP echo [Trading Companion] Personal memory defaults to data\personal_memory.sqlite3; use --no-personal-memory to disable it while keeping conversation history.
if defined TC_START_HELP echo [Trading Companion] Demo and synthetic data skip web-excerpt and personal-memory defaults; demo also skips conversation-history defaults.
echo [Trading Companion] Starting the local server...
echo Your browser will open when the server is ready. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -X utf8 -m trading_companion.api_server --open-browser --refresh-stale-directory --trace-dir "data\traces" %TC_START_MEMORY_ARGS% %TC_START_PERSONAL_ARGS% %*
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
