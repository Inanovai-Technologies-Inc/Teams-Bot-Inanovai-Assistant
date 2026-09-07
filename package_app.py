"""
Builds appPackage/EchoBot.zip -- the file you upload to Microsoft Teams.

Run:  python package_app.py
"""

import json
import pathlib
import sys
import zipfile

HERE = pathlib.Path(__file__).parent
PKG = HERE / "appPackage"
FILES = ["manifest.json", "color.png", "outline.png"]
OUT = PKG / "EchoBot.zip"

manifest = json.loads((PKG / "manifest.json").read_text())
bot_id = manifest["bots"][0]["botId"]

if bot_id.startswith("REPLACE_"):
    print("STOP. You have not set your bot id yet.")
    print()
    print("  1. Create the Azure Bot (README step 3) and copy its Microsoft App ID.")
    print("  2. Open appPackage/manifest.json")
    print('  3. Replace "REPLACE_WITH_YOUR_MICROSOFT_APP_ID" with that id.')
    print("  4. Run this script again.")
    sys.exit(1)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in FILES:
        zf.write(PKG / name, arcname=name)

print(f"Built {OUT}")
print("Upload it in Teams: Apps -> Manage your apps -> Upload a custom app")
