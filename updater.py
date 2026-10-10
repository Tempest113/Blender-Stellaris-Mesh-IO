"""Checks GitHub for a newer release. Author: ross-g."""

import json
import logging
from datetime import date, datetime
from os.path import splitext
from time import perf_counter
from urllib.request import Request, URLError, urlopen

from . import IO_PDX_INFO, IO_PDX_SETTINGS

UPDATER_LOG = logging.getLogger("io_pdx.updater")


class Github_API(object):
    API_URL = "https://api.github.com"

    def __init__(self, owner, repo):
        self.api = self.API_URL
        self.owner = owner
        self.repo = repo
        self.args = {"owner": self.owner, "repo": self.repo, "api": self.api}

        self.AT_LATEST = True
        self.LATEST_VERSION = "0.0.0"
        self.LATEST_RELEASE = "https://github.com/{owner}/{repo}/releases/latest".format(**self.args)
        self.LATEST_NOTES = ""
        self.LATEST_URL = {}
        self.CURRENT_VERSION = IO_PDX_INFO["version"]
        self.refresh()

    @staticmethod
    def parse_version(text):
        parts = tuple(int(n) for n in str(text).lstrip("vV").split("."))
        while parts and parts[-1] == 0:
            parts = parts[:-1]
        return parts

    @staticmethod
    def get_data(url, time=1.0):
        req = Request(url)
        result = urlopen(req, timeout=time)
        result_str = result.read()
        result.close()

        return json.JSONDecoder().decode(result_str.decode())

    def refresh(self, force=False):
        recheck = True

        last_check_date = IO_PDX_SETTINGS.last_update_check
        if last_check_date is not None:
            recheck = date.today() > datetime.strptime(last_check_date, "%Y-%m-%d").date()

        if recheck or force:
            start = perf_counter()

            releases_url = "{api}/repos/{owner}/{repo}/releases".format(**self.args)

            try:
                release_list = self.get_data(releases_url)
                latest = release_list[0]
                self.LATEST_RELEASE = latest.get("html_url", self.LATEST_RELEASE)
            except URLError as err:
                UPDATER_LOG.warning(f"Unable to check for update. ({err.reason})")
                return
            except IndexError as err:
                UPDATER_LOG.warning(f"Found no releases during update check. ({err})")
                return
            except Exception as err:
                UPDATER_LOG.error(f"Failed during update check. ({err})")
                return

            try:
                latest_version = str(latest["tag_name"]).lstrip("vV")
                self.parse_version(latest_version)
            except (KeyError, ValueError):
                UPDATER_LOG.warning(f"Ignoring latest release, tag isn't a version number. ({latest.get('tag_name')})")
                return

            self.LATEST_VERSION = latest_version
            self.LATEST_URL = {
                splitext(asset["name"])[0].split("-")[0]: asset["browser_download_url"] for asset in latest["assets"]
            }
            self.LATEST_NOTES = (
                f"{latest['published_at'].split('T')[0]}\r\n"
                f"Release version: {latest['tag_name']}\r\n"
                f"{latest['body']}"
            )

            IO_PDX_SETTINGS.github_latest_version = self.LATEST_VERSION
            IO_PDX_SETTINGS.github_latest_url = self.LATEST_URL
            IO_PDX_SETTINGS.github_latest_notes = self.LATEST_NOTES

            IO_PDX_SETTINGS.last_update_check = f"{date.today()}"
            UPDATER_LOG.info(f"Checked for update. ({perf_counter() - start:0.4f} sec)")

        else:
            self.LATEST_VERSION = str(IO_PDX_SETTINGS.github_latest_version or "0.0.0")
            self.LATEST_URL = IO_PDX_SETTINGS.github_latest_url
            if not isinstance(self.LATEST_URL, dict):
                self.LATEST_URL = {}
            self.LATEST_NOTES = IO_PDX_SETTINGS.github_latest_notes or ""

            UPDATER_LOG.info("Skipped update check. (already ran today)")

        try:
            self.AT_LATEST = self.parse_version(self.CURRENT_VERSION) >= self.parse_version(self.LATEST_VERSION)
        except ValueError:
            self.AT_LATEST = True


github = Github_API(*IO_PDX_INFO["website"].rstrip("/").split("/")[-2:])
