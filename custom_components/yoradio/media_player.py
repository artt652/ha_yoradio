import logging
import voluptuous as vol
import json
import asyncio
import re
from collections import OrderedDict

from aiohttp import ClientError

from homeassistant.components import mqtt, media_source
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaPlayerEnqueue,
    MediaType,
)

VERSION = "0.10"

_LOGGER = logging.getLogger(__name__)

FALLBACK_COVER = "https://raw.githubusercontent.com/artt652/ha_yoradio/refs/heads/main/images/yoradio.svg"

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

DEFAULT_NAME = "yoRadio"
CONF_MAX_VOLUME = "max_volume"
CONF_ROOT_TOPIC = "root_topic"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ROOT_TOPIC, default="yoradio"): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MAX_VOLUME, default="254"): cv.string,
    }
)

# Bounded LRU cache to prevent unbounded memory growth
COVER_CACHE_MAX = 200
cover_cache: OrderedDict = OrderedDict()

def cover_cache_get(key):
    if key in cover_cache:
        cover_cache.move_to_end(key)
        return cover_cache[key]
    return None

def cover_cache_set(key, value):
    cover_cache[key] = value
    cover_cache.move_to_end(key)
    if len(cover_cache) > COVER_CACHE_MAX:
        cover_cache.popitem(last=False)

def clean_title(text):
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

def parse_artist_title(artist, title):
    if title and " - " in title:
        parts = title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return artist, title

def is_service_message(title: str) -> bool:
    if not title:
        return True
    t = title.lower()
    service_words = [
        "host not available",
        "Error connecting to",
        "ContentType",
        "unknown content found at",
        "[ready]",
        "[stopped]",
        "[connecting]",
        "[соединение]",
        "[готов]",
        "[остановлено]",
    ]
    return any(word in t for word in service_words)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    root_topic = config.get(CONF_ROOT_TOPIC)
    name = config.get(CONF_NAME)
    max_volume = int(config.get(CONF_MAX_VOLUME, 254))

    playlist = []
    api = yoradioApi(root_topic, hass, playlist)

    async_add_entities([yoradioDevice(name, max_volume, api)], True)

class yoradioApi:

    def __init__(self, root_topic, hass, playlist):
        self.hass = hass
        self.root_topic = root_topic
        self.playlist = playlist
        self.playlisturl = ""

    async def set_command(self, command):
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_volume(self, volume):
        command = "vol " + str(int(volume))
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_source(self, source):
        # split on ". " with maxsplit=1 to handle station names containing dots
        parts = source.split(". ", 1)
        command = "play " + parts[0]
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_browse_media(self, media_content_id):
        await mqtt.async_publish(self.hass, self.root_topic + "/command", media_content_id)

    async def load_playlist(self, msg):
        self.playlisturl = msg.payload
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(self.playlisturl, timeout=10) as resp:
                resp.raise_for_status()
                text = await resp.text(encoding="utf-8")
        except (ClientError, asyncio.TimeoutError) as e:
            _LOGGER.error(f"Unable to fetch playlist from {self.playlisturl}: {e}")
            return

        counter = 1
        self.playlist.clear()
        for line in text.split("\n"):
            res = line.split("\t")
            if res[0] != "":
                self.playlist.append(str(counter) + ". " + res[0])
                counter += 1

class yoradioDevice(MediaPlayerEntity):

    def __init__(self, name, max_volume, api):
        self._name = name
        self.api = api
        self._state = MediaPlayerState.OFF
        self._volume = 0
        self._max_volume = max_volume
        self._media_content_type = "music"
        self._media_title = ""
        self._track_artist = ""
        self._media_channel = ""
        self._media_image_url = FALLBACK_COVER

        self._current_source = None
        self._last_track = ""
        # incremented on each track change to discard stale cover fetches
        self._cover_fetch_id = 0

    async def async_added_to_hass(self):
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

    async def status_listener(self, msg):
        try:
            js = json.loads(msg.payload)

            raw_title = js.get("title", "")
            station = js.get("name", "")

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

            self._current_source = str(js["station"]) + ". " + js["name"]

            self.async_write_ha_state()

        except Exception as e:
            _LOGGER.debug(f"status parse error {e}")

    async def async_update_cover(self, artist, title, fetch_id: int):
        if is_service_message(title):
            if fetch_id == self._cover_fetch_id:
                self._media_image_url = FALLBACK_COVER
                self.async_write_ha_state()
            return

        cover = await self.async_fetch_cover(artist, title)

        if fetch_id == self._cover_fetch_id:
            self._media_image_url = cover if cover else FALLBACK_COVER
            self.async_write_ha_state()

    async def async_fetch_cover(self, artist, title):
        artist, title = parse_artist_title(artist, title)
        artist = clean_title(artist)
        title = clean_title(title)

        query = artist + " " + title

        cached = cover_cache_get(query)
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

            art = data["results"][0].get("artworkUrl100")
            if not art:
                return None

            art = art.replace("100x100", "600x600")
            cover_cache_set(query, art)
            return art

        except (ClientError, asyncio.TimeoutError):
            return None

    async def playlist_listener(self, msg):
        await self.api.load_playlist(msg)
        self.async_write_ha_state()

    async def volume_listener(self, msg):
        self._volume = int(msg.payload) / self._max_volume
        self.async_write_ha_state()

    @property
    def supported_features(self):
        return SUPPORT_YORADIO

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def volume_level(self):
        return self._volume

    @property
    def media_title(self):
        return self._media_title

    @property
    def media_artist(self):
        return self._track_artist

    @property
    def media_image_url(self):
        return self._media_image_url

    @property
    def source(self):
        return self._current_source

    @property
    def source_list(self):
        return self.api.playlist
        
    @property
    def media_content_type(self):
        return self._media_content_type
        
    async def async_set_volume_level(self, volume):
        await self.api.set_volume(round(volume * self._max_volume))

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        result = await media_source.async_browse_media(
            self.hass,
            media_content_id,
        )
        if result.children:
            result.children = [
                child for child in result.children
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
        media_type,
        media_id,
        enqueue=None,
        announce=None,
        **kwargs,
    ):
        if media_source.is_media_source_id(media_id):
            media_type = MediaType.URL
            play_item = await media_source.async_resolve_media(
                self.hass,
                media_id,
                self.entity_id,
            )
            media_id = async_process_play_media_url(self.hass, play_item.url)

        await self.api.set_browse_media(media_id)

    async def async_select_source(self, source):
        await self.api.set_source(source)
        self._current_source = source
        self.async_write_ha_state()

    async def async_volume_up(self):
        new_vol = min(1.0, float(self._volume) + 0.05)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_volume_down(self):
        new_vol = max(0.0, float(self._volume) - 0.05)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_media_next_track(self):
        await self.api.set_command("next")

    async def async_media_previous_track(self):
        await self.api.set_command("prev")

    async def async_media_stop(self):
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE

    async def async_media_play(self):
        await self.api.set_command("start")
        self._state = MediaPlayerState.PLAYING

    async def async_media_pause(self):
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE

    async def async_turn_off(self):
        await self.api.set_command("turnoff")
        self._state = MediaPlayerState.OFF

    async def async_turn_on(self, **kwargs):
        await self.api.set_command("turnon")
        self._state = MediaPlayerState.IDLE
