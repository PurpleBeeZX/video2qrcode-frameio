@echo off
chcp 65001 >nul
echo ==========================================
echo   Running Full Test Suite
echo ==========================================
echo.

echo [1/8] test_auth.py
echo ------------------------------------------
python -m pytest tests/test_auth.py -v --tb=short
echo.

echo [2/8] test_config.py
echo ------------------------------------------
python -m pytest tests/test_config.py -v --tb=short
echo.

echo [3/8] test_endpoints.py
echo ------------------------------------------
python -m pytest tests/test_endpoints.py -v --tb=short
echo.

echo [4/8] test_processing.py
echo ------------------------------------------
python -m pytest tests/test_processing.py -v --tb=short
echo.

echo [5/8] test_upload.py
echo ------------------------------------------
python -m pytest tests/test_upload.py -v --tb=short
echo.

echo [6/8] test_utils.py
echo ------------------------------------------
python -m pytest tests/test_utils.py -v --tb=short
echo.

echo [7/8] test_ssl.py
echo ------------------------------------------
python -m pytest tests/test_ssl.py -v --tb=short
echo.

echo [8/8] Coverage Summary
echo ------------------------------------------
python -m pytest tests/ --cov=main --cov-report=term
echo.
echo ==========================================
echo   All test modules complete
echo ==========================================
