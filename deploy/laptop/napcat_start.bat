@echo off
rem Lingdang - start NapCat on this laptop (called by watchdog.ps1, or double click)
rem Change the path below if you extracted NapCat somewhere else. Use ASCII path only.
cd /d C:\NapCat

if exist napcat.quick.bat (
    rem quick login: reuse the cached credential, no QR code.
    rem First time: double click napcat.bat manually, scan, and tick "no confirm next time"
    call napcat.quick.bat
) else (
    call napcat.bat
)
