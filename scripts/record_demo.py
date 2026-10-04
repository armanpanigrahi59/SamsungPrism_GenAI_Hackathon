"""
Automated recording script for prism-agent using Playwright.
Records a complete, high-definition video walkthrough of:
1. Landing page with hero video and architectural breakdown
2. Flights page with real-time barge-in cancellation and booking flow
3. Airports explorer with search, details drawer, and continent filters
4. Support page with grounded slot clarification
5. How It Works architecture layer breakdown
6. Finale
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

WORKSPACE = Path(__file__).resolve().parent.parent
TEMP_VIDEO_DIR = WORKSPACE / "scratch" / "video_raw"
DOCS_DIR = WORKSPACE / "docs"

def smooth_scroll(page, start_y, end_y, steps=25, delay=0.03):
    """Smoothly scroll the page vertically."""
    delta = (end_y - start_y) / steps
    current = start_y
    for _ in range(steps):
        current += delta
        page.evaluate(f"window.scrollTo(0, {current})")
        time.sleep(delay)

def record():
    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    print("Starting Playwright recording...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(TEMP_VIDEO_DIR),
            record_video_size={"width": 1440, "height": 900}
        )
        page = context.new_page()

        # -------------------------------------------------------------
        # 1. LANDING PAGE
        # -------------------------------------------------------------
        print("1. Visiting Landing Page...")
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        time.sleep(3)  # Admire hero section and video

        # Smooth scroll down through architecture overview and layers
        smooth_scroll(page, 0, 700, steps=25, delay=0.04)
        time.sleep(2.5)

        smooth_scroll(page, 700, 1400, steps=25, delay=0.04)
        time.sleep(2.5)

        smooth_scroll(page, 1400, 0, steps=20, delay=0.03)
        time.sleep(1.5)

        # -------------------------------------------------------------
        # 2. FLIGHTS PAGE (Barge-in cancellation + booking)
        # -------------------------------------------------------------
        print("2. Visiting Flights Page...")
        page.click("a[href='/flights']")
        try:
            page.wait_for_selector("#connPill.live", timeout=6000)
        except Exception:
            time.sleep(2)
        time.sleep(2)

        # Click the barge-in demo chip
        print("Running barge-in demo...")
        page.click("[data-demo='1']")

        # Wait for the turn to stream, cancel generation 0, and finish generation 1
        time.sleep(6)

        # Scroll to show timeline and trip card
        smooth_scroll(page, 0, 450, steps=20, delay=0.03)
        time.sleep(2.5)

        # Scroll to results card
        smooth_scroll(page, 450, 950, steps=20, delay=0.03)
        time.sleep(2.5)

        # Hover over fare strip
        day_items = page.locator(".date-strip .day-pill")
        if day_items.count() > 0:
            day_items.first.hover()
            time.sleep(1)

        # Click Select on first flight offer
        select_btns = page.locator("button.btn-primary:has-text('Select')")
        if select_btns.count() > 0:
            print("Selecting flight offer...")
            select_btns.first.click()
            time.sleep(2)

            # Fill in passenger name and confirm booking
            pax_input = page.locator("input[data-pax-name='0']")
            if pax_input.is_visible():
                print("Entering passenger name...")
                pax_input.fill("Alice Smith")
                time.sleep(1)

            book_btn = page.locator("button[data-book='1']")
            if book_btn.is_visible():
                print("Confirming booking...")
                book_btn.click()
                time.sleep(3)

        # Scroll back to top
        smooth_scroll(page, 950, 0, steps=20, delay=0.03)
        time.sleep(1)

        # -------------------------------------------------------------
        # 3. AIRPORTS EXPLORER
        # -------------------------------------------------------------
        print("3. Visiting Airports Explorer...")
        page.click("a[href='/airports']")
        page.wait_for_selector("#statAirports", timeout=8000)
        time.sleep(2.5)

        # Search for Singapore
        search_input = page.locator("#exploreSearch")
        search_input.click()
        search_input.type("Singapore", delay=100)
        time.sleep(2.5)

        # Click details on Singapore Changi
        sin_details = page.locator("button:has-text('Details')").first
        if sin_details.is_visible():
            print("Opening Singapore Changi details drawer...")
            sin_details.click()
            time.sleep(3.5)

            # Close drawer
            page.click("#drawerClose")
            time.sleep(1.5)

        # Clear search input
        search_input.fill("")
        time.sleep(1)

        # Switch to Europe continent tab
        print("Filtering by Europe continent...")
        page.click("button[data-continent='EU']")
        time.sleep(2.5)

        # Switch to Asia continent tab
        print("Filtering by Asia continent...")
        page.click("button[data-continent='AS']")
        time.sleep(2.5)

        # Scroll down slightly to show airports grid
        smooth_scroll(page, 0, 500, steps=20, delay=0.03)
        time.sleep(2)
        smooth_scroll(page, 500, 0, steps=15, delay=0.03)

        # -------------------------------------------------------------
        # 4. SUPPORT CLARIFICATION AGENT
        # -------------------------------------------------------------
        print("4. Visiting Support Page...")
        page.click("a[href='/support']")
        try:
            page.wait_for_selector("#connPill.live", timeout=6000)
        except Exception:
            time.sleep(2)
        time.sleep(2)

        # Run clarification demo
        print("Running clarification demo...")
        page.click("[data-demo='1']")
        time.sleep(5)

        # Scroll to view ticket card & timeline
        smooth_scroll(page, 0, 350, steps=15, delay=0.03)
        time.sleep(3)
        smooth_scroll(page, 350, 0, steps=15, delay=0.03)

        # -------------------------------------------------------------
        # 5. HOW IT WORKS
        # -------------------------------------------------------------
        print("5. Visiting How It Works...")
        page.click("a[href='/how-it-works']")
        time.sleep(2.5)

        # Scroll smoothly through all architecture layers
        smooth_scroll(page, 0, 600, steps=20, delay=0.04)
        time.sleep(2)
        smooth_scroll(page, 600, 1200, steps=20, delay=0.04)
        time.sleep(2)
        smooth_scroll(page, 1200, 1800, steps=20, delay=0.04)
        time.sleep(2)
        smooth_scroll(page, 1800, 0, steps=25, delay=0.03)
        time.sleep(1.5)

        # -------------------------------------------------------------
        # 6. FINALE - Back to Flights
        # -------------------------------------------------------------
        print("6. Returning to Flights for finale...")
        page.click("a[href='/flights']")
        time.sleep(3)

        # Close page and context to ensure video is fully saved
        print("Closing browser context to write video...")
        page.close()
        video_path = page.video.path()
        context.close()
        browser.close()

    print(f"Raw video recorded to: {video_path}")
    return video_path

def convert_video(raw_video_path):
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    out_mp4_root = WORKSPACE / "demo.mp4"
    out_webm_root = WORKSPACE / "demo.webm"
    out_mp4_docs = DOCS_DIR / "demo.mp4"
    out_webm_docs = DOCS_DIR / "demo.webm"

    print("Converting video to MP4 (H.264 / AAC) for universal playback...")
    # Convert to MP4
    cmd_mp4 = [
        ffmpeg_exe, "-y",
        "-i", str(raw_video_path),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "22",
        "-movflags", "+faststart",
        str(out_mp4_root)
    ]
    subprocess.run(cmd_mp4, check=True)
    print(f"Created: {out_mp4_root} ({out_mp4_root.stat().st_size / 1024 / 1024:.2f} MB)")

    # Copy to docs/demo.mp4
    import shutil
    shutil.copy2(out_mp4_root, out_mp4_docs)
    print(f"Copied to: {out_mp4_docs}")

    # Copy raw webm to demo.webm and docs/demo.webm
    shutil.copy2(raw_video_path, out_webm_root)
    shutil.copy2(raw_video_path, out_webm_docs)
    print(f"Created: {out_webm_root} and {out_webm_docs}")

    # Generate animated WebP for README / docs previews
    out_webp_root = WORKSPACE / "demo.webp"
    out_webp_docs = DOCS_DIR / "demo.webp"
    print("Generating animated WebP preview...")
    cmd_webp = [
        ffmpeg_exe, "-y",
        "-i", str(raw_video_path),
        "-vf", "fps=10,scale=1024:-1:flags=lanczos",
        "-loop", "0",
        str(out_webp_root)
    ]
    try:
        subprocess.run(cmd_webp, check=True)
        shutil.copy2(out_webp_root, out_webp_docs)
        print(f"Created: {out_webp_root} and {out_webp_docs} ({out_webp_root.stat().st_size / 1024 / 1024:.2f} MB)")
    except Exception as e:
        print(f"WebP generation warning: {e}")

    print("All video conversions completed successfully!")

if __name__ == "__main__":
    raw_video = record()
    convert_video(raw_video)
