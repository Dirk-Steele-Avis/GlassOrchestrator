import time
from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


from core.navigator import Navigator
from config.config_loader import get_config
from utils.logger import log
from utils.ui_helpers import click_element, is_element_present, send_text


class LoginPage:
    def __init__(self, driver):
        self.driver = driver
        self.delay_seconds = get_config("delay_seconds", 4)
        self.wwid_entry_timeout_seconds = int(get_config("wwid_entry_timeout_seconds", 25))
        self.login_url = get_config(
            "login_url",
            "https://avisbudget.palantirfoundry.com/workspace/fleet-operations-pwa/health",
        )
        self.compass_app_label = get_config("compass_app_label", "Compass Mobile")

    def is_logged_in(self):
        """Check if Compass Mobile session is already authenticated."""
        
        log.info("[DEBUG] inside is_logged_in")
        elems = self.driver.find_elements(By.XPATH, f"//span[contains(text(),'{self.compass_app_label}')]")
        return len(elems) > 0

    def _is_wwid_page(self, timeout: int = 3) -> bool:
        """Return True when the Compass WWID prompt is visible."""
        return is_element_present(
            self.driver,
            (By.CSS_SELECTOR, "input[class*='fleet-operations-pwa__text-input__']"),
            timeout,
        )

    def _is_compass_app_page(self, timeout: int = 3) -> bool:
        """Return True only when Compass app UI is positively confirmed."""
        has_mva_input = is_element_present(
            self.driver,
            (By.CSS_SELECTOR, "input[placeholder*='MVA'], input[id*='mva'], input[name*='mva']"),
            timeout,
        )
        has_add_work_item_button = is_element_present(
            self.driver,
            (By.XPATH, "//button[normalize-space()='Add Work Item']"),
            timeout,
        )
        return has_mva_input and has_add_work_item_button

    def _open_compass_mobile_tile(self, timeout: int = 10) -> bool:
        """Open Compass Mobile from the Foundry launcher if present."""
        selectors = [
            (By.XPATH, f"//a[@role='button']//span[contains(normalize-space(.), '{self.compass_app_label}')]"),
            (By.XPATH, f"//a[@role='button'][.//*[contains(normalize-space(.), '{self.compass_app_label}')]]"),
            (By.XPATH, f"//button[.//*[contains(normalize-space(.), '{self.compass_app_label}')]]"),
            (By.XPATH, f"//*[contains(normalize-space(.), '{self.compass_app_label}') and (@role='button' or self::a or self::button)]"),
        ]

        original_handles = set(self.driver.window_handles)
        for locator in selectors:
            if click_element(self.driver, locator, timeout=timeout, desc="Compass Mobile tile"):
                # Compass may open in a new tab; switch if that happens.
                time.sleep(1)
                new_handles = [h for h in self.driver.window_handles if h not in original_handles]
                if new_handles:
                    self.driver.switch_to.window(new_handles[-1])
                    log.info("[LOGIN] Switched to Compass Mobile tab")
                return True

        return False

    def _is_compass_launcher_page(self, timeout: int = 2) -> bool:
        """Return True when the Foundry launcher tile for Compass is visible."""
        return is_element_present(
            self.driver,
            (By.XPATH, f"//*[contains(normalize-space(.), '{self.compass_app_label}')]"),
            timeout,
        )

    def _is_login_form_page(self, timeout: int = 2) -> bool:
        """Return True when Microsoft email login form is visible."""
        return is_element_present(self.driver, (By.NAME, "loginfmt"), timeout)

    def _is_element_present_now(self, by: str, selector: str) -> bool:
        """Fast, non-blocking element presence check for tight polling loops."""
        try:
            return len(self.driver.find_elements(by, selector)) > 0
        except Exception:
            return False

    def _wait_for_post_auth_landing(self, timeout: int = 30) -> str:
        """Wait for redirect away from multipass into a known landing state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_element_present_now(By.CSS_SELECTOR, "input[class*='fleet-operations-pwa__text-input__']"):
                return "wwid"
            has_mva_input = self._is_element_present_now(
                By.CSS_SELECTOR,
                "input[placeholder*='MVA'], input[id*='mva'], input[name*='mva']",
            )
            has_add_work_item_button = self._is_element_present_now(
                By.XPATH,
                "//button[normalize-space()='Add Work Item']",
            )
            if has_mva_input and has_add_work_item_button:
                return "app"
            if self._is_element_present_now(By.XPATH, f"//*[contains(normalize-space(.), '{self.compass_app_label}')]"):
                return "launcher"
            if self._is_element_present_now(By.NAME, "loginfmt"):
                return "login_form"

            url = self.driver.current_url
            if "/workspace/module/view/" in url or "/workspace/fleet-operations-pwa/" in url:
                return "workspace"

            time.sleep(0.5)

        return "unknown"

    def _complete_interactive_login(self, username: str, password: str) -> dict:
        """Perform Microsoft interactive login if session drops to login form."""
        try:
            if not send_text(self.driver, (By.NAME, "loginfmt"), username, timeout=10):
                return {"status": "failed", "reason": "email_entry_failed"}
            if not click_element(self.driver, (By.ID, "idSIButton9"), timeout=10, desc="Next button"):
                return {"status": "failed", "reason": "email_submit_failed"}

            if not send_text(self.driver, (By.NAME, "passwd"), password, timeout=10):
                return {"status": "failed", "reason": "password_entry_failed"}
            if not click_element(self.driver, (By.ID, "idSIButton9"), timeout=10, desc="Sign in button"):
                return {"status": "failed", "reason": "password_submit_failed"}

            # Optional: dismiss 'Stay signed in?' if shown.
            click_element(self.driver, (By.ID, "idBtn_Back"), timeout=3, desc="Stay signed in No")

            # Optional: if account picker appears, click first available tile.
            click_element(self.driver, (By.XPATH, "(//div[contains(@data-testid, 'account-tile')])[1]"), timeout=5, desc="First SSO account tile")
            return {"status": "ok"}
        except Exception as e:
            log.error(f"[LOGIN] Interactive login fallback failed: {e}")
            return {"status": "failed", "reason": "interactive_login_exception"}

    def ensure_logged_in(self, username: str, password: str, _login_id: str):
        # Always navigate first. Login endpoint may immediately redirect to
        # workspace/launcher/WWID states, so URL verification here is noisy.
        Navigator(self.driver).go_to(
            self.login_url, label="Login page", verify=False
        )

        # Handle automatic login redirection
        if "/multipass/automatic-login" in self.driver.current_url:
            log.info("[LOGIN] Automatic login page detected. Waiting for post-auth landing state.")
            state = self._wait_for_post_auth_landing(timeout=30)
            if state == "login_form":
                log.info("[LOGIN] Session fell back to interactive Microsoft login.")
                res = self._complete_interactive_login(username, password)
                if res["status"] != "ok":
                    return res
                state = self._wait_for_post_auth_landing(timeout=30)

            if state in {"workspace", "wwid", "app", "launcher"}:
                log.info(f"[LOGIN] Automatic-login settled on state: {state}")
                return {"status": "ok"}

            log.error("[LOGIN] Automatic-login did not settle into a known state.")
            return {"status": "failed", "reason": "automatic_login_unsettled"}

        # Check if already on the workspace page after redirection
        if "/workspace/module/view/" in self.driver.current_url or "/workspace/fleet-operations-pwa/" in self.driver.current_url:
            log.info("[LOGIN] Already on workspace page after redirection. Considering logged in.")
            return {"status": "ok"}

        if self._is_compass_launcher_page(timeout=3):
            log.info("[LOGIN] Foundry launcher detected; treating as authenticated.")
            return {"status": "ok"}

        # Newer flow often lands directly on WWID without showing email/password form.
        if self._is_wwid_page(timeout=5) or self._is_compass_app_page(timeout=3):
            log.info("[LOGIN] Session already authenticated (WWID/app page detected).")
            return {"status": "ok"}

        # If not on workspace, check if login form is present and perform login
        # We need to check for the email field to determine if we are on the login page
        email_field_present = False
        try:
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.NAME, "loginfmt"))
            )
            email_field_present = True
        except TimeoutException:
            pass

        if email_field_present:
            log.info("[LOGIN] Login form detected; running interactive fallback.")
            return self._complete_interactive_login(username, password)
        else:
            log.error("[LOGIN] Neither workspace nor login form detected after navigation. Unexpected state.")
            return {"status": "failed", "reason": "unexpected_page_state"}

    def enter_wwid(self, login_id: str):
        """Actually type and submit the WWID once."""
        try:
            wwid_locator = (
                By.CSS_SELECTOR,
                "input[class*='fleet-operations-pwa__text-input__']",
            )

            # Give WWID page time to fully hydrate before interacting.
            log.info("[LOGIN][WWID] Starting pre-entry pause (10s)")
            time.sleep(10)
            log.info("[LOGIN][WWID] Completed pre-entry pause (10s)")

            # Wait until WWID field is actually interactable.
            log.info("[LOGIN][WWID] Waiting for WWID field to become clickable (timeout=30s)")
            wwid_input = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable(wwid_locator)
            )
            log.info("[LOGIN][WWID] WWID field is clickable")

            # Use send_text for the actual entry
            if not send_text(
                self.driver,
                wwid_locator,
                login_id,
                timeout=self.wwid_entry_timeout_seconds,
            ):
                return {"status": "failed", "reason": "wwid_entry_failed"}

            # Prove whether the typed value is present immediately and over time.
            entered_value = (wwid_input.get_attribute("value") or "").strip()
            masked_login_id = f"***{login_id[-2:]}" if len(login_id) >= 2 else "***"
            masked_entered = f"***{entered_value[-2:]}" if len(entered_value) >= 2 else "***"
            log.info(
                "[LOGIN][WWID][PROOF] immediate field check: entered=%s expected=%s match=%s",
                masked_entered,
                masked_login_id,
                entered_value == login_id,
            )

            # Hold after typing so reactive validation can settle, and sample field value.
            for second in range(1, 6):
                time.sleep(1)
                sampled_value = (wwid_input.get_attribute("value") or "").strip()
                masked_sampled = f"***{sampled_value[-2:]}" if len(sampled_value) >= 2 else "***"
                log.info(
                    "[LOGIN][WWID][PROOF] +%ss field check: entered=%s expected=%s match=%s",
                    second,
                    masked_sampled,
                    masked_login_id,
                    sampled_value == login_id,
                )

            # Submit via button because Enter key behavior is inconsistent here.
            submit_locator = (By.XPATH, "//button[.//span[normalize-space()='Submit']]")
            overlay_locator = (By.CSS_SELECTOR, "div[class*='fleet-operations-pwa__overlay__']")
            submit_clicked = False

            for attempt in range(1, 6):
                try:
                    # Overlay is transient; wait briefly for it to disappear before clicking.
                    WebDriverWait(self.driver, 3).until(
                        EC.invisibility_of_element_located(overlay_locator)
                    )
                except TimeoutException:
                    pass

                try:
                    submit_button = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable(submit_locator)
                    )
                    submit_button.click()
                    submit_clicked = True
                    break
                except ElementClickInterceptedException:
                    log.warning("[LOGIN][WWID] Submit click intercepted (attempt %d/5)", attempt)
                    time.sleep(1)
                except TimeoutException:
                    log.warning("[LOGIN][WWID] Submit button not clickable yet (attempt %d/5)", attempt)
                    time.sleep(1)

            if not submit_clicked:
                # Final fallback: JS click in case Selenium click is repeatedly intercepted.
                try:
                    submit_button = self.driver.find_element(*submit_locator)
                    self.driver.execute_script("arguments[0].click();", submit_button)
                    submit_clicked = True
                    log.info("[LOGIN][WWID] Submit clicked via JS fallback")
                except Exception:
                    pass

            if not submit_clicked:
                log.warning(f"[LOGIN][WARN] Could not click WWID submit button")
                return {"status": "failed", "reason": "wwid_submit_failed"}

            log.info(f"[LOGIN] WWID submitted via button")
            return {"status": "ok"}

        except Exception as e:
            log.error(f"[LOGIN][ERROR] Unexpected error entering WWID: {e}")
            return {"status": "failed", "reason": "exception"}

    def ensure_user_context(self, login_id: str):
        """Ensure WWID is entered once Compass Mobile is loaded."""
        if self._is_compass_app_page(timeout=2):
            log.info("[LOGIN] Already inside Compass app; user context is ready.")
            return {"status": "ok"}

        if self._is_wwid_page(timeout=3):
            log.info("[LOGIN] Proceeding to WWID entry")
            return self.enter_wwid(login_id)

        if self._open_compass_mobile_tile(timeout=8):
            if self._is_wwid_page(timeout=8):
                log.info("[LOGIN] Compass tile opened; proceeding to WWID entry")
                return self.enter_wwid(login_id)
            if self._is_compass_app_page(timeout=5):
                log.info("[LOGIN] Compass tile opened directly into app page.")
                return {"status": "ok"}

        log.error("[LOGIN] Could not establish Compass user context (WWID/app not detected).")
        return {"status": "failed", "reason": "user_context_not_detected"}

    def ensure_ready(self, username: str, password: str, login_id: str):
        """
        High-level pretest setup:
        1) ensure_logged_in
        2) ensure_user_context(WWID)
        """
        log.info(f"[DEBUG] before ensure_logged_in")

        res = self.ensure_logged_in(username, password, login_id)
        log.debug(f"[LOGIN] ensure_logged_in - {res}")
        time.sleep(self.delay_seconds)
        if res["status"] != "ok":
            return res

        res = self.ensure_user_context(login_id)
        return res
