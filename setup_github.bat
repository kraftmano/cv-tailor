@echo off
echo ==============================
echo CV Tailor - GitHub Setup
echo ==============================
echo.
echo Step 1: Logging in to GitHub...
"C:\Program Files\GitHub CLI\gh.exe" auth login --web --git-protocol https
echo.
echo Step 2: Creating private GitHub repository...
"C:\Program Files\GitHub CLI\gh.exe" repo create cv-tailor --private --source . --remote origin --push
echo.
echo Done! Your repository is now on GitHub.
echo.
"C:\Program Files\GitHub CLI\gh.exe" repo view --web
pause
