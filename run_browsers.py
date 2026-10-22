import csv
import subprocess
import sys
import time
import traceback
from pathlib import Path

from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
SUMMARY_CSV = SCRIPT_DIR / "all_results_joules.csv"
REPEATS = 3
SUMMARY_FIELDS = [
    "run",
    "test_name",
    "browser",
    "config",
    "site",
    "status",
    "duration_s",
    "total_joules",
    "package_joules",
    "pp0_joules",
    "pp1_joules",
    "dram_joules",
    "raw_csv",
]

BRAVE_PATH = Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe")

FIREFOX_ADDONS = {
    "uBlock Origin": SCRIPT_DIR / "uBlock0_1.75.1b16.firefox.signed.xpi",
    "Adblock for Firefox": SCRIPT_DIR / "adblock_for_firefox-6.46.0.xpi",
}

# (browser, config name, firefox addons or profile directory name)
CONFIGURATIONS = [
    ("firefox", "no_adblock", []),
    ("firefox", "ublock_origin", ["uBlock Origin"]),
    ("firefox", "adblock", ["Adblock for Firefox"]),
    ("chrome", "no_blocker", "baseline"),
    ("chrome", "adblock", "AdBlock"),
    ("chrome", "ublock_origin_lite", "uBOL"),
    ("brave", "shields_off", "baseline"),
    ("brave", "shields_on", "brave_shields"),
]
TOR_CONFIGURATIONS = [
    ("tor", "no_noscript", "no_noscript"),
    ("tor", "noscript", "noscript"),
]


class BrowserCrashed(Exception):
    pass


# ---------------------------------------------------------------- EnergiBridge

