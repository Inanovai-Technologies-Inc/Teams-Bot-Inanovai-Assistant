"""
Runs every test suite.

    python run_tests.py

test_agent.py and test_bot.py run on their own.
test_local.py needs `python app.py` running in another terminal;
it is skipped with a note if the server is not up.
"""

import socket
import subprocess
import sys


def server_is_up(port=3978) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("localhost", port)) == 0


def run(script: str) -> bool:
    print(f"\n>>> {script}")
    return subprocess.run([sys.executable, script]).returncode == 0


results = {
    "test_agent.py (agent logic)": run("test_agent.py"),
    "test_bot.py (Teams layer)": run("test_bot.py"),
    "test_channels.py (Telegram + WhatsApp)": run("test_channels.py"),
}

if server_is_up():
    results["test_local.py (end to end)"] = run("test_local.py")
else:
    print("\n>>> test_local.py  SKIPPED -- start the bot first:")
    print("    .venv\Scripts\python.exe app.py")

print("\n" + "=" * 62)
for name, ok in results.items():
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print("=" * 62)
sys.exit(0 if all(results.values()) else 1)
