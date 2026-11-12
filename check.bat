@echo off
setlocal
cd /d "%~dp0"

set "PROJECT_ROOT=%CD%"
set "PYBIN=%PROJECT_ROOT%\.venv\Scripts\python.exe"

if exist "%PYBIN%" goto python_ready
echo [check] ERROR: .venv\Scripts\python.exe was not found.
echo [check] Create the environment and install backend requirements first.
exit /b 1

:python_ready
echo ============================================================
echo [1/3] Backend regression suite
echo ============================================================
pushd backend
"%PYBIN%" -m pytest -q
if not "%ERRORLEVEL%"=="0" goto backend_failed
popd

echo ============================================================
echo [2/3] Evaluation contract suite
echo ============================================================
"%PYBIN%" -m pytest tests\eval_refund -q
if not "%ERRORLEVEL%"=="0" goto eval_failed

echo ============================================================
echo [3/3] Frontend production build
echo ============================================================
pushd frontend
call npm run build
if not "%ERRORLEVEL%"=="0" goto frontend_failed
popd

echo.
echo [check] ALL PASS
endlocal
exit /b 0

:backend_failed
popd
echo [check] FAILED: backend regression suite
endlocal
exit /b 1

:eval_failed
echo [check] FAILED: evaluation contract suite
endlocal
exit /b 1

:frontend_failed
popd
echo [check] FAILED: frontend production build
endlocal
exit /b 1
