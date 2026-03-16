"""ёRadio media player platform."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict

from aiohttp import ClientError

from homeassistant.components import mqtt, media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerEnqueue,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_MAX_VOLUME,
    CONF_ROOT_TOPIC,
    COVER_CACHE_MAX,
    DEFAULT_MAX_VOLUME,
    DEFAULT_NAME,
    FALLBACK_COVER,
)

_LOGGER = logging.getLogger(__name__)

VERSION = "0.10.0"

SUPPORT_YORADIO = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.PLAY_MEDIA
)

# ---------------------------------------------------------------------------
# Bounded LRU cover art cache (module-level, shared across reloads)
# ---------------------------------------------------------------------------
_cover_cache: OrderedDict = OrderedDict()


def _cover_cache_get(key: str) -> str | None:
    if key in _cover_cache:
        _cover_cache.move_to_end(key)
        return _cover_cache[key]
    return None


def _cover_cache_set(key: str, value: str) -> None:
    _cover_cache[key] = value
    _cover_cache.move_to_end(key)
    if len(_cover_cache) > COVER_CACHE_MAX:
        _cover_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean_title(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\(.*?\)", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\|.*", "", text)
    text = re.sub(r"feat\..*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"ft\..*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"official.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"video.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"hd.*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_artist_title(artist: str, title: str) -> tuple[str, str]:
    if title and " - " in title:
        parts = title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return artist, title


def _is_service_message(title: str) -> bool:
    if not title:
        return True
    t = title.lower()
    service_words = [
        "host not available",
        "error connecting to",
        "contenttype",
        "[ready]",
        "[stopped]",
        "[connecting]",
        "[соединение]",
        "[готов]",
        "[остановлено]",
    ]
    return any(word in t for word in service_words)


# ---------------------------------------------------------------------------
# Platform setup — config-entry path
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ёRadio media player from a config entry."""
    data = entry.data
    root_topic: str = data[CONF_ROOT_TOPIC]
    name: str = data.get(CONF_NAME, DEFAULT_NAME)
    max_volume: int = int(data.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME))

    playlist: list[str] = []
    api = YoRadioApi(root_topic, hass, playlist)

    async_add_entities([YoRadioDevice(name, max_volume, api, entry.entry_id)], True)


# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------

