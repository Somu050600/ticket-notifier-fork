@echo off
setlocal
set GIT="C:\Program Files\Git\cmd\git.exe"
cd /d "C:\Users\ANKUR VASHISHTHA\OneDrive\Documents\TicketAlert_v2\ticket-notifier"
if exist ".git\index.lock" del /f ".git\index.lock"
%GIT% add -A > git_output.log 2>&1
%GIT% commit -m "refactor: cart-only mode + District queue handler" >> git_output.log 2>&1
%GIT% push origin main >> git_output.log 2>&1
echo DONE >> git_output.log