def start_energibridge(output_path):
    return subprocess.Popen([
        str(SCRIPT_DIR / "energibridge.exe"),
        "--summary",
        "-o",
        str(output_path),
        "python", "-c", "import time; time.sleep(60)",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish_energibridge(eb_process, output_path):
    stdout, stderr = eb_process.communicate()
    if stdout:
        print(f"EnergiBridge stdout: {stdout.decode(errors='replace').strip()}")
    if stderr:
        print(f"EnergiBridge stderr: {stderr.decode(errors='replace').strip()}")
    if eb_process.returncode != 0:
        raise subprocess.CalledProcessError(eb_process.returncode, eb_process.args)
    if not output_path.is_file():
        raise FileNotFoundError(
            f"EnergiBridge did not create the expected CSV: {output_path}"
        )
    print(f"EnergiBridge CSV saved to: {output_path}")


def stop_energibridge(eb_process):
    if eb_process is None:
        return
    if eb_process.poll() is None:
        print("Stopping unfinished EnergiBridge measurement...")
        eb_process.terminate()
        try:
            eb_process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            eb_process.kill()
            eb_process.communicate()
    else:
        eb_process.communicate()


def energy_deltas(csv_path):
    """Energy counters are cumulative, so consumption is last row minus first row."""
    columns = {
        "package_joules": "PACKAGE_ENERGY (J)",
        "pp0_joules": "PP0_ENERGY (J)",
        "pp1_joules": "PP1_ENERGY (J)",
        "dram_joules": "DRAM_ENERGY (J)",
    }
    first = last = None
    with open(csv_path, newline="") as handle:
        for row in csv.DictReader(handle):
            if first is None:
                first = row
            last = row
    result = {key: "" for key in columns}
    duration = ""
    if first is not None:
        for key, column in columns.items():
            try:
                result[key] = round(float(last[column]) - float(first[column]), 4)
            except (KeyError, TypeError, ValueError):
                pass
        try:
            duration = round((float(last["Time"]) - float(first["Time"])) / 1000, 2)
        except (KeyError, TypeError, ValueError):
            pass
    result["duration_s"] = duration
    result["total_joules"] = result["package_joules"]
    return result


class SummaryWriter:
    def __init__(self, path):
        self.handle = open(path, "w", newline="")
        self.writer = csv.DictWriter(self.handle, fieldnames=SUMMARY_FIELDS)
        self.writer.writeheader()
        self.handle.flush()

    def add(self, run, browser, config, site, status, raw_csv=None):
        row = {
            "run": run,
            "test_name": f"{browser} {config} {site}",
            "browser": browser,
            "config": config,
            "site": site,
            "status": status,
            "raw_csv": str(raw_csv) if raw_csv else "",
        }
        if status == "ok" and raw_csv:
            row.update(energy_deltas(raw_csv))
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        self.handle.close()


# --------------------------------------------------------------------- Drivers

def require_file(path, description):
    if not Path(path).is_file():
        raise FileNotFoundError(f"Could not find {description}: {path}")


def require_dir(path, description):
    if not Path(path).is_dir():
        raise FileNotFoundError(f"Could not find {description}: {path}")


def firefox_options():
    options = FirefoxOptions()
    options.set_preference("browser.cache.disk.enable", False)
    options.set_preference("browser.cache.memory.enable", False)
    options.set_preference("browser.cache.offline.enable", False)
    options.set_preference("layers.acceleration.disabled", True)  # Keep rendering on CPU
    options.set_preference("gfx.webrender.all", False)
    return options


def find_tor_browser_dir():
    candidates = [
        Path(r"D:\Tor Browser"),
        Path.home() / "Desktop" / "Tor Browser",
        Path.home() / "Downloads" / "Tor Browser",
        Path(r"C:\Program Files\Tor Browser"),
    ]
    for candidate in candidates:
        if (candidate / "Browser" / "firefox.exe").is_file():
            return candidate
    raise FileNotFoundError("Could not find Tor Browser installation.")


def make_driver(browser, setting):
    geckodriver = SCRIPT_DIR / "geckodriver.exe"
    geckodriver_path = str(geckodriver) if geckodriver.is_file() else "geckodriver.exe"

    if browser == "firefox":
        service = FirefoxService(executable_path=geckodriver_path)
        driver = webdriver.Firefox(service=service, options=firefox_options())
    elif browser == "tor":
        profile_path = SCRIPT_DIR / "tor_profiles" / setting
        require_dir(profile_path, "Tor test profile")
        require_file(geckodriver, "GeckoDriver")
        options = firefox_options()
        options.binary_location = str(find_tor_browser_dir() / "Browser" / "firefox.exe")
        options.profile = str(profile_path)
        service = FirefoxService(executable_path=str(geckodriver))
        driver = webdriver.Firefox(service=service, options=options)
    else:
        options = ChromeOptions()
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
        options.add_argument("--disk-cache-size=0")
        if browser == "chrome":
            driver_path = SCRIPT_DIR / "chromedriver.exe"
            profile_path = SCRIPT_DIR / "chrome_profiles" / setting
            require_file(driver_path, "ChromeDriver")
            require_dir(profile_path, "Chrome profile")
        else:
            driver_path = SCRIPT_DIR / "chromedriver155.exe"
            profile_path = SCRIPT_DIR / "brave_profiles" / setting
            require_file(BRAVE_PATH, "Brave")
            require_file(driver_path, "ChromeDriver")
            require_dir(profile_path, "Brave profile")
            options.binary_location = str(BRAVE_PATH)
            options.add_experimental_option(
                "prefs",
                {
                    "profile": {"exit_type": "Normal", "exited_cleanly": True},
                    "session": {"restore_on_startup": 4},
                },
            )
        options.add_argument(f"--user-data-dir={profile_path}")
        service = ChromeService(executable_path=str(driver_path))
        driver = webdriver.Chrome(service=service, options=options)

    driver.set_window_size(1920, 1080)
    return driver


# -------------------------------------------------------------------- Browsing

class Browsing:
    """Site routines shared by every browser; behaviour follows run_firefox.py."""

    def __init__(self, driver):
        self.driver = driver
        self.main_window = driver.current_window_handle

    def alive(self):
        try:
            self.driver.window_handles
            return True
        except WebDriverException:
            return False

    def close_popup_windows(self):
        driver = self.driver
        for handle in driver.window_handles:
            if handle != self.main_window:
                driver.switch_to.window(handle)
                if "getadblock.com/en/installed/" in driver.current_url:
                    print("Closing AdBlock installation tab...")
                driver.close()
        driver.switch_to.window(self.main_window)

    def close_adblock_thank_you_tabs(self):
        """AdBlock opens its thank-you page as an extra (sometimes the only) tab."""
        driver = self.driver
        handles = driver.window_handles
        keep = None
        for handle in handles:
            driver.switch_to.window(handle)
            if "getadblock.com" in driver.current_url and len(driver.window_handles) > 1:
                print("Closing AdBlock installation tab...")
                driver.close()
            elif keep is None:
                keep = handle
        remaining = driver.window_handles
        self.main_window = keep if keep in remaining else remaining[0]
        driver.switch_to.window(self.main_window)
        if "getadblock.com" in driver.current_url:
            driver.get("about:blank")

    def wait_ignoring_popups(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.close_popup_windows()
            self.close_bbc_privacy_prompt()
            self.close_bbc_register_modal()
            time.sleep(max(0, min(0.5, deadline - time.monotonic())))

    def close_bbc_privacy_prompt(self):
        driver = self.driver
        privacy_frames = driver.find_elements(
            By.CSS_SELECTOR, 'iframe[id^="sp_message_iframe"]'
        )
        if not privacy_frames:
            return
        driver.switch_to.frame(privacy_frames[0])
        try:
            accept_buttons = driver.find_elements(
                By.XPATH,
                "//button[contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept') "
                "or contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree')]",
            )
            if accept_buttons:
                print("Accepting BBC privacy prompt...")
                accept_buttons[0].click()
            else:
                print("BBC privacy prompt is present, but no accept button was found.")
        finally:
            driver.switch_to.default_content()

    def close_bbc_register_modal(self):
        close_buttons = self.driver.find_elements(
            By.CSS_SELECTOR, "article.pn-article button.pn-article__close"
        )
        if close_buttons:
            print("Closing BBC registration modal...")
            close_buttons[0].click()

    def click_imgur_overlay_if_present(self):
        overlay_buttons = self.driver.find_elements(By.CSS_SELECTOR, ".bB48ietO")
        if overlay_buttons:
            print("Clicking Imgur overlay...")
            overlay_buttons[0].click()

    def wait_with_imgur_overlay_checks(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.click_imgur_overlay_if_present()
            time.sleep(max(0, min(0.25, deadline - time.monotonic())))

    def wait_for_imgur_clickable(self, locator):
        return WebDriverWait(self.driver, 20, poll_frequency=0.25).until(
            lambda current_driver: (
                self.click_imgur_overlay_if_present()
                or EC.element_to_be_clickable(locator)(current_driver)
            )
        )

    def click_bingeflix_link(self, xpath):
        driver = self.driver
        for attempt in range(20):
            starting_url = driver.current_url
            try:
                WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                ).click()
            except ElementClickInterceptedException:
                print(
                    "BingeFlix click intercepted; clicking the page to trigger "
                    f"the popup before retrying ({attempt + 1}/20)..."
                )
                ActionChains(driver).move_to_element(
                    driver.find_element(By.TAG_NAME, "body")
                ).click().perform()
                popup_deadline = time.monotonic() + 5
                while time.monotonic() < popup_deadline:
                    if set(driver.window_handles) - {self.main_window}:
                        break
                    time.sleep(0.25)
                self.close_popup_windows()
                time.sleep(1)
                continue

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if set(driver.window_handles) - {self.main_window}:
                    self.close_popup_windows()
                    print(f"BingeFlix opened a popup tab; retrying click ({attempt + 1}/20)...")
                    break
                if driver.current_url != starting_url:
                    self.wait_ignoring_popups(2)
                    if set(driver.window_handles) == {self.main_window}:
                        return
                time.sleep(0.5)
            else:
                self.close_popup_windows()
                if driver.current_url != starting_url:
                    return

        raise RuntimeError(
            "BingeFlix link did not navigate without an intercepted click or popup "
            f"after 20 attempts: {xpath}"
        )

    def click_when_ready(self, by, selector, timeout=20):
        WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, selector))
        ).click()

    # ------------------------------------------------------------------ sites

    def archive(self):
        driver = self.driver
        print("Loading Archive.org...")
        pages = [
            "https://archive.org/",
            "https://archive.org/details/genealogy",
            "https://archive.org/details/agspublications",
            "https://archive.org/details/1885NMTerritorialCensusIndexVoI2",
            "https://archive.org/details/movies",
        ]
        for page_url in pages:
            print(f"Loading Archive.org page: {page_url}")
            driver.get(page_url)
            time.sleep(5)

    def guardian(self):
        driver = self.driver
        print("Loading The Guardian...")
        for page_url in [
            "https://www.theguardian.com/europe",
            "https://www.theguardian.com/world/2026/oct/21/europe-far-right-protest-violence-authoritarian-agenda",
            "https://www.theguardian.com/uk/sport",
            "https://www.theguardian.com/sport/formulaone",
            "https://www.theguardian.com/sport/2026/oct/21/austin-gp-about-much-more-than-potential-kimi-antonelli-first-f1-world-title",
            "https://www.theguardian.com/uk/commentisfree",
            "https://www.theguardian.com/commentisfree/2026/oct/22/prince-harry-david-brent-reboot-purpose",
        ]:
            print(f"Loading Guardian page: {page_url}")
            driver.get(page_url)
            time.sleep(5)

    def bbc(self):
        driver = self.driver
        print("Loading BBC...")
        driver.get("https://www.bbc.com/")
        self.wait_ignoring_popups(5)
        consent_button = driver.find_elements(By.XPATH, '//*[@id="notice"]/div[3]/div[2]/button')
        if consent_button:
            print("Dismissing BBC notice...")
            consent_button[0].click()
            self.wait_ignoring_popups(5)
        self.close_bbc_register_modal()

        print("Clicking BBC navigation item...")
        self.click_when_ready(
            By.XPATH, '//*[@id="main-navigation-container"]/section/nav/ul/li[2]'
        )
        self.wait_ignoring_popups(5)

        print("Loading BBC article...")
        driver.get("https://www.bbc.com/news/articles/cr9dzpx2xl0lo")
        self.wait_ignoring_popups(5)

        print("Clicking BBC navigation menu...")
        self.click_when_ready(
            By.XPATH, '//*[@id="main-navigation-container"]/section/nav/ul/li[5]/div'
        )
        self.wait_ignoring_popups(5)
        self.close_bbc_register_modal()

    def bingeflix(self):
        driver = self.driver
        print("Loading BingeFlix...")
        driver.get("https://bingeflix.tv/")
        self.close_popup_windows()
        print("Clicking BingeFlix first link...")
        self.click_bingeflix_link(
            "/html/body/div[2]/main/div/section[1]/div/div[5]/div/div[3]/a[1]"
        )
        self.wait_ignoring_popups(15)

        print("Returning to BingeFlix...")
        self.close_popup_windows()
        driver.get("https://bingeflix.tv/")
        self.wait_ignoring_popups(5)

        print("Clicking BingeFlix second homepage link...")
        self.close_popup_windows()
        self.click_bingeflix_link(
            "/html/body/div[2]/main/div/section[1]/div/div[5]/div/div[3]/a[2]"
        )
        self.wait_ignoring_popups(5)

        print("Loading BingeFlix movie details...")
        self.close_popup_windows()
        driver.get("https://bingeflix.tv/movie/1400940/details")
        self.wait_ignoring_popups(5)

        print("Clicking BingeFlix third link...")
        self.close_popup_windows()
        self.click_bingeflix_link("/html/body/div[2]/main/div/div[3]/div/div/div/a[1]")
        self.wait_ignoring_popups(5)

        print("Clicking BingeFlix final link...")
        self.close_popup_windows()
        self.click_bingeflix_link(
            "/html/body/div[2]/main/div/div[1]/div[2]/div/div/div[4]/a"
        )
        self.wait_ignoring_popups(5)
        self.close_popup_windows()

    def imgur(self):
        driver = self.driver
        print("Loading Imgur...")
        for page_url in [
            "https://imgur.com/",
            "https://imgur.com/t/funny?source=featured_tag_module",
            "https://imgur.com/gallery/well-h1o0HuT#/t/funny",
            "https://imgur.com/",
            "https://imgur.com/t/aww?source=featured_tag_module",
            "https://imgur.com/gallery/20-nice-wholesome-things-to-brighten-day-vol-354-t4H22k9#/t/aww",
        ]:
            print(f"Loading Imgur page: {page_url}")
            driver.get(page_url)
            time.sleep(7)

    def facebook(self):
        driver = self.driver
        print("Loading Facebook...")
        for page_url in [
            "https://www.facebook.com/Enschede",
            "https://www.facebook.com/reel/1314189579915644",
            "https://www.facebook.com/reel/9354689061208867",
            "https://www.facebook.com/photo?fbid=1508512471309691&set=pcb.1508573737970231",
            "https://www.facebook.com/photo?fbid=1507415151419423&set=pcb.1507421074752164",
        ]:
            print(f"Loading Facebook page: {page_url}")
            driver.get(page_url)
            time.sleep(7)

    def x(self):
        driver = self.driver
        print("Loading X...")
        driver.get("https://x.com/Gem_Enschede")
        time.sleep(10)
        if driver.find_elements(By.XPATH, "/html/body/div[2]"):
            dialog_button = driver.find_elements(
                By.XPATH, "/html/body/div[2]/div/div[2]/button[1]"
            )
            if dialog_button:
                print("Clicking X dialog button...")
                dialog_button[0].click()
                time.sleep(10)

        for page_url in [
            "https://x.com/Gem_Enschede/status/1917526303953281099",
            "https://x.com/EllieB87980022/status/2102669988976894106",
            "https://x.com/EllieB87980022",
            "https://x.com/EllieB87980022/status/1374310066925531140/photo/1",
        ]:
            print(f"Loading X page: {page_url}")
            driver.get(page_url)
            time.sleep(10)

    def espn(self):
        driver = self.driver
        print("Loading ESPN...")
        driver.get("https://www.espn.nl/")
        time.sleep(5)
        if driver.find_elements(By.XPATH, "/html/body/div[7]/div[2]/div"):
            consent_buttons = driver.find_elements(
                By.XPATH, '//*[@id="onetrust-accept-btn-handler"]'
            )
            if consent_buttons:
                print("Accepting ESPN cookie consent...")
                consent_buttons[0].click()
                time.sleep(5)

        nav_item = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located(
                (By.XPATH, "/html/body/div[4]/div[2]/header/nav[1]/ul/li[1]/a/span/span[1]")
            )
        )
        print("Hovering ESPN navigation item...")
        ActionChains(driver).move_to_element(nav_item).perform()
        time.sleep(5)

        print("Clicking ESPN navigation link...")
        self.click_when_ready(
            By.XPATH,
            "/html/body/div[4]/div[2]/header/nav[1]/ul/li[1]/div/ul[3]/li/div/div/ul[2]/li[1]/a/span/span[1]",
        )
        time.sleep(5)

        print("Clicking ESPN navigation tab...")
        driver.find_element(
            By.XPATH,
            "/html/body/div[1]/div/div/div/div/div/main/div[1]/div/div[2]/nav/div/ul/li[3]/a/span",
        ).click()
        time.sleep(5)

        print("Opening ESPN selector...")
        driver.find_element(
            By.XPATH,
            "/html/body/div[1]/div/div/div/div/div/main/div[1]/div/div[5]/div/div[1]/section/div/section/div[2]/div/select[1]",
        ).click()
        time.sleep(5)

        print("Selecting ESPN option 12...")
        driver.find_element(
            By.XPATH,
            "/html/body/div[1]/div/div/div/div/div/main/div[1]/div/div[5]/div/div[1]/section/div/section/div[2]/div/select[1]/option[12]",
        ).click()
        time.sleep(5)

    def wikipedia(self):
        driver = self.driver
        print("Loading Wikipedia...")
        for page_url in [
            "https://en.wikipedia.org/wiki/Enschede",
            "https://en.wikipedia.org/wiki/Association_football",
            "https://en.wikipedia.org/wiki/Team_sport",
            "https://en.wikipedia.org/wiki/Team",
            "https://en.wikipedia.org/wiki/Human",
        ]:
            print(f"Loading Wikipedia page: {page_url}")
            driver.get(page_url)
            time.sleep(7)


