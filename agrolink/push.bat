@echo off
REM Pushes this folder to a new, empty GitHub repository.
REM 1. Create an empty repo on github.com (leave "Add a README" unticked).
REM 2. Double-click this file and paste the repo URL when asked.
git --version >nul 2>&1 || (echo Git is not installed. Get it from https://git-scm.com & pause & exit /b 1)
git config user.name >nul 2>&1 || set /p GN=Your name for commits: 
if defined GN git config --global user.name "%GN%"
git config user.email >nul 2>&1 || set /p GE=Your email for commits: 
if defined GE git config --global user.email "%GE%"
set /p URL=Paste your GitHub repository URL: 
git init
git branch -M main
git add .
git commit -m "Initial commit: AgroLink app"
git remote add origin %URL%
git push -u origin main
echo.
echo Done. Refresh your repository page on GitHub.
pause
