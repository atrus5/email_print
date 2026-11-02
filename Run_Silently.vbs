Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd.exe /c Start_Order_Automation.bat", 0
Set WshShell = Nothing
