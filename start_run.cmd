@echo off
rem Starts the 1M-round collection detached from any console.
cd /d "%~dp0tools"
python -u collect_moon_browser.py --proxies all --workers 50 --rounds 1000000 --rate 0.5 --startup-stagger 0.3 --proxy-check-concurrency 100 --progress-every 5000 --out-root ..\output\browser-runs --run-name run-1m > ..\output\browser-runs\run-1m.log 2>&1
