import sys
import os
# Ensure project root is in sys.path for imports
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from selenium.webdriver.common.by import By
from utils.ui_helpers import find_element
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def get_vehicle_property_by_label(driver, label, timeout=5):
    """
    Find the value for a given vehicle property label (e.g., 'VIN', 'Desc').
    This is robust to dynamic classes.
    """
    try:
        xp = (
            f"//div[div[normalize-space()='{label}']]/div[contains(@class,'vehicle-property-value')]"
        )
        elem = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xp))
        )
        return elem.text.strip() if elem else "N/A"
    except Exception:
        return "N/A"
"""
Script to query Compass for VIN and Description for a list of MVAs.
Leverages existing flows and page objects.
"""
import csv
import os
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
from flows.LoginFlow import LoginFlow
from pages.vehicle_properties_page import VehiclePropertiesPage
from pages.vehicle import Vehicle
from pages.mva_input_page import MVAInputPage
from core.driver_manager import create_driver, get_driver, quit_driver
from config.config_loader import get_config
from utils.logger import log

RESULTS_FILE = "GlassResults.txt"
MVA_CSV = "data/GlassDataParser.csv"


def read_mva_list(csv_path):
    import re

    def normalize_mva(raw: str) -> str:
        s = raw.strip()
        # prefer leading 8 digits
        m = re.match(r"^(\d{8})", s)
        if m:
            return m.group(1)
        # fallback: take first 8 characters
        return s[:8]

    mvas = []
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        rows = [row[0] for row in reader if row]
        # Skip header if present (e.g., starts with '#' or 'MVA')
        if rows and (rows[0].startswith('#') or rows[0].lower().startswith('mva')):
            rows = rows[1:]

        for raw in rows:
            if not raw:
                continue
            if raw.startswith('#'):
                continue
            mvas.append(normalize_mva(raw))

    return mvas


def main():
    username = os.getenv("GLASS_LOGIN_USERNAME") or get_config("username")
    password = os.getenv("GLASS_LOGIN_PASSWORD") or get_config("password")
    login_id = os.getenv("GLASS_LOGIN_ID") or get_config("login_id")
    create_driver()
    driver = get_driver()
    login_flow = LoginFlow(driver)
    login_result = login_flow.login_handler(username, password, login_id)
    if login_result.get("status") != "ok":
        log.error(f"[LOGIN] Failed to initialize session → {login_result}")
        return


    mva_list = read_mva_list(MVA_CSV)
    results = []

    mva_input_page = MVAInputPage(driver)
    vehicle_properties_page = VehiclePropertiesPage(driver)

    # --- Workaround: Query a dummy MVA first to prime the AUT ---
    dummy_mva = get_config("warmup_mva", "50227203")
    try:
        input_field = mva_input_page.find_input()
        if not (input_field and input_field.is_enabled() and input_field.is_displayed()):
            try:
                input_field = WebDriverWait(driver, 5, poll_frequency=0.25).until(
                    lambda d: (
                        (f := mva_input_page.find_input()) and f.is_enabled() and f.is_displayed() and f
                    )
                )
            except TimeoutException:
                input_field = None
        if input_field:
            input_field.clear()
            input_field.send_keys(dummy_mva)
            log.info(f"[MVA_INPUT][WORKAROUND] Queried dummy MVA: {dummy_mva}")
            # Wait for VIN and Desc to appear (ignore results)
            def non_empty_value(label):
                def _predicate(driver):
                    val = get_vehicle_property_by_label(driver, label)
                    return val if val and val != "N/A" else False
                return _predicate
            try:
                WebDriverWait(driver, 12, poll_frequency=0.5).until(non_empty_value("VIN"))
            except Exception:
                pass
            try:
                WebDriverWait(driver, 12, poll_frequency=0.5).until(non_empty_value("Desc"))
            except Exception:
                pass
        else:
            log.warning(f"[MVA_INPUT][WORKAROUND] Could not find input field for dummy MVA.")
    except Exception as e:
        log.warning(f"[MVA_INPUT][WORKAROUND] Dummy MVA query failed: {e}")

    # --- Now process the real MVA list as usual ---
    for mva in mva_list:
        try:
            input_field = mva_input_page.find_input()
            if not (input_field and input_field.is_enabled() and input_field.is_displayed()):
                try:
                    input_field = WebDriverWait(driver, 5, poll_frequency=0.25).until(
                        lambda d: (
                            (f := mva_input_page.find_input()) and f.is_enabled() and f.is_displayed() and f
                        )
                    )
                except TimeoutException:
                    input_field = None
            if not input_field:
                log.error(f"[MVA][FATAL] Could not find MVA input field for {mva}. Exiting script.")
                driver.quit()
                raise SystemExit(1)
            import selenium.webdriver.common.keys as Keys
            # Aggressively clear the field
            for _ in range(3):
                input_field.send_keys(Keys.Keys.CONTROL + 'a')
                input_field.send_keys(Keys.Keys.DELETE)
                input_field.clear()
                time.sleep(0.2)
            # Wait up to 1s (4 x 250ms) for the field to be empty
            for _ in range(4):
                if input_field.get_attribute("value") == "":
                    break
                time.sleep(0.25)
            else:
                log.warning(f"[MVA_INPUT] Field not empty after clearing attempts!")
            # Wait up to 3 seconds for the field to be empty
            for _ in range(15):
                if input_field.get_attribute("value") == "":
                    break
                time.sleep(0.2)
            if input_field.get_attribute("value") != "":
                log.warning(f"[MVA_INPUT] Field not fully cleared before entering new MVA!")
            else:
                log.info(f"[MVA_INPUT] Field cleared before entering new MVA.")
            input_field.send_keys(mva)
            log.info(f"[MVA_INPUT] Entered MVA: {mva}")

            def non_empty_value(label):
                def _predicate(driver):
                    val = get_vehicle_property_by_label(driver, label)
                    return val if val and val != "N/A" else False
                return _predicate

            try:
                vin = WebDriverWait(driver, 12, poll_frequency=0.5).until(non_empty_value("VIN"))
            except Exception:
                vin = "N/A"
            try:
                desc = WebDriverWait(driver, 12, poll_frequency=0.5).until(non_empty_value("Desc"))
            except Exception:
                desc = "N/A"
            # Wait for the UI to echo the new MVA before scraping VIN/Desc
            last8 = mva[-8:]
            try:
                echoed = WebDriverWait(driver, 12, poll_frequency=0.5).until(
                    lambda d: vehicle_properties_page.find_mva_echo(last8, timeout=1)
                )
                log.info(f"[MVA][WAIT] UI echoed MVA {last8}")
            except Exception:
                log.warning(f"[MVA][WAIT] UI did not echo MVA {last8} in time; results may be stale.")
            results.append((mva, vin, desc))
        except Exception as e:
            log.error(f"[MVA][ERROR] {mva} - {e}")
            results.append((mva, "N/A", "N/A"))

    abs_results_path = os.path.abspath(RESULTS_FILE)
    log.info(f"[RESULTS] Writing results to: {abs_results_path}")
    try:
        with open(RESULTS_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["MVA", "VIN", "Desc"])
            writer.writerows(results)
        log.info(f"[RESULTS] Results file written: {abs_results_path}")
    except Exception as e:
        log.error(f"[RESULTS][ERROR] Failed to write results file: {e}")

    quit_driver()

if __name__ == "__main__":
    main()
