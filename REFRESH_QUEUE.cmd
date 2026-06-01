@echo off
REM Rebuilds the dialer's call list from the Command Center brain (newest OSCR leads
REM + callbacks due, in priority order). Run this right before a dial session.
cd /d "%~dp0"
echo Refreshing Chris's call queue from Command Center...
python build_dialer_queue.py --owner chris --out "C:\dialer\MyDialer_TODAY_READY\MyDialer\contacts.xlsx"
echo.
echo Done. Your dialer's contacts.xlsx now has the prioritized list.
echo Start the dialer as usual.
pause
