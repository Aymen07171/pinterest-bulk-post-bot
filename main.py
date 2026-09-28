#!/usr/bin/env python3
"""
Pinterest Bulk Post Bot - Automate bulk posting of images to Pinterest.

Supports Windows, macOS, and Linux.
Usage: python main.py [--config config.json] [--csv pins.csv] [--headless]
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from sheets_integration import (
    GoogleSheetsClient,
    generate_description,
    get_pinterest_bulk_media_url,
    get_image_for_product,
    make_temp_image_dir,
    optimize_title,
    parse_row_numbers,
    resolve_board,
    rows_to_publish,
    validate_pin_data,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PINTEREST_LOGIN_URL = "https://www.pinterest.com/login/"
PINTEREST_PIN_BUILDER_URL = "https://www.pinterest.com/pin-builder/"
PINTEREST_HOME_URL = "https://www.pinterest.com/"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
DEFAULT_IMAGES_FOLDER = "bulk_post_pinterest"
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_TIMEOUT = 30  # seconds for WebDriverWait


class PublishConfirmationError(RuntimeError):
    """The Publish click was sent, but success could not be verified safely."""


class PinRejectedError(RuntimeError):
    """Pinterest displayed a definite validation or publishing error."""

BANNER = """
\033[38;2;87;94;207m
  ╔══════════════════════════════════════════════════════════════════╗
  ║                                                                  ║
  ║   ██████╗ ██╗███╗   ██╗    ██████╗ ██╗   ██╗██╗     ██╗  ██╗   ║
  ║   ██╔══██╗██║████╗  ██║    ██╔══██╗██║   ██║██║     ██║ ██╔╝   ║
  ║   ██████╔╝██║██╔██╗ ██║    ██████╔╝██║   ██║██║     █████╔╝    ║
  ║   ██╔═══╝ ██║██║╚██╗██║    ██╔══██╗██║   ██║██║     ██╔═██╗    ║
  ║   ██║     ██║██║ ╚████║    ██████╔╝╚██████╔╝███████╗██║  ██╗   ║
  ║   ╚═╝     ╚═╝╚═╝  ╚═══╝    ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝   ║
  ║                                                                  ║
  ║\033[0m\033[38;2;197;193;185m         Pinterest Bulk Post Bot  v2.0                     \033[38;2;87;94;207m║
  ║\033[0m\033[38;2;220;218;213m         Automate bulk posting of images to Pinterest       \033[38;2;87;94;207m║
  ║                                                                  ║
  ╚══════════════════════════════════════════════════════════════════╝
\033[0m\033[38;2;197;193;185m  Built by \033[38;2;87;94;207mSoClose\033[38;2;197;193;185m | soclose.co | Digital Innovation Through Automation & AI\033[0m
"""

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("PinterestBot")

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def xpath_soup(element):
    """Build an XPath string from a BeautifulSoup element."""
    components = []
    child = element if element.name else element.parent
    for parent in child.parents:
        siblings = parent.find_all(child.name, recursive=False)
        components.append(
            child.name if siblings == [child]
            else "%s[%d]" % (child.name, 1 + siblings.index(child))
        )
        child = parent
    components.reverse()
    return "/%s" % "/".join(components)


def load_config(config_path):
    """Load configuration from a JSON file. Returns defaults if file not found."""
    defaults = {
        "board_name": "",
        "login_wait_seconds": 120,
        "delay_between_pins": 2,
        "images_folder": DEFAULT_IMAGES_FOLDER,
        "headless": False,
        "chrome_profile_dir": "",
    }
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            defaults.update(user_config)
            logger.info("Configuration loaded from %s", config_path)
        except (ValueError, IOError) as exc:
            logger.warning("Could not read config file (%s). Using defaults.", exc)
    return defaults


def load_csv_metadata(csv_path):
    """
    Load per-image metadata from a CSV file.

    Expected columns: filename, title, description, link, board (optional)
    Returns a dict keyed by filename (basename).
    """
    metadata = {}
    if not csv_path or not os.path.isfile(csv_path):
        return metadata
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = os.path.basename(row.get("filename", "").strip())
                if key:
                    metadata[key] = {
                        "title": row.get("title", "").strip(),
                        "description": row.get("description", "").strip(),
                        "link": row.get("link", "").strip(),
                        "board": row.get("board", "").strip(),
                    }
        logger.info("Loaded metadata for %d images from %s", len(metadata), csv_path)
    except (csv.Error, ValueError, IOError) as exc:
        logger.warning("Could not read CSV file (%s). Skipping.", exc)
    return metadata


def discover_images(folder_path):
    """Return a sorted list of image file paths in *folder_path*."""
    folder = Path(folder_path)
    if not folder.is_dir():
        logger.error("Images folder not found: %s", folder)
        sys.exit(1)

    images = sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not images:
        logger.error("No images found in %s", folder)
        sys.exit(1)

    logger.info("Found %d image(s) in %s", len(images), folder)
    return images


def create_driver(headless=False, profile_dir=None):
    """Create Chrome with a persistent, dedicated Pinterest login profile."""
    options = webdriver.ChromeOptions()
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_argument("--disable-notifications")
    options.add_argument("--start-maximized")

    if profile_dir:
        pinterest_profile = Path(profile_dir).expanduser().resolve()
    elif os.environ.get("LOCALAPPDATA"):
        pinterest_profile = Path(os.environ["LOCALAPPDATA"]) / "PinterestBulkPostBot" / "ChromeProfile"
    else:
        data_root = os.environ.get("XDG_DATA_HOME")
        pinterest_profile = (Path(data_root) if data_root else Path.home() / ".local" / "share") / "PinterestBulkPostBot" / "ChromeProfile"
    pinterest_profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={pinterest_profile}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as exc:
        logger.error("Failed to start Chrome: %s", exc)
        logger.info("Make sure Google Chrome is installed on your system.")
        sys.exit(1)

    return driver


def progress_bar(current, total, width=40):
    """Return a text progress bar string."""
    pct = current / total
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"|{bar}| {current}/{total} ({pct:.0%})"


# ---------------------------------------------------------------------------
# Core bot logic
# ---------------------------------------------------------------------------


def _pinterest_session_active(driver):
    """Check Pinterest account controls instead of relying on its current URL."""
    if "/login" in driver.current_url.casefold():
        return False
    return _visible_elements(driver, (
        (By.CSS_SELECTOR, "[data-test-id*='header-profile']"),
        (By.CSS_SELECTOR, "button[aria-label='Your profile']"),
        (By.CSS_SELECTOR, "a[aria-label='Your profile']"),
        (By.CSS_SELECTOR, "[aria-label*='your profile' i]"),
    )) is not None


def wait_for_login(driver, timeout_seconds):
    """Reuse an existing Pinterest session or wait for the user to log in once."""
    driver.get(PINTEREST_HOME_URL)
    try:
        WebDriverWait(driver, 15).until(
            lambda browser: browser.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass
    if _pinterest_session_active(driver):
        logger.info("Existing Pinterest login session detected.")
        return True

    driver.get(PINTEREST_LOGIN_URL)
    logger.info("Pinterest login page opened.")
    print()
    print("  Please log in to your Pinterest account in the browser window.")
    print(f"  You have {timeout_seconds} seconds to complete login.")
    print()

    # Wait until the user navigates away from the login page
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _pinterest_session_active(driver):
            logger.info("Login detected! Continuing...")
            return True
        time.sleep(1)

    # If still on login page, ask for manual confirmation
    try:
        choice = input("\n  Are you logged in? (y/n): ").strip().lower()
    except EOFError:
        logger.error("Could not confirm Pinterest login because this terminal has no interactive input.")
        return False
    if choice == "y":
        return True
    logger.error("Login not confirmed. Exiting.")
    return False


def wait_for_element(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for an element and return it."""
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )


def _visible_elements(driver, selectors):
    """Find the first visible element matching any locator in selectors."""
    for by, selector in selectors:
        try:
            for element in driver.find_elements(by, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue
    return None


def _pin_builder_diagnostic(driver):
    """Provide useful UI context instead of Selenium's empty 'Message:' timeout."""
    labels = []
    for element in driver.find_elements(By.CSS_SELECTOR, "button, a, [role='button']"):
        try:
            if not element.is_displayed():
                continue
            label = (element.get_attribute("aria-label") or element.text or element.get_attribute("title") or "").strip()
            if label and label not in labels:
                labels.append(label[:70])
            if len(labels) >= 12:
                break
        except Exception:
            continue
    inputs = len(driver.find_elements(By.CSS_SELECTOR, "input[type='file']"))
    previews = driver.execute_script(
        "return Array.from(document.images).map(img => {"
        "const r=img.getBoundingClientRect();"
        "return {loaded:img.complete && img.naturalWidth>0,width:Math.round(r.width),height:Math.round(r.height)};"
        "}).filter(x => x.loaded && x.width>=150 && x.height>=100).slice(0,5);"
    )
    text_fields = []
    for element in driver.find_elements(
        By.CSS_SELECTOR,
        "input:not([type='file']), textarea, [contenteditable], [role='textbox']",
    ):
        try:
            if not element.is_displayed():
                continue
            details = {
                "tag": element.tag_name,
                "id": element.get_attribute("id"),
                "placeholder": element.get_attribute("placeholder"),
                "aria-label": element.get_attribute("aria-label"),
                "aria-placeholder": element.get_attribute("aria-placeholder"),
                "data-placeholder": element.get_attribute("data-placeholder"),
                "data-test-id": element.get_attribute("data-test-id"),
                "role": element.get_attribute("role"),
                "contenteditable": element.get_attribute("contenteditable"),
            }
            if any(details.values()):
                details["size"] = element.size
                details["text"] = (element.text or "")[:100]
                details["html"] = (element.get_attribute("outerHTML") or "")[:350]
                text_fields.append(details)
            if len(text_fields) >= 12:
                break
        except Exception:
            continue
    return (
        f"URL={driver.current_url!r}; page={driver.title!r}; file_inputs={inputs}; "
        f"preview_images={previews!r}; visible_text_fields={text_fields!r}; "
        f"visible_controls={labels!r}"
    )


def open_pin_builder(driver, timeout=DEFAULT_TIMEOUT):
    """Open Pinterest's current Create Pin screen and verify it has an upload input."""
    direct_urls = (
        PINTEREST_PIN_BUILDER_URL,
        "https://www.pinterest.com/pin-creation-tool/",
    )
    last_error = None
    for url in direct_urls:
        try:
            driver.get(url)
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            return
        except Exception as exc:
            last_error = exc

    # Pinterest's supported desktop workflow is Create -> Create Pin. The direct
    # creation URLs can redirect when the site changes its routing or account UI.
    try:
        driver.get(PINTEREST_HOME_URL)
        WebDriverWait(driver, timeout).until(
            lambda browser: browser.execute_script("return document.readyState") == "complete"
        )
        create_button = _visible_elements(driver, (
            (By.CSS_SELECTOR, "[data-test-id*='create-pin']"),
            (By.CSS_SELECTOR, "button[aria-label='Create']"),
            (By.CSS_SELECTOR, "[role='button'][aria-label='Create']"),
            (By.XPATH, "//button[normalize-space(.)='Create']"),
            (By.XPATH, "//a[normalize-space(.)='Create']"),
            (By.XPATH, "//*[@role='button' and normalize-space(.)='Create']"),
        ))
        if create_button:
            create_button.click()
            time.sleep(1)
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                )
                return
            except Exception:
                pass
            create_pin = _visible_elements(driver, (
                (By.CSS_SELECTOR, "[data-test-id*='create-pin']"),
                (By.XPATH, "//*[self::a or self::button or @role='button'][normalize-space(.)='Create Pin']"),
                (By.XPATH, "//*[self::a or self::button or @role='button'][normalize-space(.)='Create a Pin']"),
            ))
            if create_pin:
                create_pin.click()
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                )
                return
        else:
            menu_button = _visible_elements(driver, (
                (By.CSS_SELECTOR, "button[aria-label*='menu' i]"),
                (By.CSS_SELECTOR, "button[aria-label*='navigation' i]"),
                (By.CSS_SELECTOR, "[data-test-id*='navigation'] button"),
            ))
            if menu_button:
                menu_button.click()
                time.sleep(1)
                create_button = _visible_elements(driver, (
                    (By.XPATH, "//button[normalize-space(.)='Create']"),
                    (By.XPATH, "//a[normalize-space(.)='Create']"),
                    (By.XPATH, "//*[@role='button' and normalize-space(.)='Create']"),
                ))
                if create_button:
                    create_button.click()
                    time.sleep(1)

        # If the menu option is already visible, use it without requiring a Create click.
        create_pin = _visible_elements(driver, (
            (By.XPATH, "//*[self::a or self::button or @role='button'][normalize-space(.)='Create Pin']"),
            (By.XPATH, "//*[self::a or self::button or @role='button'][normalize-space(.)='Create a Pin']"),
        ))
        if create_pin:
            create_pin.click()
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            return
    except Exception as exc:
        last_error = exc

    details = _pin_builder_diagnostic(driver)
    raise RuntimeError(
        "Could not open Pinterest's Pin creation screen or find its image upload input. "
        f"Last navigation error: {last_error}. Page details: {details}"
    ) from last_error


def wait_for_clickable(driver, by, value, timeout=DEFAULT_TIMEOUT):
    """Wait for an element to be clickable and return it."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def upload_image(driver, image_path):
    """Upload an image file to the pin builder."""
    try:
        file_input = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        file_input.send_keys(os.path.abspath(image_path))
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda browser: browser.execute_script(
                "return Array.from(document.images).some(img => {"
                "const r=img.getBoundingClientRect();"
                "return img.complete && img.naturalWidth>0 && r.width>=150 && r.height>=100;"
                "});"
            ),
            message="Pinterest did not render the uploaded mockup preview.",
        )
    except Exception as exc:
        raise RuntimeError(
            f"Pinterest did not show a loaded mockup preview after upload: {exc}. "
            f"Page details: {_pin_builder_diagnostic(driver)}"
        ) from exc


def _find_pin_field(driver, field_name):
    """Find the visible current Pinterest editor control for a Pin field."""
    selectors = {
        "title": (
            "textarea[id*='pin-draft-title']", "input[placeholder*='title' i]",
            "textarea[placeholder*='title' i]", "[contenteditable='true'][data-placeholder*='title' i]",
            "[contenteditable='true'][aria-placeholder*='title' i]", "[contenteditable='true'][aria-label*='title' i]",
            "[role='textbox'][aria-label*='title' i]", "[data-test-id*='title'][contenteditable='true']",
            "[data-test-id*='title'] textarea", "[data-test-id*='title'] input",
            "[data-test-id*='title'] [role='textbox']",
        ),
        "description": (
            "[contenteditable='true'][aria-label*='everyone what your pin' i]",
            "[id*='pin-draft-description']", "textarea[placeholder*='about your pin' i]",
            "textarea[placeholder*='everyone what' i]", "[contenteditable='true'][data-placeholder*='everyone what' i]",
            "[contenteditable='true'][aria-label*='everyone what' i]",
            "[contenteditable='true'][data-placeholder*='pin is about' i]",
            "[contenteditable='true'][data-placeholder*='description' i]",
            "[contenteditable='true'][aria-placeholder*='description' i]",
            "[contenteditable='true'][aria-label*='description' i]",
            "[role='textbox'][aria-label*='description' i]", "[data-test-id*='description'][contenteditable='true']",
            "[data-test-id*='description'] textarea", "[data-test-id*='description'] [role='textbox']",
        ),
        "destination URL": (
            "textarea[id*='pin-draft-link']", "input[placeholder*='destination' i]",
            "textarea[placeholder*='destination' i]", "[contenteditable='true'][data-placeholder*='destination' i]",
            "[contenteditable='true'][aria-label*='link' i]", "input[placeholder*='link' i]",
            "textarea[placeholder*='link' i]", "[role='textbox'][aria-label*='link' i]",
            "[data-test-id*='link'] input", "[data-test-id*='link'] textarea",
            "[data-test-id*='destination'] input", "[data-test-id*='destination'] textarea",
        ),
    }
    for selector in selectors[field_name]:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue

    hints = {
        "title": ("title",),
        "description": ("description", "pin is about", "everyone what your pin", "tell everyone"),
        "destination URL": ("destination", "website link", "link"),
    }[field_name]
    candidates = driver.find_elements(
        By.CSS_SELECTOR, "textarea, input:not([type='file']), [contenteditable='true'], [role='textbox']"
    )
    for element in candidates:
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            labels = " ".join(
                element.get_attribute(attribute) or ""
                for attribute in ("id", "name", "placeholder", "aria-placeholder", "aria-label", "data-placeholder", "data-text", "data-test-id")
            ).casefold()
            if any(hint in labels for hint in hints):
                return element
        except Exception:
            continue
    return None


def fill_pin_details(driver, title, description, link, strict=False):
    """Fill in the title, description, and link fields for a pin."""
    fields = (("title", title), ("description", description), ("destination URL", link))
    for label, value in fields:
        if not value and not strict:
            continue
        stage = "locating the field"
        try:
            if strict and not value:
                raise ValueError(f"The Pinterest {label} is empty.")
            field = WebDriverWait(driver, DEFAULT_TIMEOUT).until(
                lambda browser: _find_pin_field(browser, label),
                message=f"Pinterest {label} field was not found.",
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
            stage = "checking the editor type"
            contenteditable_value = (field.get_attribute("contenteditable") or "").casefold()
            field_role = (field.get_attribute("role") or "").casefold()
            is_contenteditable = contenteditable_value in {"true", "plaintext-only"} or field_role == "combobox"
            stage = f"typing the content (contenteditable={contenteditable_value!r}, role={field_role!r})"
            if is_contenteditable:
                # Pinterest's current description editor is a contenteditable
                # div with role=combobox. ChromeDriver reports it as visible but
                # refuses keyboard input. Use the browser's editing command so
                # Draft.js maintains its expected editable-node structure and
                # emits its normal input event.
                stage = "running the contenteditable edit command"
                editor_result = driver.execute_script(
                    "const e=arguments[0], value=arguments[1]; "
                    "e.focus(); if(document.activeElement!==e) return {focused:false,inserted:false,text:e.innerText,html:e.innerHTML}; "
                    "document.execCommand('selectAll', false, null); "
                    "const inserted=document.execCommand('insertText', false, value); "
                    "return {focused:document.activeElement===e, inserted, text:e.innerText, html:e.innerHTML};",
                    field, value,
                )
                actual_text = re.sub(r"\s+", " ", str(editor_result.get("text") or "")).strip()
                expected_text = re.sub(r"\s+", " ", value).strip()
                if actual_text != expected_text:
                    raise RuntimeError(
                        "Pinterest's description editor did not accept the browser text insertion "
                        f"(focused={editor_result.get('focused')}, inserted={editor_result.get('inserted')}, "
                        f"text={actual_text[:120]!r}, html={str(editor_result.get('html') or '')[:180]!r})."
                    )
            else:
                stage = "focusing the field"
                field.click()
                stage = "typing the content"
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(value)
            stage = "verifying the entered content"
            if is_contenteditable:
                # Pinterest may replace the Draft.js node after its input event;
                # the browser result above is the value read before that rerender.
                actual = editor_result.get("text") or ""
            else:
                actual = field.get_attribute("value")
                if actual is None:
                    actual = field.get_attribute("innerText") or field.get_attribute("textContent") or field.text
            if re.sub(r"\s+", " ", str(actual)).strip() != re.sub(r"\s+", " ", value).strip():
                raise RuntimeError(f"Pinterest did not retain the entered {label}.")
        except Exception as exc:
            if strict:
                raise RuntimeError(
                    f"Could not fill and verify the Pinterest {label} while {stage}: {exc}. Page details: "
                    f"{_pin_builder_diagnostic(driver)}"
                ) from exc
            logger.warning("Could not fill %s: %s", label, exc)


def select_board(driver, board_name, strict=False):
    """Select a Pinterest board by name."""
    try:
        dropdown_selectors = (
            (By.CSS_SELECTOR, "[data-test-id='board-dropdown-select-button']"),
            (By.CSS_SELECTOR, "[data-test-id*='board-dropdown'][data-test-id*='select']"),
            (By.CSS_SELECTOR, "button[aria-label*='board' i]"),
            (By.XPATH, "//*[@role='button' and contains(translate(@aria-label,'BOARD','board'),'board') ]"),
        )
        element = None
        last_error = None
        for by, selector in dropdown_selectors:
            try:
                element = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, selector)))
                if element.is_displayed():
                    break
                element = None
            except Exception as exc:
                last_error = exc
        if element is None:
            raise RuntimeError(f"Pinterest board selector was not found. {last_error or ''}".strip())
        driver.execute_script("arguments[0].scrollIntoView(true);", element)
        element.click()
        search_selectors = (
            (By.ID, "pickerSearchField"),
            (By.CSS_SELECTOR, "input[placeholder*='search' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='search' i]"),
        )
        search_field = None
        for by, selector in search_selectors:
            try:
                search_field = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, selector)))
                break
            except Exception:
                pass
        if search_field is None:
            raise RuntimeError("Pinterest board picker opened, but its search field was not found.")
        search_field.clear()
        search_field.send_keys(board_name)
        boards = []
        for selector in (
            "[data-test-id='boardWithoutSection']",
            "[role='option']",
            "[data-test-id*='board']",
        ):
            try:
                found = WebDriverWait(driver, 3).until(
                    lambda browser: browser.find_elements(By.CSS_SELECTOR, selector)
                )
                boards.extend(found)
                if found:
                    break
            except Exception:
                continue

        def board_label(candidate):
            label = candidate.get_attribute("aria-label") or candidate.text or ""
            return [line.strip().casefold() for line in label.splitlines() if line.strip()]

        matching_board = next(
            (board for board in boards if board.is_displayed() and board_name.strip().casefold() in board_label(board)),
            None,
        )
        if not matching_board:
            raise RuntimeError(f"Pinterest board '{board_name}' was not found in the board picker.")
        matching_board.click()
        try:
            save_btn = wait_for_clickable(driver, By.CSS_SELECTOR, "[data-test-id='board-dropdown-save-button']", timeout=5)
        except Exception:
            save_btn = wait_for_clickable(
                driver,
                By.XPATH,
                "//button[normalize-space(.)='Save' or normalize-space(.)='Done' or normalize-space(.)='Select']",
                timeout=5,
            )
        save_btn.click()
        # The exact board result was selected and Pinterest's own Save/Done control
        # accepted it. Some layouts keep the selector label generic, so do not wait
        # for that label to change; doing so can time out after a valid selection.
        time.sleep(1)

    except Exception as exc:
        logger.warning("Could not select board '%s': %s", board_name, exc)
        if strict:
            raise RuntimeError(f"Could not select Pinterest board '{board_name}': {exc}") from exc


def extract_pin_reference(driver):
    """Find the newly created Pin URL/ID when Pinterest exposes it in the page."""
    current_url = driver.current_url
    match = re.search(r"/pin/(\d+)", current_url)
    if match:
        pin_id = match.group(1)
        return pin_id, f"https://www.pinterest.com/pin/{pin_id}/"

    return "", ""


def click_publish_button(driver, strict=False):
    """Submit the completed Pin using Pinterest's visible Publish/Create button."""
    selectors = [
        (By.CSS_SELECTOR, "[data-test-id='pin-builder-publish-button']"),
        (By.XPATH, "//button[normalize-space(.)='Publish' or normalize-space(.)='Publish now']"),
        (By.XPATH, "//*[@role='button' and (normalize-space(.)='Publish' or normalize-space(.)='Publish now')]"),
        (By.XPATH, "//button[@aria-label='Publish' or @aria-label='Publish Pin']"),
    ]
    for by, selector in selectors:
        for candidate in driver.find_elements(by, selector):
            if candidate.is_displayed() and candidate.is_enabled():
                candidate.click()
                return True
    message = "Pinterest Publish button was not found or was disabled."
    if strict:
        raise RuntimeError(message)
    logger.warning(message)
    return False


def wait_for_publish(driver, timeout=60, before_url=None):
    """Return success only after a Pinterest confirmation or a new Pin URL appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        current_url = driver.current_url
        page_text = soup.get_text(" ", strip=True).casefold()
        pin_reference = extract_pin_reference(driver)
        if pin_reference != ("", "") and current_url != before_url:
            return {"pin_id": pin_reference[0], "pin_url": pin_reference[1], "confirmed": True}

        confirmation_nodes = soup.select(
            "[role='alert'], [role='status'], [data-test-id*='toast'], [data-test-id*='success']"
        )
        confirmation_text = " ".join(node.get_text(" ", strip=True).casefold() for node in confirmation_nodes)
        confirmation_phrases = (
            "pin published", "your pin was published", "your pin is published",
            "pin has been published", "pin was created", "your pin has been saved",
        )
        if any(phrase in confirmation_text for phrase in confirmation_phrases):
            link_match = None
            for node in confirmation_nodes:
                for anchor in node.select("a[href]"):
                    link_match = re.search(r"/pin/(\d+)/?", anchor.get("href", ""))
                    if link_match:
                        break
                if link_match:
                    break
            pin_id = link_match.group(1) if link_match else ""
            return {
                "pin_id": pin_id,
                "pin_url": f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else "",
                "confirmed": True,
            }

        error_nodes = soup.select("[role='alert'], [data-test-id*='error']")
        error_text = " ".join(node.get_text(" ", strip=True) for node in error_nodes).strip()
        lower_error = error_text.casefold()
        if any(token in lower_error for token in ("error", "failed", "couldn't", "could not", "try again", "something went wrong", "problem publishing")):
            raise PinRejectedError(f"Pinterest displayed an error after the Publish action: {error_text[:400]}")
        time.sleep(1)
    logger.warning("Publish timeout - Pinterest did not confirm that the Pin finished saving.")
    return None


def post_single_pin(driver, image_path, title, description, link, board_name, strict=False):
    """Validate and publish one Pin through Pinterest's browser interface."""
    if not str(title or "").strip():
        raise ValueError("Pre-publication validation failed: Pin title is empty.")
    content = {"title": title, "description": description, "keywords": "", "context": {}}
    title = optimize_title(content)
    description = generate_description(content)
    payload = {
        "pin_title": title,
        "pin_description": description,
        "destination_url": link,
        "pin_board": board_name,
    }
    errors = validate_pin_data(payload, image_path=image_path)
    if errors:
        raise ValueError("Pre-publication validation failed: " + " ".join(errors))

    upload_image(driver, image_path)
    fill_pin_details(driver, title, description, link, strict=strict)
    if board_name:
        select_board(driver, board_name, strict=strict)
    before_url = driver.current_url
    click_publish_button(driver, strict=strict)
    try:
        result = wait_for_publish(driver, before_url=before_url)
    except PinRejectedError:
        raise
    except Exception as exc:
        raise PublishConfirmationError(
            f"Publish was clicked, but the result could not be verified: {exc}. Check Pinterest before retrying."
        ) from exc
    if result is None:
        raise PublishConfirmationError(
            "Publish was clicked, but Pinterest did not provide confirmation. Check the account before retrying."
        )
    return result


def _product_identity(product):
    """Return a stable catalog key where the sheet supplies one."""
    raw = product.get("raw", {})
    normalized = {re.sub(r"[^a-z0-9]+", "", str(key).casefold()): str(value).strip()
                  for key, value in raw.items()}
    for field in ("productid", "sku", "etsylistingid", "printifyproductid"):
        value = normalized.get(field, "")
        if value:
            return field, value.casefold()
    return None


def export_pinterest_bulk_csv(products, client, project_dir, requested_path):
    """Create Pinterest's supported bulk CSV and mark rows as exported, not published."""
    columns = (
        "Title", "Media URL", "Pinterest board", "Thumbnail",
        "Description", "Link", "Publish date", "Keywords",
    )
    rows = []
    for product in products:
        try:
            media_url = get_pinterest_bulk_media_url(product, client.drive_session)
        except Exception as exc:
            logger.error("Could not prepare Pinterest CSV media for Sheet row %d: %s", product["sheet_row"], exc)
            return 1
        rows.append({
            "Title": product["pin_title"],
            "Media URL": media_url,
            "Pinterest board": product["pin_board"],
            "Thumbnail": "",
            "Description": product["pin_description"],
            "Link": product["destination_url"],
            "Publish date": "",
            "Keywords": product.get("keywords", ""),
        })

    output_path = Path(requested_path).expanduser()
    if not output_path.is_absolute():
        output_path = Path(project_dir) / output_path
    if output_path.suffix.casefold() != ".csv":
        logger.error("Pinterest bulk upload output path must end in .csv: %s", output_path)
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)

    row_chunks = [rows[start:start + 200] for start in range(0, len(rows), 200)]
    output_files = []
    try:
        for index, row_chunk in enumerate(row_chunks, start=1):
            if len(row_chunks) == 1:
                chunk_path = output_path
            else:
                chunk_path = output_path.with_name(
                    f"{output_path.stem}_part{index:02d}{output_path.suffix}"
                )
            with chunk_path.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(row_chunk)
            output_files.append(chunk_path)
    except OSError as exc:
        logger.error("Could not write Pinterest bulk CSV: %s", exc)
        return 1

    exported_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    update_failed = False
    for product in products:
        try:
            client.update_product(
                product["sheet_row"],
                status="CSV Exported",
                selected=False,
                error="",
                csv_exported_at=exported_at,
            )
        except Exception as exc:
            update_failed = True
            logger.error(
                "CSV was written, but could not update Sheet row %d to CSV Exported: %s",
                product["sheet_row"], exc,
            )

    print(f"\n  Pinterest bulk CSV created for {len(products)} product(s):")
    for path in output_files:
        print(f"  {path}")
    print("  Sheet status is CSV Exported; this means the file was created, not that Pinterest has published it.")
    print("  Upload the file in Pinterest Business: Settings > Import content > Upload .csv or .txt file.\n")
    if update_failed:
        logger.error("One or more Sheet rows were not updated after export; review them before exporting again.")
        return 1
    return 0


def run_google_sheets_workflow(args, config):
    """Load selected Google Sheet rows, publish them, and write their state back."""
    sheets_config = config.get("google_sheets", {})
    project_dir = Path(__file__).resolve().parent
    try:
        client = GoogleSheetsClient(sheets_config, project_dir)
        products = client.load_products(initialize_status=not args.dry_run)
        explicit_rows = parse_row_numbers(args.rows)
        pending = rows_to_publish(products, explicit_rows, retry_failed=args.retry_failed)
    except Exception as exc:
        logger.error("Could not load Google Sheets publishing queue: %s", exc)
        return 1

    if not pending:
        print("  No Google Sheet rows are ready to publish.")
        print("  Set Status to Ready, check the Publish column, use --rows, or pass --retry-failed.\n")
        return 0

    # Browser publishing uses one-Pin trial mode by default. Native CSV export is
    # only file preparation, so it can safely include every selected row.
    if len(pending) > 1 and not args.confirm_bulk and args.export_pinterest_csv is None:
        logger.warning(
            "One-Pin safety mode: processing only sheet row %d. Review its published Pin before using --confirm-bulk.",
            pending[0]["sheet_row"],
        )
        pending = pending[:1]

    published_identities = {}
    for product in products:
        if str(product.get("status", "")).strip().casefold() == "published" or product.get("pin_id") or product.get("pin_url"):
            identity = _product_identity(product)
            if identity:
                published_identities[identity] = product

    unique_pending = []
    seen_identities = {}
    for product in pending:
        identity = _product_identity(product)
        duplicate = published_identities.get(identity) if identity else None
        duplicate = duplicate or (seen_identities.get(identity) if identity else None)
        if duplicate:
            earlier_row = duplicate["sheet_row"]
            message = f"Duplicate product detected: catalog identity matches sheet row {earlier_row}; no Pin was created."
            logger.error("Sheet row %d: %s", product["sheet_row"], message)
            if not args.dry_run:
                try:
                    client.update_product(product["sheet_row"], status="Retry Required", error=message, selected=False)
                except Exception as exc:
                    logger.error("Could not write duplicate warning for row %d: %s", product["sheet_row"], exc)
            continue
        if identity:
            seen_identities[identity] = product
        unique_pending.append(product)
    pending = unique_pending
    if not pending:
        return 1

    category_boards = sheets_config.get("category_boards", {})
    fallback_board = args.board or config.get("board_name") or sheets_config.get("default_board", "")
    for product in pending:
        product["pin_board"] = resolve_board(product, category_boards, fallback_board)

    if any(not product["pin_board"] for product in pending):
        fallback_board = input("  Default Pinterest board for rows without a board/category mapping: ").strip()
        for product in pending:
            if not product["pin_board"]:
                product["pin_board"] = fallback_board

    usable = []
    failed_before_browser = 0
    default_destination = sheets_config.get("default_destination_url", "").strip()
    for product in pending:
        if not product["pin_board"]:
            failed_before_browser += 1
            message = "No board is set in the row and no fallback board was provided."
            logger.error("Sheet row %d: %s", product["sheet_row"], message)
            if not args.dry_run:
                try:
                    client.update_product(product["sheet_row"], status="Retry Required", error=message, selected=False)
                except Exception as exc:
                    logger.error("Could not write failure status for row %d: %s", product["sheet_row"], exc)
            continue
        try:
            product["pin_title"] = optimize_title(product)
            product["pin_description"] = generate_description(product)
            product["destination_url"] = product.get("url", "").strip() or default_destination
            errors = validate_pin_data(product)
            if errors:
                raise ValueError(" ".join(errors))
        except Exception as exc:
            failed_before_browser += 1
            message = f"Pre-publication validation failed: {exc}"
            logger.error("Sheet row %d: %s", product["sheet_row"], message)
            if not args.dry_run:
                try:
                    client.update_product(product["sheet_row"], status="Retry Required", error=message[:500], selected=False)
                except Exception as update_exc:
                    logger.error("Could not write validation failure for row %d: %s", product["sheet_row"], update_exc)
            continue
        usable.append(product)

    if not usable:
        return 1

    if args.export_pinterest_csv is not None:
        return export_pinterest_bulk_csv(
            usable, client, project_dir, args.export_pinterest_csv
        )

    print(f"\n  Preparing {len(usable)} Google Sheet row(s) for Pinterest...\n")
    with make_temp_image_dir() as image_temp_dir:
        prepared = []
        for product in usable:
            try:
                product["image_path"] = get_image_for_product(
                    product, project_dir, image_temp_dir, client.drive_session
                )
                errors = validate_pin_data(product, image_path=product["image_path"])
                if errors:
                    raise ValueError(" ".join(errors))
                prepared.append(product)
            except Exception as exc:
                failed_before_browser += 1
                logger.error("Could not prepare mockup for sheet row %d: %s", product["sheet_row"], exc)
                if not args.dry_run:
                    try:
                        client.update_product(
                            product["sheet_row"], status="Retry Required",
                            error=f"Mockup preflight failed: {exc}"[:500], selected=False
                        )
                    except Exception as update_exc:
                        logger.error("Could not write failure status for row %d: %s", product["sheet_row"], update_exc)

        if not prepared:
            return 1

        print("\n  Single-Pin preview (nothing is published until the checks below pass):")
        for product in prepared:
            print(f"  Sheet row: {product['sheet_row']}")
            print(f"  Title ({len(product['pin_title'])}/100): {product['pin_title']}")
            print(f"  Description ({len(product['pin_description'])}/500): {product['pin_description']}")
            print(f"  Destination: {product['destination_url']}")
            print(f"  Board: {product['pin_board']}")
            print(f"  Mockup: {Path(product['image_path']).name}\n")

        if args.dry_run:
            logger.info("Dry run completed: %d row(s) passed validation; no Pins were published.", len(prepared))
            return 0

        logger.info("Starting Chrome browser...")
        driver = create_driver(
            headless=args.headless or config.get("headless", False),
            profile_dir=config.get("chrome_profile_dir"),
        )
        try:
            if not wait_for_login(driver, config.get("login_wait_seconds", 120)):
                return 1

            successful = 0
            failed = failed_before_browser
            total = len(prepared)
            print(f"\n  Starting to publish {total} validated Pin(s)...\n")

            for index, product in enumerate(prepared, start=1):
                row_number = product["sheet_row"]
                logger.info("Posting %s  Google Sheet row %d", progress_bar(index, total), row_number)
                try:
                    # The in-flight marker prevents an automatic duplicate if the process stops mid-post.
                    client.update_product(row_number, status="Processing", error="")
                except Exception as exc:
                    logger.error("Could not mark sheet row %d as Processing; stopping before post: %s", row_number, exc)
                    failed += 1
                    break

                try:
                    open_pin_builder(driver)
                    result = post_single_pin(
                        driver,
                        product["image_path"],
                        product["pin_title"],
                        product["pin_description"],
                        product["destination_url"],
                        product["pin_board"],
                        strict=True,
                    )
                except Exception as exc:
                    failed += 1
                    logger.error("Failed to post sheet row %d: %s", row_number, exc)
                    try:
                        if isinstance(exc, PublishConfirmationError):
                            # Avoid automatic retries after a possibly successful click.
                            client.update_product(
                                row_number, status="Processing", error=str(exc)[:500], selected=False
                            )
                        elif isinstance(exc, PinRejectedError):
                            client.update_product(
                                row_number, status="Failed", error=str(exc)[:500], selected=False
                            )
                        else:
                            client.update_product(
                                row_number, status="Retry Required", error=str(exc)[:500], selected=False
                            )
                    except Exception as update_exc:
                        logger.error(
                            "Could not write failure status for row %d; it remains marked Processing: %s",
                            row_number,
                            update_exc,
                        )
                else:
                    try:
                        client.update_product(
                            row_number,
                            status="Published",
                            pin_id=result.get("pin_id") or None,
                            pin_url=result.get("pin_url") or None,
                            error="",
                            selected=False,
                            published_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            published_title=product["pin_title"],
                            published_description=product["pin_description"],
                        )
                    except Exception as exc:
                        # Pinterest accepted the Pin, so keep Processing as a duplicate guard.
                        failed += 1
                        logger.error(
                            "Pin for row %d appears saved, but Google Sheets could not record it. "
                            "The row remains Processing and needs review: %s",
                            row_number,
                            exc,
                        )
                    else:
                        successful += 1
                        if not result.get("pin_id") and not result.get("pin_url"):
                            logger.warning(
                                "Row %d was confirmed by Pinterest, but the browser did not expose a Pin ID/URL.", row_number
                            )
                        logger.info("Sheet row %d posted successfully.", row_number)

                if index < total:
                    time.sleep(config.get("delay_between_pins", 2))

            print()
            print("\033[38;2;87;94;207m" + "=" * 60)
            print(f"  COMPLETED: {successful} posted | {failed} failed | {len(pending)} selected")
            print("=" * 60 + "\033[0m\n")
            return 0 if failed == 0 else 1
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Rows already marked Processing need review before retrying.")
            return 1
        finally:
            driver.quit()
            logger.info("Browser closed. Goodbye!")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
    try:
        print(BANNER)
    except UnicodeEncodeError:
        print("Pinterest Bulk Post Bot v2.0\nAutomate bulk posting of images to Pinterest\n")

    # Parse CLI arguments
    parser = argparse.ArgumentParser(
        description="Pinterest Bulk Post Bot - Automate posting images to Pinterest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_FILE,
        help="Path to JSON config file (default: config.json)",
    )
    parser.add_argument(
        "--csv", default=None,
        help="Path to CSV file with per-image metadata",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run Chrome in headless mode (no visible browser window)",
    )
    parser.add_argument(
        "--board", default=None,
        help="Pinterest board name to post to",
    )
    parser.add_argument(
        "--images", default=None,
        help="Path to folder containing images to post",
    )
    parser.add_argument(
        "--sheets", action="store_true",
        help="Read selected product rows from the configured Google Sheet",
    )
    parser.add_argument(
        "--rows", default=None,
        help="Comma-separated 1-based Google Sheet row numbers to publish, for example 2,5,8",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Include rows whose sheet status is Failed or Retry Required",
    )
    parser.add_argument(
        "--confirm-bulk", action="store_true",
        help="Allow publishing more than one selected Sheet row after the one-Pin trial succeeds",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read the Sheet, download mockups, and validate Pins without opening Pinterest or writing row statuses",
    )
    parser.add_argument(
        "--export-pinterest-csv", nargs="?", const="exports/pinterest_bulk_upload.csv", default=None,
        metavar="FILE",
        help="Export selected Google Sheet rows in Pinterest's native bulk-upload CSV format; requires --sheets",
    )
    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)

    if args.sheets:
        if args.csv:
            parser.error("--sheets and --csv are separate content sources; choose one.")
        if args.export_pinterest_csv is not None and args.dry_run:
            parser.error("--export-pinterest-csv writes a CSV and updates Sheet export status; do not combine it with --dry-run.")
        sys.exit(run_google_sheets_workflow(args, config))
    if args.rows or args.retry_failed or args.confirm_bulk or args.dry_run or args.export_pinterest_csv is not None:
        parser.error("--rows, --retry-failed, --confirm-bulk, --dry-run, and --export-pinterest-csv require --sheets.")

    # CLI args override config file
    headless = args.headless or config.get("headless", False)
    images_folder = args.images or config.get("images_folder", DEFAULT_IMAGES_FOLDER)
    board_name = args.board or config.get("board_name", "")
    csv_path = args.csv

    # Resolve images folder (relative to script location)
    if not os.path.isabs(images_folder):
        images_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), images_folder)

    # Discover images
    images = discover_images(images_folder)

    # Load CSV metadata if provided
    csv_metadata = load_csv_metadata(csv_path)

    # Collect default metadata from user if no CSV provided
    default_title = ""
    default_description = ""
    default_link = ""

    if not csv_metadata:
        print("  Enter the default metadata for your pins:")
        print("  (Leave blank to skip a field)\n")
        default_title = input("  Title: ").strip()
        default_description = input("  Description: ").strip()
        default_link = input("  Link: ").strip()
        print()

    if not board_name:
        board_name = input("  Board name: ").strip()
        print()

    # Start browser
    logger.info("Starting Chrome browser...")
    driver = create_driver(headless=headless, profile_dir=config.get("chrome_profile_dir"))

    try:
        # Login
        if not wait_for_login(driver, config.get("login_wait_seconds", 120)):
            driver.quit()
            sys.exit(1)

        total = len(images)
        successful = 0
        failed = 0

        print(f"\n  Starting to post {total} pin(s)...\n")

        for i, image_path in enumerate(images, start=1):
            filename = os.path.basename(image_path)
            meta = csv_metadata.get(filename, {})

            title = meta.get("title") or default_title
            description = meta.get("description") or default_description
            link = meta.get("link") or default_link
            pin_board = meta.get("board") or board_name

            logger.info(
                "Posting %s  %s",
                progress_bar(i, total),
                filename,
            )

            try:
                open_pin_builder(driver)

                post_single_pin(driver, image_path, title, description, link, pin_board)
                successful += 1
                logger.info("Pin %d/%d posted successfully.", i, total)

            except Exception as exc:
                failed += 1
                logger.error("Failed to post pin %d/%d (%s): %s", i, total, filename, exc)
                continue

        # Summary
        print()
        print("\033[38;2;87;94;207m" + "=" * 60)
        print(f"  COMPLETED: {successful} posted | {failed} failed | {total} total")
        print("=" * 60 + "\033[0m")
        print("\033[38;2;197;193;185m  Powered by SoClose | soclose.co\033[0m")
        print()

    except KeyboardInterrupt:
        logger.info("Interrupted by user. Stopping...")

    finally:
        driver.quit()
        logger.info("Browser closed. Goodbye!")


if __name__ == "__main__":
    main()
