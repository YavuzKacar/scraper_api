"""Delete files that are no longer used in the simplified codebase."""
import pathlib

root = pathlib.Path(__file__).parent
delete = [
    "classifier.py",
    "strategy.py",
    "_patch_browser.py",
    "_write_scheduler.py",
    "_write_scraper.py",
]
for name in delete:
    p = root / name
    if p.exists():
        p.unlink()
        print(f"Deleted {name}")
    else:
        print(f"Not found: {name}")
print("Done.")