class YoRadioApi:
    """Thin wrapper around MQTT publish calls for ёRadio."""

    def __init__(
        self,
        root_topic: str,
        hass: HomeAssistant,
        playlist: list[str],
    ) -> None:
        self.hass = hass
        self.root_topic = root_topic
        self.playlist = playlist
        self.playlisturl = ""

    async def set_command(self, command: str) -> None:
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_volume(self, volume: int) -> None:
        await mqtt.async_publish(
            self.hass, self.root_topic + "/command", f"vol {volume}"
        )

    async def set_source(self, source: str) -> None:
        # Split on ". " with maxsplit=1 to handle station names that contain dots
        parts = source.split(". ", 1)
        await mqtt.async_publish(
            self.hass, self.root_topic + "/command", f"play {parts[0]}"
        )

    async def set_browse_media(self, media_content_id: str) -> None:
        await mqtt.async_publish(
            self.hass, self.root_topic + "/command", media_content_id
        )

    async def load_playlist(self, msg) -> None:
        self.playlisturl = msg.payload
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(self.playlisturl, timeout=10) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding="utf-8")
        except (ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.error("Unable to fetch playlist from %s: %s", self.playlisturl, exc)
            return

        counter = 1
        self.playlist.clear()
        for line in text.split("\n"):
            res = line.split("\t")
            if res[0] != "":
                self.playlist.append(f"{counter}. {res[0]}")
                counter += 1


# ---------------------------------------------------------------------------
# Media player entity
# ---------------------------------------------------------------------------

class YoRadioDevice(MediaPlayerEntity):
    """Representation of a ёRadio media player."""

    _attr_has_entity_name = True
    _attr_name = None  # uses device name as entity name

    def __init__(
        self,
        name: str,
        max_volume: int,
        api: YoRadioApi,
        entry_id: str,
    ) -> None:
        self._device_name = name
        self.api = api
        self._max_volume = max_volume
        self._entry_id = entry_id

        self._state = MediaPlayerState.OFF
        self._volume: float = 0.0
        self._media_title = ""
        self._track_artist = ""
        self._media_channel = ""
        self._media_image_url: str = FALLBACK_COVER
        self._current_source: str | None = None
        self._last_track = ""
        self._cover_fetch_id = 0

        # Stable unique_id derived from the MQTT root topic
        self._attr_unique_id = f"yoradio_{api.root_topic}"

    @property
    def device_info(self):
        return {
            "identifiers": {("yoradio", self.api.root_topic)},
            "name": self._device_name,
            "manufacturer": "ёRadio",
            "model": "yoRadio",
            "sw_version": VERSION,
        }

    async def async_added_to_hass(self) -> None:
        await asyncio.sleep(3)

        await mqtt.async_subscribe(
            self.hass,
            self.api.root_topic + "/status",
            self.status_listener,
        )
        await mqtt.async_subscribe(
            self.hass,
            self.api.root_topic + "/playlist",
            self.playlist_listener,
        )
        await mqtt.async_subscribe(
            self.hass,
            self.api.root_topic + "/volume",
            self.volume_listener,
        )

    # ------------------------------------------------------------------
    # MQTT listeners
    # ------------------------------------------------------------------

    async def status_listener(self, msg) -> None:
        try:
            js = json.loads(msg.payload)

            raw_title: str = js.get("title", "")
            station: str = js.get("name", "")

            if " - " in raw_title:
                parts = raw_title.split(" - ", 1)
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                artist = station
                title = raw_title

            self._media_title = title
            self._track_artist = artist
            self._media_channel = station

            track_id = artist + title
            if track_id != self._last_track:
                self._last_track = track_id
                self._cover_fetch_id += 1
                fetch_id = self._cover_fetch_id
                self.hass.async_create_task(
                    self.async_update_cover(artist, title, fetch_id)
                )

            if js["on"] == 1:
                self._state = (
                    MediaPlayerState.PLAYING
                    if js["status"] == 1
                    else MediaPlayerState.IDLE
                )
            else:
                self._state = MediaPlayerState.OFF

            self._current_source = f"{js['station']}. {js['name']}"

            self.async_write_ha_state()

        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("status parse error: %s", exc)

    async def async_update_cover(self, artist: str, title: str, fetch_id: int) -> None:
        if _is_service_message(title):
            if fetch_id == self._cover_fetch_id:
                self._media_image_url = FALLBACK_COVER
                self.async_write_ha_state()
            return

        cover = await self._async_fetch_cover(artist, title)

        if fetch_id == self._cover_fetch_id:
            self._media_image_url = cover if cover else FALLBACK_COVER
            self.async_write_ha_state()

    async def _async_fetch_cover(self, artist: str, title: str) -> str | None:
        artist, title = _parse_artist_title(artist, title)
        artist = _clean_title(artist)
        title = _clean_title(title)

        query = f"{artist} {title}".strip()
        if not query:
            return None

        cached = _cover_cache_get(query)
        if cached is not None:
            return cached

        session = async_get_clientsession(self.hass)
        params = {"term": query, "entity": "song", "limit": "1"}

        try:
            async with session.get(
                "https://itunes.apple.com/search", params=params, timeout=6
            ) as resp:
                data = await resp.json(content_type=None)

            if data.get("resultCount", 0) == 0:
                return None

            art: str | None = data["results"][0].get("artworkUrl100")
            if not art:
                return None

            art = art.replace("100x100", "600x600")
            _cover_cache_set(query, art)
            return art

        except (ClientError, asyncio.TimeoutError):
            return None

    async def playlist_listener(self, msg) -> None:
        await self.api.load_playlist(msg)
        self.async_write_ha_state()

    async def volume_listener(self, msg) -> None:
        self._volume = int(msg.payload) / self._max_volume
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return SUPPORT_YORADIO

    @property
    def state(self) -> MediaPlayerState:
        return self._state

    @property
    def volume_level(self) -> float:
        return self._volume

    @property
    def media_title(self) -> str:
        return self._media_title

    @property
    def media_artist(self) -> str:
        return self._track_artist

    @property
    def media_image_url(self) -> str:
        return self._media_image_url

    @property
    def source(self) -> str | None:
        return self._current_source

    @property
    def source_list(self) -> list[str]:
        return self.api.playlist

    # ------------------------------------------------------------------
    # Service handlers
    # ------------------------------------------------------------------

    async def async_set_volume_level(self, volume: float) -> None:
        await self.api.set_volume(round(volume * self._max_volume))

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        result = await media_source.async_browse_media(self.hass, media_content_id)
        if result.children:
            result.children = [
                child
                for child in result.children
                if child.can_expand
                or (
                    child.media_content_type is not None
                    and not child.media_content_type.startswith("image/")
                    and not child.media_content_type.startswith("video/")
                )
            ]
        return result

    async def async_play_media(
        self,
        media_type: str,
        media_id: str,
        enqueue: MediaPlayerEnqueue | None = None,
        announce: bool | None = None,
        **kwargs,
    ) -> None:
        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = async_process_play_media_url(self.hass, play_item.url)

        await self.api.set_browse_media(media_id)

    async def async_select_source(self, source: str) -> None:
        await self.api.set_source(source)
        self._current_source = source
        self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        new_vol = min(1.0, self._volume + 0.05)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_volume_down(self) -> None:
        new_vol = max(0.0, self._volume - 0.05)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_media_next_track(self) -> None:
        await self.api.set_command("next")

    async def async_media_previous_track(self) -> None:
        await self.api.set_command("prev")

    async def async_media_stop(self) -> None:
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE

    async def async_media_play(self) -> None:
        await self.api.set_command("start")
        self._state = MediaPlayerState.PLAYING

    async def async_media_pause(self) -> None:
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE

    async def async_turn_off(self) -> None:
        await self.api.set_command("turnoff")
        self._state = MediaPlayerState.OFF

    async def async_turn_on(self, **kwargs) -> None:
        await self.api.set_command("turnon")
        self._state = MediaPlayerState.IDLE
