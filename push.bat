@echo off
chcp 65001 >nul
title Addenda-Cache 一键推送
cd /d "%~dp0"

echo ============================================
echo  Addenda-Cache ^→ GitHub 一键推送
echo  仓库: Sycho-Finally/Addenda-Cache (main)
echo ============================================
echo.

git add -A
for /f %%i in ('git status -s ^| find /c /v ""') do set CHANGES=%%i
if "%CHANGES%"=="0" (
    echo [1/2] 没有新的变更需要提交
) else (
    echo [1/2] 发现 %CHANGES% 个文件变更，提交中...
    git commit -m "update: content sync"
)

echo [2/2] 推送到 GitHub...
git -c http.schannelCheckRevoke=false push origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送未成功。排查顺序：
    echo   1. 确认代理/VPN 已开启
    echo   2. 确认 GitHub 仓库 Sycho-Finally/Addenda-Cache 存在且账号已登录
    echo   3. 重试本脚本
) else (
    echo.
    echo [成功] 已推送到 https://github.com/Sycho-Finally/Addenda-Cache
)
echo.
pause