SITES = [
    ("archive", "archive"),
    ("guardian", "guardian"),
    ("bbc", "bbc"),
    ("bingeflix", "bingeflix"),
    ("imgur", "imgur"),
    ("facebook", "facebook"),
    ("x", "x"),
    ("espn", "espn"),
    ("wikipedia", "wikipedia"),
]


# ---------------------------------------------------------------------- Runner

def run_configuration(run, browser, config, setting, summary, failures):
    """One browser session per configuration; every site is measured inside it."""
    print(f"\n=== Run {run}/{REPEATS}: {browser} / {config} ===")
    driver = None
    completed = 0
    try:
        driver = make_driver(browser, setting)
        browsing = Browsing(driver)

        if browser == "firefox":
            for addon in setting:
                addon_path = FIREFOX_ADDONS[addon]
                require_file(addon_path, f"{addon} extension")
                print(f"Installing {addon}...")
                driver.install_addon(str(addon_path), temporary=True)
                time.sleep(5)  # Allow extension filter lists to initialize
                browsing.close_popup_windows()
        else:
            print(f"Using {browser} profile: {setting}")
            time.sleep(5)
            browsing.close_adblock_thank_you_tabs()
        browsing.close_popup_windows()

        for index, (site, method_name) in enumerate(SITES):
            test_name = f"run{run} {browser} {config} {site}"
            raw_csv = RESULTS_DIR / f"{browser}_{config}_{site}_run{run}.csv"
            eb_process = None
            try:
                print(f"\nStarting {test_name} EnergiBridge measurement...")
                eb_process = start_energibridge(raw_csv)
                getattr(browsing, method_name)()
                print(f"Waiting for {test_name} EnergiBridge measurement to finalize CSV...")
                browsing.close_popup_windows()
                finish_energibridge(eb_process, raw_csv)
                summary.add(run, browser, config, site, "ok", raw_csv)
                completed += 1
            except Exception as error:
                stop_energibridge(eb_process)
                message = f"{type(error).__name__}: {str(error).strip().splitlines()[0] if str(error).strip() else ''}"
                print(f"FAILED {test_name}: {message}")
                traceback.print_exc()
                failures.append((test_name, message))
                if not browsing.alive():
                    crash = f"browser crashed ({message})"
                    summary.add(run, browser, config, site, f"error: {crash}")
                    for later_site, _ in SITES[index + 1:]:
                        summary.add(run, browser, config, later_site, "not run: browser crashed")
                        failures.append((f"run{run} {browser} {config} {later_site}", "not run: browser crashed"))
                    raise BrowserCrashed(crash)
                summary.add(run, browser, config, site, f"error: {message}")
                # Return to a clean state so the next site is unaffected.
                try:
                    browsing.close_popup_windows()
                except WebDriverException:
                    pass
    except BrowserCrashed as error:
        print(f"Browser session for {browser} / {config} aborted: {error}")
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        print(f"Configuration {browser} / {config} failed before/while running: {message}")
        traceback.print_exc()
        if completed == 0:
            for site, _ in SITES:
                summary.add(run, browser, config, site, f"error: {message}")
                failures.append((f"run{run} {browser} {config} {site}", message))
    finally:
        if driver is not None:
            print("Closing browser...")
            try:
                driver.quit()
            except WebDriverException as error:
                print(f"driver.quit() failed: {error}")


def main():
    include_tor = "--no-tor" not in sys.argv
    RESULTS_DIR.mkdir(exist_ok=True)
    summary = SummaryWriter(SUMMARY_CSV)
    failures = []
    try:
        configurations = list(CONFIGURATIONS)
        if include_tor:
            configurations += TOR_CONFIGURATIONS  # Tor always runs last
        for run in range(1, REPEATS + 1):
            for browser, config, setting in configurations:
                run_configuration(run, browser, config, setting, summary, failures)
    finally:
        summary.close()

    print(f"\nSummary CSV written to: {SUMMARY_CSV}")
    if failures:
        print(f"\n{len(failures)} test(s) failed:")
        for name, message in failures:
            print(f"  - {name}: {message}")
    else:
        print("All tests completed successfully.")


if __name__ == "__main__":
    main()
