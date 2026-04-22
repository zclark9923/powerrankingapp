"""Send Discord webhook alerts for watched Ottoneu lineup players during MLB games.

Usage:
    set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    python ottoneu_friend_notifier.py --watchlist ottoneu_lineup_watchlist.csv

Optional env vars:
    NOTIFIER_POLL_SECONDS=90
    NOTIFIER_DATE=YYYY-MM-DD

Watchlist CSV columns:
    player_name,mlbam_id,role
Where role is optional and one of: hitter, pitcher, both.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import tempfile
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

try:
    import websocket  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    websocket = None

STATS_API = "https://statsapi.mlb.com/api/v1"
LIVE_FEED_TEMPLATE = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
GAME_CONTENT_TEMPLATE = "https://statsapi.mlb.com/api/v1/game/{game_pk}/content"
PEOPLE_TEMPLATE = "https://statsapi.mlb.com/api/v1/people/{player_id}?hydrate=currentTeam"
PEOPLE_SEARCH_TEMPLATE = (
    "https://statsapi.mlb.com/api/v1/people/search?names={name}&sportId=1"
)
STATE_PATH = Path(__file__).with_name("ottoneu_friend_notifier_state.json")
DEFAULT_WATCHLIST = Path(__file__).with_name("ottoneu_lineup_watchlist.csv")
NICKNAMES_PATH = Path(__file__).with_name("nicknames.csv")
DISCORD_API = "https://discord.com/api/v10"
DEFAULT_IDLE_SECONDS = 900
DEFAULT_WATCHLIST_REFRESH_SECONDS = 600
DEFAULT_POSTFINAL_POLL_SECONDS = 300
DEFAULT_POSTFINAL_HIGHLIGHT_SECONDS = 1800
DISCORD_GATEWAY_INTENT_GUILD_MESSAGES = 1 << 9
DISCORD_GATEWAY_INTENT_MESSAGE_CONTENT = 1 << 15

EVENT_EMOJIS = {
    "lineup": "🧢",
    "single": "🟢",
    "double": "🟠",
    "triple": "🔺",
    "home_run": "💥",
    "strikeout": "🌀",
    "stolen_base": "🛼",
    "caught_stealing": "🚫",
    "milestone": "📈",
    "highlight": "🎬",
    "final": "📊",
}

NOTABLE_BATTER_EVENTS = {
    "home_run",
    "triple",
    "double",
    "single",
    "stolen_base",
    "caught_stealing",
}

NOTABLE_PITCHER_EVENTS = {
    "home_run",
}

# SABR Points scoring system
SABR_HITTING_POINTS = {
    "at_bat": -1.0,
    "hit": 5.6,
    "double": 2.9,
    "triple": 5.7,
    "home_run": 9.4,
    "walk": 3.0,
    "hbp": 3.0,
    "stolen_base": 1.9,
    "caught_stealing": -2.8,
}

SABR_PITCHING_POINTS = {
    "ip": 5.0,
    "strikeout": 2.0,
    "walk": -3.0,
    "hbp": -3.0,
    "home_run": -13.0,
    "save": 5.0,
    "hold": 4.0,
}

CF_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies to continue",
    "cf-chl",
    "cloudflare",
)

_NAME_RESOLVE_CACHE: dict[str, int | None] = {}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        # Keep explicit shell env values higher priority than .env.
        os.environ.setdefault(key, value)


def bootstrap_env() -> None:
    here = Path(__file__).resolve().parent
    candidates = [here / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        load_env_file(candidate)


bootstrap_env()


@dataclass(frozen=True)
class WatchPlayer:
    player_id: int
    name: str
    role: str  # hitter | pitcher | both


@dataclass(frozen=True)
class DiscordHtmlAttachment:
    message_id: str
    created_at: str
    filename: str
    url: str


@dataclass(frozen=True)
class DiscordCommandMessage:
    message_id: str
    author_id: str
    content: str
    source: str = "poll"
    created_at: str = ""


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href_stack: list[str | None] = []
        self._text_stack: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = None
        for key, value in attrs:
            if key.lower() == "href":
                href = value
                break
        self._href_stack.append(href)
        self._text_stack.append([])

    def handle_data(self, data: str) -> None:
        if self._text_stack:
            self._text_stack[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href_stack or not self._text_stack:
            return
        href = self._href_stack.pop()
        text = "".join(self._text_stack.pop()).strip()
        if href:
            self.anchors.append((href, text))


def _to_utc(game_datetime: str | None) -> datetime | None:
    if not game_datetime:
        return None
    try:
        return datetime.fromisoformat(game_datetime.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _http_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    req_headers = {"User-Agent": "ottoneu-discord-notifier/1.0"}
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_text(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> str:
    req_headers = {"User-Agent": "ottoneu-discord-notifier/1.0"}
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _looks_like_cloudflare_challenge(html: str) -> bool:
    lower = html.lower()
    return any(marker in lower for marker in CF_CHALLENGE_MARKERS)


def _playwright_page_html(url: str, cookie_header: str, timeout_ms: int = 30000) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright; "
            "python -m playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        if cookie_header.strip():
            context.set_extra_http_headers({"Cookie": cookie_header.strip()})

        page = context.new_page()
        try:
            page.goto(
                "https://ottoneu.fangraphs.com",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Best effort: capture whatever rendered so far.
            pass

        page.wait_for_timeout(2000)
        html = page.content()
        context.close()
        browser.close()
    return html


def _debug_dump_html(debug_dir: Path, url: str, html: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "_") or "root"
    out_file = debug_dir / f"ottoneu_{slug}.html"
    out_file.write_text(html, encoding="utf-8")


def fetch_ottoneu_page_html(url: str, cookie_header: str, fetch_mode: str) -> str:
    headers: dict[str, str] = {}
    if cookie_header.strip():
        headers["Cookie"] = cookie_header.strip()

    if fetch_mode == "http":
        return _http_text(url, headers=headers)

    if fetch_mode == "playwright":
        return _playwright_page_html(url, cookie_header)

    # auto mode: try http first, then fallback to playwright on challenge pages.
    html = _http_text(url, headers=headers)
    if _looks_like_cloudflare_challenge(html):
        print("Cloudflare challenge detected; retrying with Playwright...")
        return _playwright_page_html(url, cookie_header)
    return html


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> int:
    data = json.dumps(payload).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "User-Agent": "ottoneu-discord-notifier/1.0",
    }
    if headers:
        req_headers.update(headers)
    req = Request(
        url,
        data=data,
        headers=req_headers,
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return int(getattr(resp, "status", 200))


def fetch_discord_command_messages(
    channel_id: str,
    bot_token: str,
    limit: int = 20,
) -> list[DiscordCommandMessage]:
    if not channel_id.strip() or not bot_token.strip():
        return []
    url = f"{DISCORD_API}/channels/{channel_id}/messages?limit={max(1, limit)}"
    headers = {"Authorization": f"Bot {bot_token.strip()}"}
    payload = _http_json(url, headers=headers)
    if not isinstance(payload, list):
        return []

    messages: list[DiscordCommandMessage] = []
    for message in reversed(payload):
        message_id = str(message.get("id", "")).strip()
        author = message.get("author", {})
        author_id = str(author.get("id", "")).strip()
        if not message_id or not author_id:
            continue
        if bool(author.get("bot", False)):
            continue
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        messages.append(
            DiscordCommandMessage(
                message_id=message_id,
                author_id=author_id,
                content=content,
                source="poll",
                created_at=str(message.get("timestamp", "") or ""),
            )
        )
    return messages


def _command_message_age_seconds(created_at: str) -> float | None:
    stamp = str(created_at or "").strip()
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _effective_command_poll_seconds(args: argparse.Namespace) -> int:
    configured = int(max(1, args.discord_command_poll_seconds))
    if args.discord_command_mode == "push":
        return max(2, min(5, configured))
    return max(5, configured)


def send_discord_channel_message(
    channel_id: str,
    bot_token: str,
    message: str,
    reply_to_message_id: str | None = None,
) -> None:
    if not channel_id.strip() or not bot_token.strip():
        return
    text = message.strip()
    if not text:
        return
    payload: dict[str, Any] = {"content": text[:1900]}
    if reply_to_message_id:
        payload["message_reference"] = {"message_id": reply_to_message_id}
    status = _http_post_json(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        payload,
        headers={"Authorization": f"Bot {bot_token.strip()}"},
    )
    if status not in (200, 201):
        raise RuntimeError(f"Discord channel send failed with status {status}")


class DiscordGatewayCommandListener:
    def __init__(self, *, channel_id: str, bot_token: str) -> None:
        self.channel_id = channel_id.strip()
        self.bot_token = bot_token.strip()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._messages: deque[DiscordCommandMessage] = deque()
        self._lock = threading.Lock()

    def start(self) -> bool:
        if websocket is None:
            print("Discord push commands unavailable: install websocket-client")
            return False
        if not self.channel_id or not self.bot_token:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="discord-command-gateway", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def drain_messages(self) -> list[DiscordCommandMessage]:
        with self._lock:
            if not self._messages:
                return []
            drained = list(self._messages)
            self._messages.clear()
            return drained

    def _enqueue(self, message: DiscordCommandMessage) -> None:
        with self._lock:
            self._messages.append(message)
            while len(self._messages) > 500:
                self._messages.popleft()

    def _run(self) -> None:
        sequence: int | None = None
        session_id = ""
        resume_url = ""
        while not self._stop_event.is_set():
            try:
                gateway_payload = _http_json(
                    f"{DISCORD_API}/gateway/bot",
                    headers={"Authorization": f"Bot {self.bot_token}"},
                )
                base_gateway_url = str(gateway_payload.get("url", "")).strip()
                if not base_gateway_url:
                    raise RuntimeError("Discord gateway URL missing")
                gateway_url = f"{base_gateway_url}?v=10&encoding=json"
                ws = websocket.create_connection(gateway_url, timeout=30)
            except Exception as exc:  # noqa: BLE001
                print(f"Discord gateway connect failed: {exc}")
                if self._stop_event.wait(5):
                    return
                continue

            try:
                hello_raw = ws.recv()
                hello = json.loads(hello_raw)
                heartbeat_interval_ms = float(hello.get("d", {}).get("heartbeat_interval", 45000))
                heartbeat_interval = max(5.0, heartbeat_interval_ms / 1000.0)

                if session_id and resume_url:
                    ws.send(
                        json.dumps(
                            {
                                "op": 6,
                                "d": {
                                    "token": self.bot_token,
                                    "session_id": session_id,
                                    "seq": sequence,
                                },
                            }
                        )
                    )
                else:
                    ws.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": self.bot_token,
                                    "intents": (
                                        DISCORD_GATEWAY_INTENT_GUILD_MESSAGES
                                        | DISCORD_GATEWAY_INTENT_MESSAGE_CONTENT
                                    ),
                                    "properties": {
                                        "os": "linux",
                                        "browser": "ottoneu-notifier",
                                        "device": "ottoneu-notifier",
                                    },
                                },
                            }
                        )
                    )

                next_heartbeat = time.time() + heartbeat_interval
                while not self._stop_event.is_set():
                    now = time.time()
                    timeout = max(0.5, min(2.0, next_heartbeat - now))
                    ws.settimeout(timeout)
                    payload_raw = None
                    try:
                        payload_raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        pass

                    now = time.time()
                    if now >= next_heartbeat:
                        ws.send(json.dumps({"op": 1, "d": sequence}))
                        next_heartbeat = now + heartbeat_interval

                    if payload_raw is None:
                        continue

                    payload = json.loads(payload_raw)
                    op = int(payload.get("op", -1))
                    seq = payload.get("s")
                    if isinstance(seq, int):
                        sequence = seq

                    if op == 7:
                        break
                    if op == 9:
                        session_id = ""
                        resume_url = ""
                        sequence = None
                        break
                    if op != 0:
                        continue

                    event = str(payload.get("t", ""))
                    data = payload.get("d", {})
                    if event == "READY":
                        session_id = str(data.get("session_id", ""))
                        resume_url = str(data.get("resume_gateway_url", "")).strip() or base_gateway_url
                        continue
                    if event != "MESSAGE_CREATE":
                        continue

                    if str(data.get("channel_id", "")).strip() != self.channel_id:
                        continue
                    author = data.get("author", {})
                    if bool(author.get("bot", False)):
                        continue
                    message_id = str(data.get("id", "")).strip()
                    author_id = str(author.get("id", "")).strip()
                    content = str(data.get("content", "")).strip()
                    if not message_id or not author_id or not content:
                        continue
                    self._enqueue(
                        DiscordCommandMessage(
                            message_id=message_id,
                            author_id=author_id,
                            content=content,
                            source="push",
                            created_at=str(data.get("timestamp", "") or ""),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                if not self._stop_event.is_set():
                    print(f"Discord gateway error: {exc}")
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass

            if self._stop_event.wait(2):
                return


def load_watchlist(path: Path) -> dict[int, WatchPlayer]:
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found: {path}. Create it from ottoneu_lineup_watchlist.csv."
        )

    players: dict[int, WatchPlayer] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"player_name", "mlbam_id"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                "Watchlist CSV must include headers: player_name,mlbam_id[,role]"
            )

        for row in reader:
            raw_id = str(row.get("mlbam_id", "")).strip()
            if not raw_id:
                continue
            try:
                player_id = int(raw_id)
            except ValueError:
                continue

            name = str(row.get("player_name", "")).strip() or f"Player {player_id}"
            role = str(row.get("role", "both")).strip().lower() or "both"
            if role not in {"hitter", "pitcher", "both"}:
                role = "both"

            players[player_id] = WatchPlayer(player_id=player_id, name=name, role=role)

    if not players:
        raise ValueError(f"No valid rows found in watchlist: {path}")
    return players


def load_nicknames(path: Path = NICKNAMES_PATH) -> dict[int, str]:
    """Load player nicknames from CSV. Returns {player_id: nickname}."""
    if not path.exists():
        return {}
    nicknames: dict[int, str] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    player_id = int(row.get("player_id", "").strip())
                    nickname = str(row.get("nickname", "")).strip()
                    if nickname:
                        nicknames[player_id] = nickname
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return nicknames


def save_nicknames(nicknames: dict[int, str], path: Path = NICKNAMES_PATH) -> None:
    """Save player nicknames to CSV."""
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["player_id", "nickname"])
            writer.writeheader()
            for player_id, nickname in sorted(nicknames.items()):
                writer.writerow({"player_id": player_id, "nickname": nickname})
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to save nicknames: {exc}")


def load_bridge_id_map(path: Path) -> dict[int, int]:
    """Parse master_bridge.csv Fantasy column (playercard?id=X) -> MLBAM ID."""
    if not path.exists():
        raise FileNotFoundError(f"Bridge CSV not found: {path}")
    mapping: dict[int, int] = {}
    id_re = re.compile(r"playercard\?id=(\d+)", re.IGNORECASE)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fantasy_html = str(row.get("Fantasy", ""))
            mlbam_raw = str(row.get("MLBAMID", "")).strip()
            m = id_re.search(fantasy_html)
            if not m or not mlbam_raw:
                continue
            try:
                mapping[int(m.group(1))] = int(mlbam_raw)
            except ValueError:
                continue
    if not mapping:
        raise ValueError(f"No valid playercard?id -> MLBAMID rows found in {path}")
    return mapping


def parse_ottoneu_player_ids(page_html: str) -> set[int]:
    """Extract Ottoneu player IDs from /players/{id} links in game HTML."""
    return {
        int(m.group(1))
        for m in re.finditer(r'/players/(\d+)', page_html)
    }


def load_leaderboard_name_map(path: Path) -> dict[str, int]:
    if not path.exists():
        raise FileNotFoundError(f"Leaderboard CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = {
            str(name).replace("\ufeff", "").strip() for name in (reader.fieldnames or [])
        }
        required = {"Name", "MLBAMID"}
        if not required.issubset(fieldnames):
            raise ValueError(
                "Leaderboard CSV must include Name and MLBAMID columns"
            )

        mapping: dict[str, int] = {}
        for row in reader:
            name = str(row.get("Name", "")).strip()
            raw_id = str(row.get("MLBAMID", "")).strip()
            if not name or not raw_id:
                continue
            try:
                mapping[name.lower()] = int(raw_id)
            except ValueError:
                continue

    if not mapping:
        raise ValueError(f"No valid Name/MLBAMID rows found in {path}")
    return mapping


def parse_ottoneu_player_names(
    page_html: str,
    leaderboard_name_map: dict[str, int] | None = None,
) -> set[str]:
    collector = _AnchorCollector()
    collector.feed(page_html)

    names: set[str] = set()
    for href, raw_text in collector.anchors:
        href_lower = href.lower()
        if "/playercard" not in href_lower and "/players/" not in href_lower:
            continue

        name = unescape(raw_text).strip()
        if len(name) < 3 or len(name) > 40:
            continue
        if not re.match(r"^[A-Za-z .'-]+$", name):
            continue
        if " " not in name:
            continue
        names.add(name)
    if names:
        return names

    # Fallback: if we have a known name dictionary from FanGraphs exports,
    # match those names against the page HTML when explicit player links are absent.
    if leaderboard_name_map:
        lower_html = page_html.lower()
        for known_name in leaderboard_name_map:
            if known_name in lower_html:
                names.add(known_name.title())

    return names


def resolve_name_to_mlbam_id(
    name: str,
    leaderboard_name_map: dict[str, int] | None = None,
) -> int | None:
    if leaderboard_name_map:
        mapped = leaderboard_name_map.get(name.lower())
        if mapped is not None:
            return mapped

    cached = _NAME_RESOLVE_CACHE.get(name)
    if name in _NAME_RESOLVE_CACHE:
        return cached

    url = PEOPLE_SEARCH_TEMPLATE.format(name=quote_plus(name))
    try:
        payload = _http_json(url)
    except (HTTPError, URLError, TimeoutError):
        _NAME_RESOLVE_CACHE[name] = None
        return None

    people = payload.get("people", [])
    if not people:
        _NAME_RESOLVE_CACHE[name] = None
        return None

    lower_name = name.lower()
    exact = [
        p
        for p in people
        if str(p.get("fullName", "")).strip().lower() == lower_name
        and str(p.get("primaryPosition", {}).get("abbreviation", "")) != ""
    ]
    pick = exact[0] if exact else people[0]

    pid = pick.get("id")
    resolved = pid if isinstance(pid, int) else None
    _NAME_RESOLVE_CACHE[name] = resolved
    return resolved


_MLBAM_NAME_CACHE: dict[int, str | None] = {}


def _mlbam_to_name(mlbam_id: int) -> str | None:
    if mlbam_id in _MLBAM_NAME_CACHE:
        return _MLBAM_NAME_CACHE[mlbam_id]
    url = PEOPLE_TEMPLATE.format(player_id=mlbam_id)
    try:
        payload = _http_json(url)
    except (HTTPError, URLError, TimeoutError):
        _MLBAM_NAME_CACHE[mlbam_id] = None
        return None
    people = payload.get("people", [])
    name = str(people[0].get("fullName", "")).strip() if people else None
    _MLBAM_NAME_CACHE[mlbam_id] = name or None
    return _MLBAM_NAME_CACHE[mlbam_id]


def load_watchlist_from_html_files(
    html_paths: list[Path],
    bridge_id_map: dict[int, int],
    role: str,
) -> dict[int, WatchPlayer]:
    """Build watchlist by parsing local saved Ottoneu game HTML files.

    Extracts /players/{id} links, resolves to MLBAM via bridge_id_map,
    then looks up the player's name from the MLB Stats API.
    """
    out: dict[int, WatchPlayer] = {}
    for path in html_paths:
        if not path.exists():
            print(f"Warning: HTML file not found: {path}")
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        ottoneu_ids = parse_ottoneu_player_ids(html)
        resolved = 0
        for ott_id in ottoneu_ids:
            mlbam = bridge_id_map.get(ott_id)
            if mlbam is None:
                continue
            if mlbam in out:
                resolved += 1
                continue
            # Fetch player name from MLB Stats API
            name = _mlbam_to_name(mlbam)
            out[mlbam] = WatchPlayer(player_id=mlbam, name=name or str(mlbam), role=role)
            resolved += 1
        print(f"{path.name}: found {len(ottoneu_ids)} player IDs, resolved {resolved} via bridge CSV")
    return out


def _extract_html_from_webarchive(webarchive_data: bytes) -> str | None:
    """Attempt to extract HTML content from a macOS Safari .webarchive file (plist format)."""
    import plistlib
    try:
        plist = plistlib.loads(webarchive_data)
        main_resource = plist.get("WebMainResource", {})
        html_data = main_resource.get("WebResourceData")
        if isinstance(html_data, bytes):
            return html_data.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _split_discord_channel_ids(channel_id: str) -> list[str]:
    return [cid.strip() for cid in channel_id.split(",") if cid.strip()]


def fetch_discord_html_attachments(
    channel_id: str,
    bot_token: str,
    limit: int = 10,
) -> list[tuple[str, str]]:
    if not channel_id.strip():
        raise ValueError("Discord HTML channel ID is required")
    if not bot_token.strip():
        raise ValueError("Discord bot token is required")

    url = f"{DISCORD_API}/channels/{channel_id}/messages?limit={limit}"
    headers = {"Authorization": f"Bot {bot_token.strip()}"}
    payload = _http_json(url, headers=headers)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Discord API response while reading channel messages")

    html_files: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for message in payload:
        attachments = message.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            filename = str(attachment.get("filename", "")).strip()
            url = str(attachment.get("url", "")).strip()
            content_type = str(attachment.get("content_type", "")).strip().lower()
            if not filename or not url or url in seen_urls:
                continue
            lower_name = filename.lower()
            is_supported_page = lower_name.endswith((".html", ".htm", ".mht", ".mhtml", ".webarchive"))
            if not is_supported_page and "text/html" not in content_type and "message/rfc822" not in content_type:
                continue
            
            # Download file content (text for HTML, binary for webarchive)
            if lower_name.endswith(".webarchive"):
                try:
                    req = Request(url, headers={"User-Agent": "ottoneu-discord-notifier/1.0"})
                    with urlopen(req, timeout=20) as resp:
                        webarchive_bytes = resp.read()
                    extracted_html = _extract_html_from_webarchive(webarchive_bytes)
                    if extracted_html:
                        html_files.append((filename, extracted_html))
                except Exception as exc:
                    print(f"Warning: Failed to extract HTML from {filename}: {exc}")
            else:
                html_files.append((filename, _http_text(url)))
            seen_urls.add(url)

    return html_files


def load_watchlist_from_discord_html_attachments(
    channel_id: str,
    bot_token: str,
    bridge_id_map: dict[int, int],
    role: str,
    limit: int = 10,
) -> dict[int, WatchPlayer]:
    # Support comma-separated list of channel IDs so multiple rosters can be merged
    channel_ids = _split_discord_channel_ids(channel_id)
    html_files: list[tuple[str, str]] = []
    for cid in channel_ids:
        files = fetch_discord_html_attachments(
            channel_id=cid,
            bot_token=bot_token,
            limit=limit,
        )
        html_files.extend(files)
    if not html_files:
        print("No Discord HTML attachments found in the configured channel(s)")
        return {}

    out: dict[int, WatchPlayer] = {}
    with tempfile.TemporaryDirectory(prefix="ottoneu_discord_html_") as tmpdir:
        html_paths: list[Path] = []
        for idx, (filename, html) in enumerate(html_files):
            stem = Path(filename).stem or "discord_upload"
            suffix = Path(filename).suffix or ".html"
            safe_name = f"{idx:02d}_{stem}{suffix}"
            path = Path(tmpdir) / safe_name
            path.write_text(html, encoding="utf-8")
            html_paths.append(path)
        out.update(load_watchlist_from_html_files(html_paths, bridge_id_map, role))

    print(f"Loaded {len(html_files)} HTML attachment(s) from Discord")
    return out


def load_watchlist_from_ottoneu_games(
    game_urls: list[str],
    role: str,
    cookie_header: str,
    fetch_mode: str,
    debug_dir: Path | None = None,
    leaderboard_name_map: dict[str, int] | None = None,
) -> dict[int, WatchPlayer]:
    if not game_urls:
        return {}

    out: dict[int, WatchPlayer] = {}
    unresolved: list[str] = []
    for url in game_urls:
        try:
            html = fetch_ottoneu_page_html(
                url=url,
                cookie_header=cookie_header,
                fetch_mode=fetch_mode,
            )
        except HTTPError as exc:
            if exc.code == 403:
                raise RuntimeError(
                    "Ottoneu returned 403. Provide --ottoneu-cookie-header or "
                    "set OTTONEU_COOKIE_HEADER from a logged-in browser session."
                ) from exc
            raise

        if debug_dir is not None:
            _debug_dump_html(debug_dir, url, html)

        if _looks_like_cloudflare_challenge(html):
            raise RuntimeError(
                "Ottoneu fetch returned a Cloudflare challenge page (not matchup HTML). "
                "Try --ottoneu-fetch-mode playwright and verify browser/session access."
            )

        names = parse_ottoneu_player_names(html, leaderboard_name_map=leaderboard_name_map)
        for name in names:
            mlbam = resolve_name_to_mlbam_id(name, leaderboard_name_map)
            if mlbam is None:
                unresolved.append(name)
                continue
            out[mlbam] = WatchPlayer(player_id=mlbam, name=name, role=role)

        print(f"{url}: extracted {len(names)} player names")

    if unresolved:
        sample = ", ".join(sorted(set(unresolved))[:10])
        print(f"Warning: could not resolve MLBAM ID for some Ottoneu names: {sample}")
    return out


def load_state(path: Path) -> dict[str, Any]:
    default_state = {
        "state_date": "",
        "sent_keys": [],
        "announced_lineups": [],
        "final_summaries": [],
        "seen_clips": [],
        "final_game_times": {},
        "discord_watchlist_cache": {},
        "handled_command_ids": [],
    }
    if not path.exists():
        return default_state.copy()
    try:
        with path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default_state.copy()

    for key, value in default_state.items():
        state.setdefault(key, value)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _serialize_watchlist(watchlist: dict[int, WatchPlayer]) -> list[dict[str, Any]]:
    return [
        {"player_id": wp.player_id, "name": wp.name, "role": wp.role}
        for wp in sorted(watchlist.values(), key=lambda item: item.player_id)
    ]


def _deserialize_watchlist(items: list[dict[str, Any]] | None) -> dict[int, WatchPlayer]:
    out: dict[int, WatchPlayer] = {}
    for item in items or []:
        try:
            player_id = int(item.get("player_id"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("name", "")).strip() or f"Player {player_id}"
        role = str(item.get("role", "both")).strip().lower() or "both"
        if role not in {"hitter", "pitcher", "both"}:
            role = "both"
        out[player_id] = WatchPlayer(player_id=player_id, name=name, role=role)
    return out


def _roll_state_for_date(state: dict[str, Any], target_date: date) -> None:
    state_date = str(state.get("state_date", "")).strip()
    target_key = target_date.isoformat()
    if state_date == target_key:
        return
    state["state_date"] = target_key
    state["sent_keys"] = []
    state["announced_lineups"] = []
    state["final_summaries"] = []
    state["seen_clips"] = []
    state["final_game_times"] = {}


def _discord_attachment_supported(filename: str, content_type: str) -> bool:
    lower_name = filename.lower()
    return lower_name.endswith((".html", ".htm", ".mht", ".mhtml", ".webarchive")) or (
        "text/html" in content_type or "message/rfc822" in content_type
    )


def fetch_latest_discord_html_attachment(
    channel_id: str,
    bot_token: str,
    limit: int = 10,
) -> DiscordHtmlAttachment | None:
    if not channel_id.strip():
        raise ValueError("Discord HTML channel ID is required")
    if not bot_token.strip():
        raise ValueError("Discord bot token is required")

    url = f"{DISCORD_API}/channels/{channel_id}/messages?limit={limit}"
    headers = {"Authorization": f"Bot {bot_token.strip()}"}
    payload = _http_json(url, headers=headers)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Discord API response while reading channel messages")

    for message in payload:
        attachments = message.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        created_at = str(message.get("timestamp", "")).strip()
        message_id = str(message.get("id", "")).strip()
        for attachment in attachments:
            filename = str(attachment.get("filename", "")).strip()
            attachment_url = str(attachment.get("url", "")).strip()
            content_type = str(attachment.get("content_type", "")).strip().lower()
            if not filename or not attachment_url:
                continue
            if not _discord_attachment_supported(filename, content_type):
                continue
            return DiscordHtmlAttachment(
                message_id=message_id,
                created_at=created_at,
                filename=filename,
                url=attachment_url,
            )
    return None


def load_watchlist_from_discord_attachment(
    attachment: DiscordHtmlAttachment,
    bridge_id_map: dict[int, int],
    role: str,
) -> dict[int, WatchPlayer]:
    # Handle both regular HTML and macOS webarchive formats
    if attachment.filename.lower().endswith(".webarchive"):
        try:
            req = Request(attachment.url, headers={"User-Agent": "ottoneu-discord-notifier/1.0"})
            with urlopen(req, timeout=20) as resp:
                webarchive_bytes = resp.read()
            html = _extract_html_from_webarchive(webarchive_bytes)
            if not html:
                raise ValueError("Could not extract HTML from webarchive")
        except Exception as exc:
            print(f"Error extracting HTML from {attachment.filename}: {exc}")
            return {}
    else:
        html = _http_text(attachment.url)
    
    with tempfile.TemporaryDirectory(prefix="ottoneu_discord_html_") as tmpdir:
        safe_name = Path(attachment.filename).name or "discord_upload.html"
        path = Path(tmpdir) / safe_name
        path.write_text(html, encoding="utf-8")
        out = load_watchlist_from_html_files([path], bridge_id_map, role)
    print(
        f"Loaded Discord upload {attachment.filename} ({attachment.created_at or 'unknown time'})"
    )
    return out


def refresh_discord_watchlist_cache(
    state: dict[str, Any],
    channel_id: str,
    bot_token: str,
    bridge_id_map: dict[int, int],
    role: str,
    limit: int = 10,
) -> tuple[dict[int, WatchPlayer], bool]:
    cache = state.get("discord_watchlist_cache", {})
    cached_watchlist = _deserialize_watchlist(cache.get("players"))

    new_watchlist = load_watchlist_from_discord_html_attachments(
        channel_id=channel_id,
        bot_token=bot_token,
        bridge_id_map=bridge_id_map,
        role=role,
        limit=limit,
    )
    if not new_watchlist:
        if cached_watchlist:
            print("No Discord HTML uploads found; keeping cached watchlist")
        return cached_watchlist, False

    new_serialized = _serialize_watchlist(new_watchlist)
    cached_serialized = cache.get("players") or []
    if cached_watchlist and new_serialized == cached_serialized:
        return cached_watchlist, False

    state["discord_watchlist_cache"] = {
        "players": new_serialized,
        "source": "discord_html_attachments",
        "attachment_limit": limit,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    return new_watchlist, True


def _watchlist_preview(watchlist: dict[int, WatchPlayer], limit: int = 20) -> str:
    names = sorted({wp.name for wp in watchlist.values()})
    if not names:
        return "(empty)"
    preview = names[: max(1, limit)]
    suffix = "" if len(names) <= len(preview) else f" ... (+{len(names) - len(preview)} more)"
    return ", ".join(preview) + suffix


def _build_tracked_games_summary(team_ids: set[int], target_date: date) -> str:
    try:
        all_games = game_schedule(target_date)
    except Exception as exc:  # noqa: BLE001
        return f"Could not fetch schedule: {exc}"
    games = tracked_games_for_watchlist(all_games, team_ids)
    if not games:
        return "No tracked games scheduled today."

    live = 0
    pregame = 0
    final = 0
    postponed = 0
    labels: list[str] = []
    for game in games:
        bucket = game_status_bucket(game)
        if bucket == "live":
            live += 1
        elif bucket == "pregame":
            pregame += 1
        elif bucket == "final":
            final += 1
        elif bucket == "postponed":
            postponed += 1
        if len(labels) < 5:
            labels.append(f"{game_label_from_schedule(game)} [{bucket}]")
    return (
        f"Tracked games: {len(games)} (live {live}, pregame {pregame}, final {final}, postponed {postponed})\n"
        f"Examples: {'; '.join(labels)}"
    )


def _ascii_name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    clean = re.sub(r"[^a-z0-9]+", " ", stripped.lower())
    return " ".join(clean.split())


def _find_player_boxscore_entry(
    feed: dict[str, Any],
    player_id: int,
) -> dict[str, Any] | None:
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    key = f"ID{player_id}"
    for side in ("home", "away"):
        players = teams.get(side, {}).get("players", {})
        if key in players:
            return players[key]
    return None


def _player_play_lines(
    feed: dict[str, Any],
    player_id: int,
    *,
    include_all_batter_events: bool = False,
    include_statcast: bool = False,
) -> tuple[list[str], list[str], float, float]:
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    batter_lines: list[str] = []
    pitcher_lines: list[str] = []
    batter_points = 0.0
    pitcher_points = 0.0

    for play in plays:
        about = play.get("about", {})
        matchup = play.get("matchup", {})
        result = play.get("result", {})
        inning = int(about.get("inning", 0) or 0)
        half = str(about.get("halfInning", "")).strip().lower()
        inning_label = f"{inning}{'T' if half == 'top' else 'B'}"
        desc = str(result.get("description") or result.get("event") or "").strip()
        if not desc:
            continue
        event_type = str(result.get("eventType", "")).lower().strip()
        scoring = bool(about.get("isScoringPlay", False))

        batter_id = matchup.get("batter", {}).get("id")
        if batter_id == player_id:
            pts = _event_sabr_points(event_type, is_batter=True)
            batter_points += pts
            statcast_suffix = _statcast_suffix(play) if include_statcast else ""
            if include_all_batter_events:
                batter_lines.append(f"{inning_label}: {desc}{statcast_suffix} ({pts:+.1f} pts)")
            elif scoring or event_type in NOTABLE_BATTER_EVENTS:
                batter_lines.append(f"{inning_label}: {desc}{statcast_suffix} ({pts:+.1f} pts)")

        pitcher_id = matchup.get("pitcher", {}).get("id")
        if pitcher_id == player_id:
            pts = _event_sabr_points(event_type, is_batter=False)
            pitcher_points += pts
            if scoring or event_type in NOTABLE_PITCHER_EVENTS:
                pitcher_lines.append(f"{inning_label}: {desc} ({pts:+.1f} pts)")

    return batter_lines, pitcher_lines, batter_points, pitcher_points


def _ip_to_innings(ip_value: Any) -> float:
    text = str(ip_value or "0.0").strip()
    if not text:
        return 0.0
    try:
        whole_str, frac_str = text.split(".", 1)
        whole = int(whole_str)
        frac = int(frac_str[:1] or "0")
    except (ValueError, AttributeError):
        try:
            return float(text)
        except ValueError:
            return 0.0
    if frac == 1:
        return whole + (1.0 / 3.0)
    if frac == 2:
        return whole + (2.0 / 3.0)
    return float(whole)


def _batter_sabr_points_from_box(batting: dict[str, Any]) -> float:
    ab = int(batting.get("atBats", 0) or 0)
    hits = int(batting.get("hits", 0) or 0)
    doubles = int(batting.get("doubles", 0) or 0)
    triples = int(batting.get("triples", 0) or 0)
    hrs = int(batting.get("homeRuns", 0) or 0)
    bb = int(batting.get("baseOnBalls", 0) or 0)
    hbp = int(batting.get("hitByPitch", 0) or 0)
    sb = int(batting.get("stolenBases", 0) or 0)
    cs = int(batting.get("caughtStealing", 0) or 0)
    return (
        ab * SABR_HITTING_POINTS["at_bat"]
        + hits * SABR_HITTING_POINTS["hit"]
        + doubles * SABR_HITTING_POINTS["double"]
        + triples * SABR_HITTING_POINTS["triple"]
        + hrs * SABR_HITTING_POINTS["home_run"]
        + bb * SABR_HITTING_POINTS["walk"]
        + hbp * SABR_HITTING_POINTS["hbp"]
        + sb * SABR_HITTING_POINTS["stolen_base"]
        + cs * SABR_HITTING_POINTS["caught_stealing"]
    )


def _pitcher_sabr_points_from_box(pitching: dict[str, Any]) -> float:
    return sum(_pitcher_sabr_component_points(pitching).values())


def _pitcher_sabr_component_points(pitching: dict[str, Any]) -> dict[str, float]:
    innings = _ip_to_innings(pitching.get("inningsPitched", "0.0"))
    so = int(pitching.get("strikeOuts", 0) or 0)
    bb = int(pitching.get("baseOnBalls", pitching.get("walks", 0)) or 0)
    hbp = int(pitching.get("hitBatsmen", pitching.get("hitByPitch", 0)) or 0)
    hr = int(pitching.get("homeRuns", 0) or 0)
    saves = int(pitching.get("saves", 0) or 0)
    holds = int(pitching.get("holds", 0) or 0)
    return {
        "IP": innings * SABR_PITCHING_POINTS["ip"],
        "K": so * SABR_PITCHING_POINTS["strikeout"],
        "BB": bb * SABR_PITCHING_POINTS["walk"],
        "HBP": hbp * SABR_PITCHING_POINTS["hbp"],
        "HR": hr * SABR_PITCHING_POINTS["home_run"],
        "SV": saves * SABR_PITCHING_POINTS["save"],
        "HLD": holds * SABR_PITCHING_POINTS["hold"],
    }


def _pitcher_sabr_breakdown_text(pitching: dict[str, Any]) -> str:
    component_points = _pitcher_sabr_component_points(pitching)
    parts = [f"{label} {points:+.1f}" for label, points in component_points.items() if abs(points) > 1e-9]
    return ", ".join(parts) if parts else "No SABR scoring events"


def _build_player_day_report(target_date: date, requested_name: str, nicknames: dict[int, str] | None = None) -> str:
    target_key = _ascii_name_key(requested_name)
    if not target_key:
        return "Usage: !ot player Full Name"

    try:
        games = game_schedule(target_date)
    except Exception as exc:  # noqa: BLE001
        return f"Could not fetch schedule: {exc}"

    exact_matches: list[tuple[int, str, int, dict[str, Any]]] = []
    fuzzy_matches: list[tuple[int, str, int, dict[str, Any]]] = []

    for game in games:
        game_pk = int(game.get("gamePk", 0) or 0)
        if game_pk <= 0:
            continue
        try:
            feed = _http_json(LIVE_FEED_TEMPLATE.format(game_pk=game_pk))
        except Exception:
            continue
        players = _safe_player_map(feed)
        for pid, obj in players.items():
            display_name = _display_name(obj, str(pid), player_id=pid, nicknames=nicknames)
            original_name = (
                obj.get("name")
                or obj.get("fullName")
                or obj.get("lastFirstName")
                or str(pid)
            )
            candidate_keys = {
                _ascii_name_key(display_name),
                _ascii_name_key(original_name),
            }
            candidate_keys.discard("")
            if not candidate_keys:
                continue
            row = (pid, display_name, game_pk, feed)
            if target_key in candidate_keys:
                exact_matches.append(row)
            elif any(target_key in name_key or name_key in target_key for name_key in candidate_keys):
                fuzzy_matches.append(row)

    matches = exact_matches or fuzzy_matches
    if not matches:
        return f"No player match found for '{requested_name}' on {target_date.isoformat()}."

    if len(matches) > 1:
        sample = []
        for _, name, _, feed in matches[:5]:
            sample.append(f"{name} ({_game_label(feed)})")
        return (
            f"Multiple matches for '{requested_name}'. Please be more specific.\n"
            f"Examples: {'; '.join(sample)}"
        )

    player_id, player_name, _game_pk, feed = matches[0]
    game_text = _game_label(feed)
    status = _status_label(feed)
    box_entry = _find_player_boxscore_entry(feed, player_id) or {}
    batting = box_entry.get("stats", {}).get("batting", {})
    pitching = box_entry.get("stats", {}).get("pitching", {})

    batter_lines, pitcher_lines, batter_points, pitcher_points = _player_play_lines(
        feed,
        player_id,
        include_all_batter_events=True,
        include_statcast=True,
    )
    lines: list[str] = [f"{player_name} - {game_text} ({status})"]

    if batting:
        ab = int(batting.get("atBats", 0) or 0)
        h = int(batting.get("hits", 0) or 0)
        r = int(batting.get("runs", 0) or 0)
        rbi = int(batting.get("rbi", 0) or 0)
        hr = int(batting.get("homeRuns", 0) or 0)
        sb = int(batting.get("stolenBases", 0) or 0)
        batter_total_points = _batter_sabr_points_from_box(batting)
        lines.append(
            f"Batting: {h}-{ab}, R {r}, RBI {rbi}, HR {hr}, SB {sb} | total pts {batter_total_points:+.1f}"
        )
    if pitching:
        ip = str(pitching.get("inningsPitched", "0.0"))
        so = int(pitching.get("strikeOuts", 0) or 0)
        bb = int(pitching.get("baseOnBalls", 0) or 0)
        er = int(pitching.get("earnedRuns", 0) or 0)
        h_allowed = int(pitching.get("hits", 0) or 0)
        whip = pitching.get("whip", "-")
        pitcher_total_points = _pitcher_sabr_points_from_box(pitching)
        lines.append(
            f"Pitching: {ip} IP, H {h_allowed}, K {so}, BB {bb}, ER {er}, WHIP {whip} | total pts {pitcher_total_points:+.1f}"
        )

    detail_lines: list[str] = []
    if batter_lines:
        detail_lines.extend(["Batter plays:"] + batter_lines[:8])
    if pitcher_lines:
        detail_lines.extend(["Pitcher plays:"] + pitcher_lines[:8])

    if detail_lines:
        if batting:
            detail_lines.insert(0, f"Batter notable-play pts: {batter_points:+.1f}")
        if pitching:
            insert_at = 1 if batting else 0
            detail_lines.insert(insert_at, f"Pitcher notable-play pts: {pitcher_points:+.1f}")
        lines.extend(detail_lines)
    else:
        lines.append("No notable play events logged yet for this player today.")

    return "\n".join(lines)


def process_discord_text_commands(
    *,
    state: dict[str, Any],
    args: argparse.Namespace,
    target_date: date,
    bridge_id_map: dict[int, int],
    base_watchlist: dict[int, WatchPlayer],
    discord_watchlist: dict[int, WatchPlayer],
    watchlist: dict[int, WatchPlayer],
    team_ids: set[int],
    nicknames: dict[int, str] | None = None,
    messages: list[DiscordCommandMessage] | None = None,
) -> tuple[dict[int, WatchPlayer], dict[int, WatchPlayer], set[int], dict[int, str]]:
    if nicknames is None:
        nicknames = {}
    if not args.discord_command_channel_id.strip() or not args.discord_bot_token.strip():
        return discord_watchlist, watchlist, team_ids, nicknames

    handled_ids = list(state.get("handled_command_ids", []))
    handled_set = set(handled_ids)
    prefix = args.discord_command_prefix.strip() or "!ot"
    lower_prefix = prefix.lower()

    command_messages = messages
    if command_messages is None:
        try:
            command_messages = fetch_discord_command_messages(
                channel_id=args.discord_command_channel_id,
                bot_token=args.discord_bot_token,
                limit=max(1, args.discord_command_limit),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Discord command poll failed: {exc}")
            return discord_watchlist, watchlist, team_ids, nicknames

    for message in command_messages:
        if message.message_id in handled_set:
            continue
        content = message.content.strip()
        if not content.lower().startswith(lower_prefix):
            continue

        command_text = content[len(prefix):].strip()
        command = command_text.lower() if command_text else "help"
        command_source = str(message.source or "poll").strip().lower() or "poll"
        age_seconds = _command_message_age_seconds(message.created_at)
        response = ""

        if command in {"help", "?"}:
            response = (
                f"{prefix} help - show commands\n"
                f"{prefix} status - notifier status\n"
                f"{prefix} watchlist - preview tracked players\n"
                f"{prefix} refresh - force Discord upload refresh\n"
                f"{prefix} games - tracked games summary\n"
                f"{prefix} player Full Name - player day report\n"
                f"{prefix} setnickname Full Name - New Nickname - set player nickname\n"
                f"{prefix} removenickname Full Name - remove player nickname\n"
                f"{prefix} clearnicknames - remove all nicknames"
            )
        elif command == "status":
            html_channel_count = len(_split_discord_channel_ids(args.discord_html_channel_id))
            response = (
                f"Watching {len(watchlist)} players across {len(team_ids)} teams for {target_date.isoformat()}.\n"
                f"Intervals: live {args.poll_seconds}s, pregame {args.pregame_seconds}s, idle {args.idle_seconds}s.\n"
                f"Watchlist refresh {args.watchlist_refresh_seconds}s, post-final poll {args.postfinal_poll_seconds}s.\n"
                f"Discord HTML channels configured: {html_channel_count}."
            )
        elif command == "watchlist":
            response = f"Watchlist ({len(watchlist)}): {_watchlist_preview(watchlist)}"
        elif command == "games":
            response = _build_tracked_games_summary(team_ids, target_date)
        elif command == "refresh":
            if not args.discord_html_channel_id.strip():
                response = "No Discord HTML upload channel configured on this notifier."
            else:
                try:
                    # Force-clear cache so we always get a fresh load on manual refresh
                    state["discord_watchlist_cache"] = {}
                    refreshed_discord_watchlist, changed = refresh_discord_watchlist_cache(
                        state=state,
                        channel_id=args.discord_html_channel_id,
                        bot_token=args.discord_bot_token,
                        bridge_id_map=bridge_id_map,
                        role=args.ottoneu_role,
                        limit=max(1, args.discord_html_limit),
                    )
                except Exception as exc:  # noqa: BLE001
                    response = f"Refresh failed: {exc}"
                else:
                    html_channel_count = len(_split_discord_channel_ids(args.discord_html_channel_id))
                    if changed:
                        discord_watchlist = refreshed_discord_watchlist
                        watchlist = dict(base_watchlist)
                        watchlist.update(discord_watchlist)
                        team_ids = watched_team_ids(watchlist)
                        state["announced_lineups"] = []
                        response = (
                            f"Loaded {len(discord_watchlist)} Discord players from {html_channel_count} HTML channel(s). "
                            f"Now tracking {len(watchlist)} total players."
                        )
                    else:
                        response = (
                            f"No HTML uploads found across {html_channel_count} HTML channel(s). Tracking {len(discord_watchlist)} Discord players "
                            f"and {len(watchlist)} total players."
                        )
        elif command.startswith("player "):
            requested_name = command_text.split(" ", 1)[1].strip()
            response = _build_player_day_report(target_date, requested_name, nicknames=nicknames)
        elif command.startswith("setnickname "):
            remainder = command_text.split(" ", 1)[1].strip()
            if " - " not in remainder:
                response = f"Usage: {prefix} setnickname Full Name - New Nickname"
            else:
                player_name, nickname = remainder.split(" - ", 1)
                player_name = player_name.strip()
                nickname = nickname.strip()
                if not player_name or not nickname:
                    response = f"Usage: {prefix} setnickname Full Name - New Nickname"
                else:
                    found_ids = []
                    try:
                        for pid, wp in watchlist.items():
                            if _ascii_name_key(wp.name) == _ascii_name_key(player_name):
                                found_ids.append(pid)
                    except Exception:
                        pass
                    if len(found_ids) == 1:
                        pid = found_ids[0]
                        nicknames[pid] = nickname
                        save_nicknames(nicknames)
                        response = f"Nickname set: {watchlist[pid].name} -> {nickname}"
                    elif len(found_ids) > 1:
                        response = f"Multiple players match '{player_name}'. Be more specific."
                    else:
                        response = f"Player '{player_name}' not found in watchlist."
        elif command.startswith("removenickname "):
            remainder = command_text.split(" ", 1)[1].strip()
            if not remainder:
                response = f"Usage: {prefix} removenickname Full Name"
            else:
                found_ids = []
                try:
                    for pid, wp in watchlist.items():
                        if _ascii_name_key(wp.name) == _ascii_name_key(remainder):
                            found_ids.append(pid)
                except Exception:
                    pass
                if len(found_ids) == 1:
                    pid = found_ids[0]
                    if pid in nicknames:
                        del nicknames[pid]
                        save_nicknames(nicknames)
                        response = f"Nickname removed for {watchlist[pid].name}"
                    else:
                        response = f"{watchlist[pid].name} has no nickname set."
                elif len(found_ids) > 1:
                    response = f"Multiple players match '{remainder}'. Be more specific."
                else:
                    response = f"Player '{remainder}' not found in watchlist."
        elif command == "clearnicknames":
            if nicknames:
                count = len(nicknames)
                nicknames.clear()
                save_nicknames(nicknames)
                response = f"Cleared {count} nicknames."
            else:
                response = "No nicknames to clear."
        else:
            response = f"Unknown command '{command_text}'. Try: {prefix} help"

        if getattr(args, "discord_command_debug", False):
            age_text = f"{age_seconds:.1f}s" if age_seconds is not None else "n/a"
            print(
                f"Command '{command_text or 'help'}' via {command_source} "
                f"(message {message.message_id}, age {age_text})"
            )
        if getattr(args, "discord_command_debug_reply", False):
            age_text = f"{age_seconds:.1f}s" if age_seconds is not None else "n/a"
            response = f"{response}\nDebug: source={command_source}, age={age_text}"

        try:
            send_discord_channel_message(
                channel_id=args.discord_command_channel_id,
                bot_token=args.discord_bot_token,
                message=response,
                reply_to_message_id=message.message_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Discord command response failed: {exc}")

        handled_ids.append(message.message_id)
        handled_set.add(message.message_id)

    if len(handled_ids) > 500:
        handled_ids = handled_ids[-500:]
    state["handled_command_ids"] = handled_ids
    return discord_watchlist, watchlist, team_ids, nicknames


def game_schedule(target_date: date) -> list[dict[str, Any]]:
    url = (
        f"{STATS_API}/schedule?sportId=1&date={target_date.isoformat()}"
        "&hydrate=team,linescore"
    )
    payload = _http_json(url)
    games: list[dict[str, Any]] = []
    for day in payload.get("dates", []):
        games.extend(day.get("games", []))
    return games


def watched_team_ids(watchlist: dict[int, WatchPlayer]) -> set[int]:
    teams: set[int] = set()
    for pid in watchlist:
        try:
            payload = _http_json(PEOPLE_TEMPLATE.format(player_id=pid))
        except (HTTPError, URLError, TimeoutError):
            continue
        people = payload.get("people", [])
        if not people:
            continue
        team_id = people[0].get("currentTeam", {}).get("id")
        if isinstance(team_id, int):
            teams.add(team_id)
    return teams


def tracked_games_for_watchlist(
    schedule_games: list[dict[str, Any]],
    watch_team_ids: set[int],
) -> list[dict[str, Any]]:
    tracked: list[dict[str, Any]] = []
    for game in schedule_games:
        teams = game.get("teams", {})
        away_id = teams.get("away", {}).get("team", {}).get("id")
        home_id = teams.get("home", {}).get("team", {}).get("id")
        if away_id in watch_team_ids or home_id in watch_team_ids:
            tracked.append(game)
    return tracked


def game_status_bucket(game: dict[str, Any]) -> str:
    abstract_state = str(game.get("status", {}).get("abstractGameState", "")).strip()
    detailed = str(game.get("status", {}).get("detailedState", "")).strip()
    lower_detailed = detailed.lower()

    if abstract_state in {"Live", "Manager Challenge"}:
        return "live"
    if abstract_state == "Final":
        return "final"
    if abstract_state in {"Preview", "Pre-Game", "Warmup"}:
        return "pregame"
    if "postponed" in lower_detailed or "suspended" in lower_detailed:
        return "postponed"
    return "other"


def game_label_from_schedule(game: dict[str, Any]) -> str:
    teams = game.get("teams", {})
    away = teams.get("away", {}).get("team", {}).get("abbreviation", "AWAY")
    home = teams.get("home", {}).get("team", {}).get("abbreviation", "HOME")
    return f"{away} @ {home}"


def _safe_player_map(feed: dict[str, Any]) -> dict[int, dict[str, Any]]:
    players = feed.get("gameData", {}).get("players", {})
    out: dict[int, dict[str, Any]] = {}
    for key, player_obj in players.items():
        if key.startswith("ID"):
            try:
                out[int(key[2:])] = player_obj
            except ValueError:
                continue
    return out


def _display_name(player_obj: dict[str, Any], fallback: str, player_id: int | None = None, nicknames: dict[int, str] | None = None) -> str:
    if nicknames and player_id in nicknames:
        return nicknames[player_id]
    return (
        player_obj.get("name")
        or player_obj.get("fullName")
        or player_obj.get("lastFirstName")
        or fallback
    )


def _notification_name(
    player_obj: dict[str, Any],
    fallback: str,
    *,
    player_id: int | None = None,
    nicknames: dict[int, str] | None = None,
) -> str:
    original_name = (
        player_obj.get("name")
        or player_obj.get("fullName")
        or player_obj.get("lastFirstName")
        or fallback
    )
    if not nicknames or player_id not in nicknames:
        return original_name
    nickname = nicknames[player_id].strip()
    if not nickname:
        return original_name
    if _ascii_name_key(nickname) == _ascii_name_key(original_name):
        return original_name
    return f"{nickname} ({original_name})"


def _game_label(feed: dict[str, Any]) -> str:
    gd = feed.get("gameData", {})
    teams = gd.get("teams", {})
    away = teams.get("away", {}).get("abbreviation", "AWAY")
    home = teams.get("home", {}).get("abbreviation", "HOME")
    return f"{away} @ {home}"


def _team_logo_url(team_abbr: str) -> str:
    """Return a Discord-friendly MLB team logo URL (PNG)."""
    return f"https://a.espncdn.com/i/teamlogos/mlb/500/{team_abbr.lower()}.png"


def _player_headshot_url(player_id: int) -> str:
    return (
        "https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"w_213,q_auto:best/v1/people/{player_id}/headshot/67/current"
    )


def _discord_embed(
    title: str,
    description: str,
    away_abbr: str,
    home_abbr: str,
    status: str,
    color: int,
    player_id: int | None = None,
) -> dict[str, Any]:
    away_logo = _team_logo_url(away_abbr)
    home_logo = _team_logo_url(home_abbr)
    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "author": {
            "name": f"{away_abbr} @ {home_abbr}",
            "icon_url": away_logo,
        },
        "footer": {
            "text": status,
            "icon_url": home_logo,
        },
    }
    if player_id is not None:
        embed["thumbnail"] = {"url": _player_headshot_url(player_id)}
    return embed


def _status_label(feed: dict[str, Any]) -> str:
    st = feed.get("gameData", {}).get("status", {})
    return st.get("detailedState") or st.get("abstractGameState") or "Unknown"


def send_webhook(
    webhook_url: str,
    message: str,
    dry_run: bool = False,
    embeds: list[dict[str, Any]] | None = None,
) -> None:
    if dry_run:
        out = f"[DRY RUN] {message}"
        if embeds:
            out += " [embed]"
        try:
            print(out)
        except UnicodeEncodeError:
            # Windows cp1252 consoles can fail on emoji; replace unsupported chars.
            print(out.encode("cp1252", errors="replace").decode("cp1252"))
        return

    if embeds:
        payload: dict[str, Any] = {"embeds": embeds}
    else:
        payload = {"content": message}
    status = _http_post_json(webhook_url, payload)
    if status not in (200, 204):
        raise RuntimeError(f"Discord webhook returned status {status}")


def _lineup_presence_ids(feed: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    boxscore = feed.get("liveData", {}).get("boxscore", {})
    teams = boxscore.get("teams", {})
    for side in ("home", "away"):
        team_players = teams.get(side, {}).get("players", {})
        for key in team_players:
            if key.startswith("ID"):
                try:
                    ids.add(int(key[2:]))
                except ValueError:
                    continue
    return ids


def _starting_pitcher_ids(feed: dict[str, Any]) -> set[int]:
    """Return all pitcher IDs (starters and relief) who appeared in the game."""
    ids: set[int] = set()
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        pitcher_ids = teams.get(side, {}).get("pitchers", [])
        if isinstance(pitcher_ids, list):
            for pid in pitcher_ids:
                if isinstance(pid, int):
                    ids.add(pid)
    return ids


def _build_final_summary(
    game_text: str,
    feed: dict[str, Any],
    watchlist: dict[int, WatchPlayer],
    nicknames: dict[int, str] | None = None,
) -> list[tuple[str, int]]:
    """Build final game summary including SABR points for watched players."""
    lines: list[tuple[str, int]] = []
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})

    for side in ("home", "away"):
        players = teams.get(side, {}).get("players", {})
        for key, pobj in players.items():
            if not key.startswith("ID"):
                continue
            try:
                pid = int(key[2:])
            except ValueError:
                continue
            if pid not in watchlist:
                continue

            wp = watchlist[pid]
            name = _notification_name(pobj.get("person", {}), wp.name, player_id=pid, nicknames=nicknames)
            batting = pobj.get("stats", {}).get("batting", {})
            pitching = pobj.get("stats", {}).get("pitching", {})

            batting_points = 0.0
            if batting:
                ab = int(batting.get("atBats", 0) or 0)
                hits = int(batting.get("hits", 0) or 0)
                doubles = int(batting.get("doubles", 0) or 0)
                triples = int(batting.get("triples", 0) or 0)
                hrs = int(batting.get("homeRuns", 0) or 0)
                bb = int(batting.get("baseOnBalls", 0) or 0)
                hbp = int(batting.get("hitByPitch", 0) or 0)
                sb = int(batting.get("stolenBases", 0) or 0)
                cs = int(batting.get("caughtStealing", 0) or 0)

                batting_points = (
                    ab * SABR_HITTING_POINTS["at_bat"]
                    + hits * SABR_HITTING_POINTS["hit"]
                    + doubles * SABR_HITTING_POINTS["double"]
                    + triples * SABR_HITTING_POINTS["triple"]
                    + hrs * SABR_HITTING_POINTS["home_run"]
                    + bb * SABR_HITTING_POINTS["walk"]
                    + hbp * SABR_HITTING_POINTS["hbp"]
                    + sb * SABR_HITTING_POINTS["stolen_base"]
                    + cs * SABR_HITTING_POINTS["caught_stealing"]
                )

            pitching_points = 0.0
            if pitching:
                pitching_points = _pitcher_sabr_points_from_box(pitching)

            if batting:
                ab = int(batting.get("atBats", 0) or 0)
                hits = int(batting.get("hits", 0) or 0)
                hr = int(batting.get("homeRuns", 0) or 0)
                rbi = int(batting.get("rbi", 0) or 0)
                runs = int(batting.get("runs", 0) or 0)
                if ab > 0 or hits > 0 or hr > 0 or rbi > 0 or runs > 0:
                    lines.append((
                        f"{EVENT_EMOJIS['final']} Final {game_text}: {name} batting {hits}-{ab}, HR {hr}, RBI {rbi}, R {runs} | **{batting_points:.1f} pts**",
                        pid,
                    ))
                    batter_play_lines, _, _, _ = _player_play_lines(
                        feed,
                        pid,
                        include_all_batter_events=True,
                        include_statcast=True,
                    )
                    if batter_play_lines:
                        lines.append((
                            f"{EVENT_EMOJIS['final']} Final {game_text}: {name} AB log:\n" + "\n".join(batter_play_lines[:20]),
                            pid,
                        ))

            if pitching:
                ip = str(pitching.get("inningsPitched", "0.0"))
                so = int(pitching.get("strikeOuts", 0) or 0)
                er = int(pitching.get("earnedRuns", 0) or 0)
                whip = pitching.get("whip", "-")
                if ip != "0.0" or so > 0 or er > 0:
                    breakdown = _pitcher_sabr_breakdown_text(pitching)
                    lines.append((
                        f"{EVENT_EMOJIS['final']} Final {game_text}: {name} pitching {ip} IP, {so} K, {er} ER, WHIP {whip} | {breakdown} | **{pitching_points:.1f} pts**",
                        pid,
                    ))

    return lines


def _fetch_highlights(game_pk: int) -> list[dict]:
    """Fetch highlight clips for a game. Returns list of {clip_id, headline, mp4_url, player_ids}."""
    try:
        content = _http_json(GAME_CONTENT_TEMPLATE.format(game_pk=game_pk))
    except Exception:
        return []
    items = content.get("highlights", {}).get("highlights", {}).get("items", [])
    clips = []
    for item in items:
        clip_id = item.get("mediaPlaybackId") or item.get("id", "")
        if not clip_id:
            continue
        headline = item.get("headline", "")
        mp4_url = next(
            (pb.get("url", "") for pb in item.get("playbacks", []) if pb.get("name") == "mp4Avc"),
            "",
        )
        if not mp4_url:
            continue
        player_ids = [
            int(kw["value"])
            for kw in item.get("keywordsAll", [])
            if kw.get("type") == "player_id" and str(kw.get("value", "")).isdigit()
        ]
        lower_headline = headline.lower()
        # Exclude utility/analysis packages so we keep mostly in-game play clips.
        if any(
            marker in lower_headline
            for marker in (
                "breaking down",
                "distance behind",
                "through bat tracking data",
                "outing against",
                "probable pitchers",
                "starting lineups",
                "bench availability",
                "bullpen availability",
                "fielding alignment",
                "condensed game",
                "recap",
            )
        ):
            continue
        if any(
            marker in lower_headline
            for marker in (
                "diving play",
                "spectacular play",
                "great play",
                "defensive play",
                "sliding stop",
                "leaping catch",
                "great catch",
                "running catch",
                "barehanded",
                "web gem",
                "robs",
                "robbery",
                "snags",
                "throws out",
                "double play",
            )
        ):
            continue
        if not any(
            marker in lower_headline
            for marker in (
                "single",
                "double",
                "triple",
                "home run",
                "homer",
                "grand slam",
                "walk-off",
                "walks",
                "hit by pitch",
                "rbi",
                "drives in",
                "plates",
                "scores",
                "stolen base",
                "steals",
                "collects",
            )
        ):
            continue
        headline_key = _ascii_name_key(headline)
        url_path = Path(urlparse(mp4_url).path)
        fallback_key = _ascii_name_key(url_path.stem)
        dedupe_key = f"{game_pk}:{headline_key or fallback_key}:{','.join(str(pid) for pid in sorted(set(player_ids)))}"
        clips.append({
            "clip_id": clip_id,
            "dedupe_key": dedupe_key,
            "headline": headline,
            "mp4_url": mp4_url,
            "player_ids": player_ids,
        })
    return clips


def _event_sabr_points(event_type: str, is_batter: bool = True) -> float:
    """Get SABR points for a play event type.

    Hitting: every at-bat costs AB (-1.0). Hits add H (+5.6) on top.
    Extra-base hits stack their bonus: 2B adds +2.9, 3B adds +5.7, HR adds +9.4.
    So a HR = -1.0 + 5.6 + 9.4 = +14.0 pts total for that plate appearance.
    Walks and HBP are not ABs, so no -1.0 penalty.
    """
    if is_batter:
        AB = SABR_HITTING_POINTS["at_bat"]   # -1.0
        H  = SABR_HITTING_POINTS["hit"]       # +5.6
        lookup = {
            # AB + H
            "single":          AB + H,
            # AB + H + extra-base bonus
            "double":          AB + H + SABR_HITTING_POINTS["double"],
            "triple":          AB + H + SABR_HITTING_POINTS["triple"],
            "home_run":        AB + H + SABR_HITTING_POINTS["home_run"],
            # Not an AB, no H
            "walk":            SABR_HITTING_POINTS["walk"],
            "hit_by_pitch":    SABR_HITTING_POINTS["hbp"],
            # Baserunning events (no AB) — match e.g. stolen_base_2b, caught_stealing_3b
            "stolen_base":     SABR_HITTING_POINTS["stolen_base"],
            "caught_stealing": SABR_HITTING_POINTS["caught_stealing"],
        }
        # Exact match first, then prefix match for suffixed variants
        if event_type in lookup:
            return lookup[event_type]
        for prefix, val in (
            ("stolen_base", SABR_HITTING_POINTS["stolen_base"]),
            ("caught_stealing", SABR_HITTING_POINTS["caught_stealing"]),
        ):
            if event_type.startswith(prefix):
                return val
        return 0.0
    else:
        lookup = {
            "strikeout": SABR_PITCHING_POINTS["strikeout"],
            "home_run":  SABR_PITCHING_POINTS["home_run"],
            "walk":      SABR_PITCHING_POINTS["walk"],
        }
        return lookup.get(event_type, 0.0)


def _statcast_suffix(play: dict[str, Any]) -> str:
    """Build optional Statcast metrics text (EV/LA) when available."""
    events = play.get("playEvents", [])
    if not isinstance(events, list):
        return ""

    hit_data = None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        candidate = event.get("hitData")
        if isinstance(candidate, dict):
            hit_data = candidate
            break
    if not isinstance(hit_data, dict):
        return ""

    parts: list[str] = []
    launch_speed = hit_data.get("launchSpeed")
    if isinstance(launch_speed, (int, float)):
        parts.append(f"EV {launch_speed:.1f} mph")

    launch_angle = hit_data.get("launchAngle")
    if isinstance(launch_angle, (int, float)):
        parts.append(f"LA {launch_angle:.1f} deg")

    distance = hit_data.get("totalDistance")
    if isinstance(distance, (int, float)):
        parts.append(f"Dist {distance:.0f} ft")

    if not parts:
        return ""
    return f" ({', '.join(parts)})"


def _innings_pitched_to_outs(ip_value: str) -> int:
    """Convert innings pitched string (e.g., 1.2) to recorded outs (e.g., 5)."""
    try:
        whole_str, frac_str = str(ip_value).split(".", 1)
        whole = int(whole_str)
        frac = int(frac_str)
    except (ValueError, TypeError):
        return 0
    if frac not in {0, 1, 2}:
        return 0
    return whole * 3 + frac


def process_game(
    game_pk: int,
    watchlist: dict[int, WatchPlayer],
    webhook_url: str,
    state: dict[str, Any],
    dry_run: bool,
    nicknames: dict[int, str] | None = None,
) -> bool:
    feed = _http_json(LIVE_FEED_TEMPLATE.format(game_pk=game_pk))
    game_text = _game_label(feed)
    status = _status_label(feed)
    teams = feed.get("gameData", {}).get("teams", {})
    away_abbr = teams.get("away", {}).get("abbreviation", "AWAY")
    home_abbr = teams.get("home", {}).get("abbreviation", "HOME")
    sent_keys: set[str] = set(state.get("sent_keys", []))
    announced_lineups: set[str] = set(state.get("announced_lineups", []))
    final_summaries: set[str] = set(state.get("final_summaries", []))
    
    # Track running SABR points for players throughout the game
    running_points: dict[int, float] = {}

    player_map = _safe_player_map(feed)
    present_ids = _lineup_presence_ids(feed)
    watched_here = [pid for pid in watchlist if pid in present_ids]

    if watched_here:
        lineup_key = f"{game_pk}:lineup"
        if lineup_key not in announced_lineups:
            announced_lineups.add(lineup_key)

    plays_data = feed.get("liveData", {}).get("plays", {})
    plays = plays_data.get("allPlays", [])
    current_pitcher_id = None
    current_play = plays_data.get("currentPlay", {})
    if isinstance(current_play, dict):
        maybe_pitcher = current_play.get("matchup", {}).get("pitcher", {}).get("id")
        if isinstance(maybe_pitcher, int):
            current_pitcher_id = maybe_pitcher
    for play in plays:
        result = play.get("result", {})
        about = play.get("about", {})
        matchup = play.get("matchup", {})
        at_bat_index = about.get("atBatIndex")
        event_type = str(result.get("eventType", "")).lower().strip()
        is_scoring = bool(about.get("isScoringPlay", False))
        rbi = int(result.get("rbi", 0) or 0)

        batter_id = matchup.get("batter", {}).get("id")
        pitcher_id = matchup.get("pitcher", {}).get("id")

        if isinstance(batter_id, int) and batter_id in watchlist:
            wp = watchlist[batter_id]
            batter_name = _notification_name(player_map.get(batter_id, {}), wp.name, player_id=batter_id, nicknames=nicknames)
            is_notable_batter = (
                event_type in NOTABLE_BATTER_EVENTS
                or event_type.startswith("stolen_base")
                or event_type.startswith("caught_stealing")
            )
            if wp.role in {"hitter", "both"} and (is_notable_batter or is_scoring):
                key = f"{game_pk}:{at_bat_index}:batter:{batter_id}:{event_type}:{rbi}"
                if key not in sent_keys:
                    # Calculate SABR points for this event
                    event_points = _event_sabr_points(event_type, is_batter=True)
                    statcast_suffix = _statcast_suffix(play)
                    event_name = str(result.get("event") or "").strip() or event_type.replace("_", " ").title()
                    if batter_id not in running_points:
                        running_points[batter_id] = 0.0
                    running_points[batter_id] += event_points
                    
                    msg = result.get("description") or f"{batter_name}: {event_type}"
                    sign = "+" if event_points >= 0 else ""
                    points_str = f" | **{sign}{event_points:.1f} pts** (total: {running_points[batter_id]:.1f} pts)" if event_points != 0 else ""
                    event_emoji = EVENT_EMOJIS.get(event_type, "⚾")
                    send_webhook(
                        webhook_url,
                        f"{event_emoji} {game_text}: {batter_name} - {msg}{statcast_suffix}{points_str}",
                        dry_run=dry_run,
                        embeds=[
                            _discord_embed(
                                title=f"{batter_name} Alert - {event_name}",
                                description=f"{event_emoji} {msg}{statcast_suffix}{points_str}",
                                away_abbr=away_abbr,
                                home_abbr=home_abbr,
                                status=status,
                                color=0x1ABC9C,
                                player_id=batter_id,
                            )
                        ],
                    )
                    sent_keys.add(key)

        if isinstance(pitcher_id, int) and pitcher_id in watchlist:
            wp = watchlist[pitcher_id]
            pitcher_name = _notification_name(player_map.get(pitcher_id, {}), wp.name, player_id=pitcher_id, nicknames=nicknames)
            if wp.role in {"pitcher", "both"} and event_type in NOTABLE_PITCHER_EVENTS:
                key = f"{game_pk}:{at_bat_index}:pitcher:{pitcher_id}:{event_type}:{rbi}"
                if key not in sent_keys:
                    # Calculate SABR points for this pitcher event
                    event_points = _event_sabr_points(event_type, is_batter=False)
                    pitcher_total_points = event_points
                    pitcher_box = _find_player_boxscore_entry(feed, pitcher_id) or {}
                    pitcher_stats = pitcher_box.get("stats", {}).get("pitching", {}) if pitcher_box else {}
                    if pitcher_stats:
                        pitcher_total_points = _pitcher_sabr_points_from_box(pitcher_stats)
                    
                    msg = result.get("description") or f"{pitcher_name}: {event_type}"
                    sign = "+" if event_points >= 0 else ""
                    points_str = (
                        f" | **{sign}{event_points:.1f} pts** (total: {pitcher_total_points:.1f} pts)"
                        if event_points != 0
                        else f" | total: {pitcher_total_points:.1f} pts"
                    )
                    event_emoji = EVENT_EMOJIS.get(event_type, "⚾")
                    send_webhook(
                        webhook_url,
                        f"{event_emoji} {game_text}: {pitcher_name} involved - {msg}{points_str}",
                        dry_run=dry_run,
                        embeds=[
                            _discord_embed(
                                title=f"{event_emoji} {pitcher_name} Pitching Alert",
                                description=f"{event_emoji} {msg}{points_str}",
                                away_abbr=away_abbr,
                                home_abbr=home_abbr,
                                status=status,
                                color=0xE67E22,
                                player_id=pitcher_id,
                            )
                        ],
                    )
                    sent_keys.add(key)

    # Pitcher outing-complete alerts (helps surface reliever appearances).
    bs_teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    is_final = status.lower() == "final"
    for side in ("home", "away"):
        for key, pobj in bs_teams.get(side, {}).get("players", {}).items():
            if not key.startswith("ID"):
                continue
            try:
                pid = int(key[2:])
            except ValueError:
                continue
            if pid not in watchlist:
                continue
            wp = watchlist[pid]
            if wp.role not in {"pitcher", "both"}:
                continue

            pitching = pobj.get("stats", {}).get("pitching", {})
            if not pitching:
                continue
            outs_recorded = _innings_pitched_to_outs(str(pitching.get("inningsPitched", "0.0")))
            if outs_recorded <= 0:
                continue

            outing_key = f"{game_pk}:pitcher_outing:{pid}"
            if outing_key in sent_keys:
                continue
            # Only announce outing complete if: (1) game is final, OR (2) a different pitcher is currently on mound
            # But NOT if the pitcher is still actively pitching (current) and game isn't over
            if not is_final and current_pitcher_id == pid:
                continue

            so = int(pitching.get("strikeOuts", 0) or 0)
            bb = int(pitching.get("baseOnBalls", 0) or 0)
            er = int(pitching.get("earnedRuns", 0) or 0)
            whip = pitching.get("whip", "-")
            ip = str(pitching.get("inningsPitched", "0.0"))

            outing_points = _pitcher_sabr_points_from_box(pitching)
            breakdown = _pitcher_sabr_breakdown_text(pitching)
            sign = "+" if outing_points >= 0 else ""
            api_obj = player_map.get(pid, {})
            name = _notification_name(api_obj, wp.name, player_id=pid, nicknames=nicknames)
            summary = (
                f"{name} finished: {ip} IP, {so} K, {bb} BB, {er} ER, WHIP {whip}"
                f" | {breakdown} | **{sign}{outing_points:.1f} pts**"
            )
            send_webhook(
                webhook_url,
                f"{EVENT_EMOJIS['final']} {game_text}: {summary}",
                dry_run=dry_run,
                embeds=[
                    _discord_embed(
                        title=f"{name} Outing Complete",
                        description=summary,
                        away_abbr=away_abbr,
                        home_abbr=home_abbr,
                        status=status,
                        color=0x8E44AD,
                        player_id=pid,
                    )
                ],
            )
            sent_keys.add(outing_key)

    # --- Highlight clips (fires as clips become available, even mid-game) ---
    seen_clips: set[str] = set(state.get("seen_clips", []))
    starter_pitcher_ids = _starting_pitcher_ids(feed)
    for clip in _fetch_highlights(game_pk):
        clip_id = clip["clip_id"]
        clip_key = clip.get("dedupe_key") or clip_id
        if clip_id in seen_clips or clip_key in seen_clips:
            continue

        watched_ids = [
            pid for pid in clip["player_ids"]
            if pid in watchlist and watchlist[pid].role in {"hitter", "both"}
        ]
        if not watched_ids:
            continue

        # Suppress videos when only watched starting pitchers are associated with the clip.
        non_starter_matches = [pid for pid in watched_ids if pid not in starter_pitcher_ids]
        if not non_starter_matches:
            seen_clips.add(clip_id)
            seen_clips.add(clip_key)
            continue

        send_webhook(
            webhook_url,
            f"{EVENT_EMOJIS['highlight']} {game_text}: {clip['headline']}\n{clip['mp4_url']}",
            dry_run=dry_run,
        )
        seen_clips.add(clip_id)
        seen_clips.add(clip_key)

    if is_final:
        final_key = f"{game_pk}:final"
        if final_key not in final_summaries:
            for line, pid in _build_final_summary(game_text, feed, watchlist, nicknames=nicknames):
                send_webhook(
                    webhook_url,
                    line,
                    dry_run=dry_run,
                    embeds=[
                        _discord_embed(
                            title="Final Statline",
                            description=line,
                            away_abbr=away_abbr,
                            home_abbr=home_abbr,
                            status=status,
                            color=0x95A5A6,
                            player_id=pid,
                        )
                    ],
                )
            final_summaries.add(final_key)

    state["sent_keys"] = sorted(sent_keys)
    state["announced_lineups"] = sorted(announced_lineups)
    state["final_summaries"] = sorted(final_summaries)
    state["seen_clips"] = sorted(seen_clips)

    live_states = {"Live", "In Progress", "Manager Challenge", "Warmup"}
    return status in live_states


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ottoneu lineup Discord notifier")
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST,
        help="CSV file with player_name, mlbam_id, role",
    )
    parser.add_argument(
        "--leaderboard-csv",
        type=Path,
        default=Path(os.getenv("FANGRAPHS_LEADERBOARD_CSV", "")).expanduser()
        if os.getenv("FANGRAPHS_LEADERBOARD_CSV", "").strip()
        else None,
        help="Optional FanGraphs leaderboard CSV with Name and MLBAMID columns",
    )
    parser.add_argument(
        "--bridge-csv",
        type=Path,
        default=Path(os.getenv("OTTONEU_BRIDGE_CSV", "")).expanduser()
        if os.getenv("OTTONEU_BRIDGE_CSV", "").strip()
        else None,
        help="master_bridge.csv with Fantasy (playercard?id=X) and MLBAMID columns",
    )
    parser.add_argument(
        "--ottoneu-html-file",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="Path to a locally saved Ottoneu game HTML file (repeatable). "
             "Use with --bridge-csv to build watchlist without scraping.",
    )
    parser.add_argument(
        "--discord-html-channel-id",
        type=str,
        default=os.getenv("DISCORD_HTML_CHANNEL_ID", ""),
        help="Discord channel ID(s) to read uploaded Ottoneu HTML attachments from (comma-separated for multiple channels)",
    )
    parser.add_argument(
        "--discord-bot-token",
        type=str,
        default=os.getenv("DISCORD_BOT_TOKEN", ""),
        help="Discord bot token used to read HTML attachments from a channel",
    )
    parser.add_argument(
        "--discord-html-limit",
        type=int,
        default=int(os.getenv("DISCORD_HTML_LIMIT", "50")),
        help="How many recent Discord messages to scan for HTML attachments",
    )
    parser.add_argument(
        "--discord-command-channel-id",
        type=str,
        default=os.getenv("DISCORD_COMMAND_CHANNEL_ID", ""),
        help="Discord channel ID used for text commands (e.g. !ot status)",
    )
    parser.add_argument(
        "--discord-command-prefix",
        type=str,
        default=os.getenv("DISCORD_COMMAND_PREFIX", "!ot"),
        help="Command prefix for Discord text commands",
    )
    parser.add_argument(
        "--discord-command-limit",
        type=int,
        default=int(os.getenv("DISCORD_COMMAND_LIMIT", "20")),
        help="How many recent command-channel messages to scan each command poll",
    )
    parser.add_argument(
        "--discord-command-poll-seconds",
        type=int,
        default=int(os.getenv("DISCORD_COMMAND_POLL_SECONDS", "20")),
        help="How often to poll for command messages in the command channel",
    )
    parser.add_argument(
        "--discord-command-mode",
        type=str,
        default=os.getenv("DISCORD_COMMAND_MODE", "push"),
        choices=["push", "poll"],
        help="Command intake mode: push via Discord Gateway, or poll via REST",
    )
    parser.add_argument(
        "--discord-command-debug",
        action="store_true",
        default=os.getenv("DISCORD_COMMAND_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"},
        help="Log command intake diagnostics (source and message age)",
    )
    parser.add_argument(
        "--discord-command-debug-reply",
        action="store_true",
        default=os.getenv("DISCORD_COMMAND_DEBUG_REPLY", "0").strip().lower() in {"1", "true", "yes", "on"},
        help="Append command intake debug details to command replies",
    )
    parser.add_argument(
        "--no-csv-watchlist",
        action="store_true",
        help="Ignore local watchlist CSV and use Ottoneu game URLs only",
    )
    parser.add_argument(
        "--ottoneu-game-url",
        action="append",
        default=[],
        help="Ottoneu matchup URL to scrape players from (repeatable)",
    )
    parser.add_argument(
        "--ottoneu-role",
        type=str,
        default="both",
        choices=["hitter", "pitcher", "both"],
        help="Role assigned to players scraped from Ottoneu game URLs",
    )
    parser.add_argument(
        "--ottoneu-cookie-header",
        type=str,
        default=os.getenv("OTTONEU_COOKIE_HEADER", ""),
        help="Cookie header from logged-in Ottoneu browser session",
    )
    parser.add_argument(
        "--ottoneu-fetch-mode",
        type=str,
        default=os.getenv("OTTONEU_FETCH_MODE", "auto"),
        choices=["auto", "http", "playwright"],
        help="How to fetch Ottoneu pages (auto/http/playwright)",
    )
    parser.add_argument(
        "--ottoneu-debug-dir",
        type=Path,
        default=Path(os.getenv("OTTONEU_DEBUG_DIR", "")).expanduser()
        if os.getenv("OTTONEU_DEBUG_DIR", "").strip()
        else None,
        help="Optional folder to dump fetched Ottoneu HTML pages for debugging",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=os.getenv("NOTIFIER_DATE", date.today().isoformat()),
        help="Target date in YYYY-MM-DD",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("NOTIFIER_POLL_SECONDS", "90")),
        help="Polling interval while games are live",
    )
    parser.add_argument(
        "--pregame-seconds",
        type=int,
        default=int(os.getenv("NOTIFIER_PREGAME_SECONDS", "300")),
        help="Polling interval before tracked games go live",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=int(os.getenv("NOTIFIER_IDLE_SECONDS", str(DEFAULT_IDLE_SECONDS))),
        help="Polling interval when no tracked games are near/live",
    )
    parser.add_argument(
        "--watchlist-refresh-seconds",
        type=int,
        default=int(
            os.getenv(
                "NOTIFIER_WATCHLIST_REFRESH_SECONDS",
                str(DEFAULT_WATCHLIST_REFRESH_SECONDS),
            )
        ),
        help="How often to check Discord for a newer lineup upload",
    )
    parser.add_argument(
        "--postfinal-poll-seconds",
        type=int,
        default=int(
            os.getenv(
                "NOTIFIER_POSTFINAL_POLL_SECONDS",
                str(DEFAULT_POSTFINAL_POLL_SECONDS),
            )
        ),
        help="Polling interval while checking for late highlight clips after final",
    )
    parser.add_argument(
        "--postfinal-highlight-seconds",
        type=int,
        default=int(
            os.getenv(
                "NOTIFIER_POSTFINAL_HIGHLIGHT_SECONDS",
                str(DEFAULT_POSTFINAL_HIGHLIGHT_SECONDS),
            )
        ),
        help="How long to keep checking for late highlight clips after a tracked game goes final",
    )
    parser.add_argument(
        "--replay-final-games",
        action="store_true",
        help="Process tracked games even if they are already final, useful for testing past dates",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=STATE_PATH,
        help="State JSON path used for dedupe tracking",
    )
    parser.add_argument(
        "--game-pk",
        action="append",
        type=int,
        default=[],
        help="Only process the specified MLB gamePk value(s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one pass and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print notifications instead of posting to Discord",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url and not args.dry_run:
        raise ValueError("Set DISCORD_WEBHOOK_URL env var or run with --dry-run")

    date_is_fixed = "--date" in sys.argv or bool(os.getenv("NOTIFIER_DATE", "").strip())
    fixed_target_date: date | None = None
    if date_is_fixed:
        try:
            fixed_target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("--date must be YYYY-MM-DD") from exc

    state = load_state(args.state_file)

    leaderboard_name_map = None
    if args.leaderboard_csv is not None:
        leaderboard_name_map = load_leaderboard_name_map(args.leaderboard_csv)

    bridge_id_map: dict[int, int] = {}
    if args.bridge_csv is not None:
        bridge_id_map = load_bridge_id_map(args.bridge_csv)

    base_watchlist: dict[int, WatchPlayer] = {}
    if not args.no_csv_watchlist and args.watchlist.exists():
        base_watchlist.update(load_watchlist(args.watchlist))

    if args.ottoneu_html_file:
        if not bridge_id_map:
            raise ValueError(
                "--ottoneu-html-file requires --bridge-csv (or OTTONEU_BRIDGE_CSV env var)"
            )
        from_html = load_watchlist_from_html_files(
            html_paths=args.ottoneu_html_file,
            bridge_id_map=bridge_id_map,
            role=args.ottoneu_role,
        )
        base_watchlist.update(from_html)

    discord_watchlist: dict[int, WatchPlayer] = {}
    if args.discord_html_channel_id:
        if not bridge_id_map:
            raise ValueError(
                "--discord-html-channel-id requires --bridge-csv (or OTTONEU_BRIDGE_CSV env var)"
            )
        discord_watchlist = _deserialize_watchlist(
            state.get("discord_watchlist_cache", {}).get("players")
        )
        if discord_watchlist:
            print(f"Loaded cached Discord watchlist with {len(discord_watchlist)} players")
        discord_watchlist, _ = refresh_discord_watchlist_cache(
            state=state,
            channel_id=args.discord_html_channel_id,
            bot_token=args.discord_bot_token,
            bridge_id_map=bridge_id_map,
            role=args.ottoneu_role,
            limit=max(1, args.discord_html_limit),
        )

    if args.ottoneu_game_url:
        scraped = load_watchlist_from_ottoneu_games(
            game_urls=args.ottoneu_game_url,
            role=args.ottoneu_role,
            cookie_header=args.ottoneu_cookie_header,
            fetch_mode=args.ottoneu_fetch_mode,
            debug_dir=args.ottoneu_debug_dir,
            leaderboard_name_map=leaderboard_name_map,
        )
        base_watchlist.update(scraped)

    watchlist = dict(base_watchlist)
    watchlist.update(discord_watchlist)

    nicknames = load_nicknames()

    if not watchlist:
        raise ValueError(
            "No players loaded. Provide a watchlist CSV, --ottoneu-html-file, --discord-html-channel-id, or --ottoneu-game-url."
        )

    team_ids = watched_team_ids(watchlist)
    if not team_ids:
        raise RuntimeError(
            "Could not resolve watched players to MLB teams. Check mlbam_id values."
        )

    initial_target_date = fixed_target_date or date.today()
    _roll_state_for_date(state, initial_target_date)
    print(f"Watching {len(watchlist)} players for {initial_target_date.isoformat()}")
    print(f"Resolved {len(team_ids)} tracked MLB teams")
    print(f"Live poll interval: {args.poll_seconds}s")
    print(f"Pregame poll interval: {args.pregame_seconds}s")
    print(f"Idle poll interval: {args.idle_seconds}s")
    print(f"Watchlist refresh interval: {args.watchlist_refresh_seconds}s")
    print(f"Post-final highlight grace: {args.postfinal_highlight_seconds}s")
    command_push_listener: DiscordGatewayCommandListener | None = None
    if args.discord_command_channel_id.strip():
        if args.discord_command_mode == "push":
            command_push_listener = DiscordGatewayCommandListener(
                channel_id=args.discord_command_channel_id,
                bot_token=args.discord_bot_token,
            )
            if command_push_listener.start():
                print(
                    f"Command channel enabled: {args.discord_command_channel_id} "
                    f"(prefix '{args.discord_command_prefix}', mode push)"
                )
            else:
                command_push_listener = None
                print(
                    f"Command channel enabled: {args.discord_command_channel_id} "
                    f"(prefix '{args.discord_command_prefix}', poll {args.discord_command_poll_seconds}s fallback)"
                )
        else:
            print(
                f"Command channel enabled: {args.discord_command_channel_id} "
                f"(prefix '{args.discord_command_prefix}', poll {args.discord_command_poll_seconds}s)"
            )
        if args.discord_command_debug:
            print(
                f"Command debug enabled (reply={args.discord_command_debug_reply}, "
                f"fallback poll {_effective_command_poll_seconds(args)}s)"
            )

    next_watchlist_refresh_at = 0.0
    next_command_poll_at = 0.0

    while True:
        target_date = fixed_target_date or date.today()
        _roll_state_for_date(state, target_date)

        now_ts = time.time()
        if args.discord_html_channel_id and now_ts >= next_watchlist_refresh_at:
            next_watchlist_refresh_at = now_ts + max(30, args.watchlist_refresh_seconds)
            try:
                refreshed_discord_watchlist, changed = refresh_discord_watchlist_cache(
                    state=state,
                    channel_id=args.discord_html_channel_id,
                    bot_token=args.discord_bot_token,
                    bridge_id_map=bridge_id_map,
                    role=args.ottoneu_role,
                    limit=max(1, args.discord_html_limit),
                )
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"Discord watchlist refresh failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"Discord watchlist refresh error: {exc}")
            else:
                if changed:
                    discord_watchlist = refreshed_discord_watchlist
                    watchlist = dict(base_watchlist)
                    watchlist.update(discord_watchlist)
                    team_ids = watched_team_ids(watchlist)
                    state["announced_lineups"] = []
                    print(
                        f"Activated new Discord upload watchlist with {len(discord_watchlist)} players"
                    )

        push_ready = bool(command_push_listener and command_push_listener.is_alive())
        if command_push_listener:
            pushed_messages = command_push_listener.drain_messages()
            if pushed_messages:
                discord_watchlist, watchlist, team_ids, nicknames = process_discord_text_commands(
                    state=state,
                    args=args,
                    target_date=target_date,
                    bridge_id_map=bridge_id_map,
                    base_watchlist=base_watchlist,
                    discord_watchlist=discord_watchlist,
                    watchlist=watchlist,
                    team_ids=team_ids,
                    nicknames=nicknames,
                    messages=pushed_messages,
                )

        should_poll_commands = (
            args.discord_command_mode == "poll"
            or not push_ready
            or (args.discord_command_mode == "push" and args.discord_command_poll_seconds > 0)
        )
        if args.discord_command_channel_id and should_poll_commands and now_ts >= next_command_poll_at:
            next_command_poll_at = now_ts + _effective_command_poll_seconds(args)
            discord_watchlist, watchlist, team_ids, nicknames = process_discord_text_commands(
                state=state,
                args=args,
                target_date=target_date,
                bridge_id_map=bridge_id_map,
                base_watchlist=base_watchlist,
                discord_watchlist=discord_watchlist,
                watchlist=watchlist,
                team_ids=team_ids,
                nicknames=nicknames,
            )

        try:
            all_games = game_schedule(target_date)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"Schedule fetch failed: {exc}")
            if args.once:
                return 1
            time.sleep(args.idle_seconds)
            continue

        if not all_games:
            print("No MLB games found for date.")
            if args.once:
                return 0
            time.sleep(args.idle_seconds)
            continue

        games = tracked_games_for_watchlist(all_games, team_ids)
        if args.game_pk:
            allowed_game_pks = set(args.game_pk)
            games = [game for game in games if int(game.get("gamePk")) in allowed_game_pks]
        if not games:
            print("No scheduled games for tracked players/teams on this date.")
            if args.once:
                return 0
            save_state(args.state_file, state)
            time.sleep(args.idle_seconds)
            continue

        live_game_pks: list[int] = []
        pregame_game_pks: list[int] = []
        final_grace_game_pks: list[int] = []
        done_count = 0
        final_game_times: dict[str, str] = state.setdefault("final_game_times", {})
        now_utc = datetime.now(timezone.utc)
        for game in games:
            game_pk = int(game.get("gamePk"))
            bucket = game_status_bucket(game)
            if bucket == "live":
                live_game_pks.append(game_pk)
                final_game_times.pop(str(game_pk), None)
            elif bucket == "pregame":
                pregame_game_pks.append(game_pk)
                final_game_times.pop(str(game_pk), None)
            elif bucket == "final":
                stamp = final_game_times.get(str(game_pk))
                if not stamp:
                    stamp = now_utc.isoformat()
                    final_game_times[str(game_pk)] = stamp
                try:
                    first_seen = datetime.fromisoformat(stamp)
                except ValueError:
                    first_seen = now_utc
                    final_game_times[str(game_pk)] = first_seen.isoformat()
                age_seconds = max(0.0, (now_utc - first_seen).total_seconds())
                if age_seconds <= max(0, args.postfinal_highlight_seconds):
                    final_grace_game_pks.append(game_pk)
                else:
                    done_count += 1
            elif bucket == "postponed":
                done_count += 1
                final_game_times.pop(str(game_pk), None)
            else:
                final_game_times.pop(str(game_pk), None)

        if done_count == len(games) and not live_game_pks and not pregame_game_pks and not final_grace_game_pks and not args.replay_final_games:
            print("All tracked games are complete for the date. Waiting for the next schedule window.")
            if args.once:
                return 0
            save_state(args.state_file, state)
            time.sleep(args.idle_seconds)
            continue

        any_live = False
        if live_game_pks:
            pks_to_process = list(live_game_pks) + [pk for pk in final_grace_game_pks if pk not in live_game_pks]
        elif pregame_game_pks:
            pks_to_process = list(pregame_game_pks) + [pk for pk in final_grace_game_pks if pk not in pregame_game_pks]
        elif final_grace_game_pks:
            pks_to_process = list(final_grace_game_pks)
        elif args.replay_final_games:
            pks_to_process = [int(game.get("gamePk")) for game in games]
        else:
            pks_to_process = []

        if live_game_pks:
            print(f"Scanning {len(live_game_pks)} live tracked games...")
        elif pregame_game_pks:
            next_msg = []
            for game in games:
                if int(game.get("gamePk")) not in pregame_game_pks:
                    continue
                label = game_label_from_schedule(game)
                game_dt = _to_utc(game.get("gameDate"))
                if game_dt is not None:
                    next_msg.append(f"{label} at {game_dt.strftime('%H:%M UTC')}")
                else:
                    next_msg.append(label)
            if next_msg:
                print("Tracked games not live yet: " + "; ".join(next_msg[:5]))
        elif final_grace_game_pks:
            print(
                f"Checking {len(final_grace_game_pks)} final tracked game(s) for late highlight clips..."
            )
        elif args.replay_final_games:
            print(f"Replaying {len(pks_to_process)} completed tracked games...")

        for game_pk in pks_to_process:
            try:
                game_is_live = process_game(
                    game_pk=game_pk,
                    watchlist=watchlist,
                    webhook_url=webhook_url,
                    state=state,
                    dry_run=args.dry_run,
                    nicknames=nicknames,
                )
                any_live = any_live or game_is_live
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"Game {game_pk} fetch failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"Game {game_pk} processing error: {exc}")

        save_state(args.state_file, state)

        if args.once:
            return 0

        # Poll quickly only during live action; otherwise back off until the next useful window.
        if any_live or live_game_pks:
            sleep_seconds = args.poll_seconds
        elif final_grace_game_pks:
            sleep_seconds = args.postfinal_poll_seconds
        elif pregame_game_pks:
            sleep_seconds = args.pregame_seconds
        else:
            sleep_seconds = args.idle_seconds

        # Keep command responsiveness high during sleep windows.
        if args.discord_command_channel_id.strip() and args.discord_bot_token.strip():
            remaining = max(0, int(sleep_seconds))
            while remaining > 0:
                step = min(1, remaining)
                time.sleep(step)
                remaining -= step
                now_ts = time.time()
                push_ready = bool(command_push_listener and command_push_listener.is_alive())
                if command_push_listener:
                    pushed_messages = command_push_listener.drain_messages()
                    if pushed_messages:
                        discord_watchlist, watchlist, team_ids, nicknames = process_discord_text_commands(
                            state=state,
                            args=args,
                            target_date=target_date,
                            bridge_id_map=bridge_id_map,
                            base_watchlist=base_watchlist,
                            nicknames=nicknames,
                            discord_watchlist=discord_watchlist,
                            watchlist=watchlist,
                            team_ids=team_ids,
                            messages=pushed_messages,
                        )

                should_poll_commands = (
                    args.discord_command_mode == "poll"
                    or not push_ready
                    or (args.discord_command_mode == "push" and args.discord_command_poll_seconds > 0)
                )
                if should_poll_commands and now_ts >= next_command_poll_at:
                    next_command_poll_at = now_ts + _effective_command_poll_seconds(args)
                    discord_watchlist, watchlist, team_ids, nicknames = process_discord_text_commands(
                        state=state,
                        args=args,
                        target_date=target_date,
                        bridge_id_map=bridge_id_map,
                        base_watchlist=base_watchlist,
                        nicknames=nicknames,
                        discord_watchlist=discord_watchlist,
                        watchlist=watchlist,
                        team_ids=team_ids,
                    )
        else:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
