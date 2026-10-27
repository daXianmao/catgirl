@echo off
rem Lingdang - reverse SSH tunnel keepalive
rem Edit the three lines below (server IP / ssh user / key path), then double click this file.
rem The tunnel maps this laptop's 127.0.0.1:3001 to the server's 0.0.0.0:3001.
set SERVER=1.2.3.4
set SSHUSER=ubuntu
set KEY=%USERPROFILE%\.ssh\lingdang_napcat

:loop
echo [%date% %time%] opening tunnel to %SSHUSER%@%SERVER% ...
ssh -N -T -i "%KEY%" -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o TCPKeepAlive=yes -R 0.0.0.0:3001:127.0.0.1:3001 %SSHUSER%@%SERVER%
echo [%date% %time%] tunnel closed, retry in 10s
timeout /t 10 /nobreak >nul
goto loop
