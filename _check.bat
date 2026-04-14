@echo off
setlocal
set GIT="C:\Program Files\Git\cmd\git.exe"
cd /d "C:\Users\ANKUR VASHISHTHA\OneDrive\Documents\TicketAlert_v2\ticket-notifier"
%GIT% log --oneline -3 > git_output.log 2>&1
%GIT% status --short >> git_output.log 2>&1
echo --- >> git_output.log
%GIT% add -A >> git_output.log 2>&1
%GIT% commit -m "chore: update gitignore and cleanup" >> git_output.log 2>&1
%GIT% push origin main >> git_output.log 2>&1
echo DONE >> git_output.log
