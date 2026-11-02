"""The headless browser the DOM tests drive: chromium on the Pi, google-chrome on a desktop without it.
Ubuntu's chromium is a snap that cannot read the pages the tests write under /tmp, so google-chrome is the
one to install there."""
import shutil

BROWSER = next((path for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                if (path := shutil.which(name))), "chromium")
