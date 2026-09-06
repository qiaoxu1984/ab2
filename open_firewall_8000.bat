@echo off
rem Allow other build machines to connect to the Manager over the private network.
netsh advfirewall firewall delete rule name="AB2 Manager TCP 8000" >nul 2>&1
netsh advfirewall firewall add rule name="AB2 Manager TCP 8000" dir=in action=allow protocol=TCP localport=8000 profile=any
echo AB2 Manager TCP 8000 firewall rule added.
pause
