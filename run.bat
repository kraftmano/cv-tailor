@echo off
cd /d "%~dp0"
echo Starting CV Tailor...
echo.
echo Opening in your browser at http://localhost:8501
echo Press Ctrl+C to stop.
echo.
streamlit run app.py --server.headless false
pause
