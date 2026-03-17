import os
import time
from PIL import ImageGrab

ROOT = r"c:\Users\robin\Documents\GitHub\Twin PLL GPSDO"
OUT_DIR = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT_DIR, exist_ok=True)

captures = [
    ("01-main-live.png", "Switch to Main tab"),
    ("02-details-live.png", "Switch to Details tab"),
    ("03-about-live.png", "Switch to About tab"),
    ("04-set-op1-dialog.png", "Open Set O/P 1 dialog and leave it visible"),
    ("05-registers-dialog.png", "Open PLL Registers dialog and leave it visible"),
]

print("Starting capture sequence...")
for filename, prompt in captures:
    print(prompt)
    time.sleep(3)
    path = os.path.join(OUT_DIR, filename)
    ImageGrab.grab().save(path)
    print(f"Saved {path}")

print("Capture complete.")
