@echo off
chcp 65001 >nul
echo Step 1: Starting OpenClaw...
start "OpenClaw" cmd /c "chcp 65001 >nul && powershell -ExecutionPolicy Bypass -Command ""& 'C:\Users\13632\.stepfun\runtimes\node\install_1769483068832_457v07jhfvf\node-v22.18.0-win-x64\openclaw.ps1' gateway"""
timeout /t 5 /nobreak >nul
echo Step 2: Starting XiaoYi...
cd /d "C:\D\???n8n-coze-dify\skill\skill-main\projects\xiaoyue-web"
if not exist node_modules (
    npm install
)
node server-with-openclaw.js
