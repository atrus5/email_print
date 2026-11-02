# Save as trigger_manual_check.py
with open("trigger.txt", "w") as f:
    f.write("run")
print("Content-type: text/html\n\nTriggered.")