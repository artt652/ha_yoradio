"""Platform for yoRadio integration."""
import logging
import voluptuous as vol
import json
import asyncio
import re
import hashlib
from urllib.parse import quote
from collections import OrderedDict
from typing import Optional, Dict, Any, List

from aiohttp import ClientError

from homeassistant.components import mqtt, media_source
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)

VERSION = "0.11.0"

_LOGGER = logging.getLogger(__name__)

FALLBACK_COVER = "https://raw.githubusercontent.com/home-assistant/brands/refs/heads/master/custom_integrations/yoradio/icon%402x.png"

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
CONF_COVER_SOURCES = "cover_sources"

DEFAULT_COVER_SOURCES = ["itunes", "ui_avatars", "gravatar"]
DEFAULT_MAX_VOLUME = 254
INITIAL_DELAY = 3
COVER_FETCH_TIMEOUT = 6
PLAYLIST_FETCH_TIMEOUT = 10
VOLUME_STEP = 0.05
COVER_CACHE_MAX = 200
UI_AVATAR_CACHE_MAX = 100

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ROOT_TOPIC, default="yoradio"): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MAX_VOLUME, default=str(DEFAULT_MAX_VOLUME)): cv.string,
        vol.Optional(CONF_COVER_SOURCES, default=DEFAULT_COVER_SOURCES): vol.All(
            cv.ensure_list,
            [vol.In(["itunes", "ui_avatars", "gravatar"])]
        ),
    }
)

# Bounded LRU cache to prevent unbounded memory growth
cover_cache: OrderedDict = OrderedDict()
ui_avatar_cache: OrderedDict = OrderedDict()

# Color scheme for UI Avatars
UI_AVATARS_COLORS = [
    "264653", "2a9d8f", "e9c46a", "f4a261", "e76f51",
    "e63946", "a8dadc", "457b9d", "1d3557", "2b2d42",
    "3d5a80", "98c1d9", "ee6c4d", "293241", "6c757d"
]


def cover_cache_get(key: str) -> Optional[str]:
    """Get value from cache and move to end (LRU)."""
    if key in cover_cache:
        cover_cache.move_to_end(key)
        return cover_cache[key]
    return None


def cover_cache_set(key: str, value: str) -> None:
    """Set value in cache with LRU eviction."""
    cover_cache[key] = value
    cover_cache.move_to_end(key)
    if len(cover_cache) > COVER_CACHE_MAX:
        cover_cache.popitem(last=False)


def clean_title(text: str) -> str:
    """Clean title from extra information."""
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


def parse_artist_title(artist: str, title: str) -> tuple[str, str]:
    """Parse artist and title from combined string."""
    if title and " - " in title:
        parts = title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return artist, title


def is_service_message(title: str) -> bool:
    """Check if message is a service/status message rather than actual track."""
    if not title:
        return True
    
    t = title.lower().strip()
    service_patterns = [
        r'host not available',
        r'error connecting',
        r'contenttype',
        r'unknown content',
        r'\[ready\]',
        r'\[stopped\]',
        r'\[connecting\]',
        r'\[соединение\]',
        r'\[готов\]',
        r'\[остановлено\]',
        r'^buffering',
        r'^загрузка',
        r'^подключение',
        r'^connecting',
        r'^\d+%',
    ]
    
    return any(re.search(pattern, t) for pattern in service_patterns)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the yoRadio platform."""
    root_topic = config.get(CONF_ROOT_TOPIC)
    name = config.get(CONF_NAME)
    max_volume = int(config.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME))
    cover_sources = config.get(CONF_COVER_SOURCES, DEFAULT_COVER_SOURCES)

    playlist = []
    api = yoradioApi(root_topic, hass, playlist)

    async_add_entities([yoradioDevice(name, max_volume, api, cover_sources)], True)


class yoradioApi:
    """API class for yoRadio MQTT communication."""

    def __init__(self, root_topic: str, hass, playlist: List):
        """Initialize the API."""
        self.hass = hass
        self.root_topic = root_topic
        self.playlist = playlist
        self.playlisturl = ""
        self._unsub_callbacks = []

    async def set_command(self, command: str) -> None:
        """Send a command via MQTT."""
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_volume(self, volume: int) -> None:
        """Set volume via MQTT."""
        command = "vol " + str(int(volume))
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_source(self, source: str) -> None:
        """Set source/station via MQTT."""
        parts = source.split(". ", 1)
        command = "play " + parts[0]
        await mqtt.async_publish(self.hass, self.root_topic + "/command", command)

    async def set_browse_media(self, media_content_id: str) -> None:
        """Send browse media command via MQTT."""
        await mqtt.async_publish(self.hass, self.root_topic + "/command", media_content_id)

    async def load_playlist(self, msg) -> None:
        """Load playlist from URL received via MQTT."""
        self.playlisturl = msg.payload
        session = async_get_clientsession(self.hass)
        
        try:
            async with session.get(self.playlisturl, timeout=PLAYLIST_FETCH_TIMEOUT) as resp:
                resp.raise_for_status()
                
                # Check content type
                content_type = resp.headers.get('Content-Type', '')
                if 'text/plain' not in content_type and 'text/html' not in content_type:
                    _LOGGER.warning(f"Unexpected content type for playlist: {content_type}")
                
                text = await resp.text(encoding="utf-8")
                
        except (ClientError, asyncio.TimeoutError) as e:
            _LOGGER.error(f"Unable to fetch playlist from {self.playlisturl}: {e}")
            return

        counter = 1
        self.playlist.clear()
        
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            
            # Try different separators
            for sep in ['\t', ',', ';']:
                if sep in line:
                    parts = line.split(sep, 1)
                    station_name = parts[0].strip()
                    if station_name:
                        self.playlist.append(f"{counter}. {station_name}")
                        counter += 1
                    break


class yoradioDevice(MediaPlayerEntity):
    """Representation of a yoRadio device."""

    def __init__(self, name: str, max_volume: int, api: yoradioApi, cover_sources: List[str]):
        """Initialize the device."""
        self._name = name
        self.api = api
        self._state = MediaPlayerState.OFF
        self._volume = 0
        self._max_volume = max_volume
        self._cover_sources = cover_sources

        self._media_content_type = MediaType.MUSIC
        self._media_title = ""
        self._track_artist = ""
        self._media_channel = ""
        self._media_image_url = FALLBACK_COVER

        self._current_source = None
        self._last_track = ""
        self._cover_fetch_id = 0
        self._unsub_callbacks = []
        self._attr_unique_id = f"yoradio_{api.root_topic}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT topics when entity is added."""
        await asyncio.sleep(INITIAL_DELAY)

        # Subscribe to MQTT topics and store callbacks for cleanup
        self._unsub_callbacks.append(
            await mqtt.async_subscribe(
                self.hass,
                self.api.root_topic + "/status",
                self.status_listener,
            )
        )
        self._unsub_callbacks.append(
            await mqtt.async_subscribe(
                self.hass,
                self.api.root_topic + "/playlist",
                self.playlist_listener,
            )
        )
        self._unsub_callbacks.append(
            await mqtt.async_subscribe(
                self.hass,
                self.api.root_topic + "/volume",
                self.volume_listener,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from MQTT topics when entity is removed."""
        for callback in self._unsub_callbacks:
            callback()

    async def status_listener(self, msg) -> None:
        """Handle status updates via MQTT."""
        try:
            js = json.loads(msg.payload)
        except json.JSONDecodeError as e:
            _LOGGER.debug(f"Invalid JSON in status message: {e}")
            return

        try:
            raw_title = js.get("title", "")
            station = js.get("name", "")

            # Save old values for change detection
            old_state = self._state
            old_title = self._media_title
            old_image = self._media_image_url

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

            # Only update if something changed
            if (old_state != self._state or 
                old_title != self._media_title or
                old_image != self._media_image_url):
                self.async_write_ha_state()

        except Exception as e:
            _LOGGER.debug(f"Status parse error: {e}")

    async def async_update_cover(self, artist: str, title: str, fetch_id: int) -> None:
        """Update cover image for current track."""
        if is_service_message(title):
            if fetch_id == self._cover_fetch_id:
                self._media_image_url = FALLBACK_COVER
                self.async_write_ha_state()
            return

        cover = await self.async_fetch_cover(artist, title)

        if fetch_id == self._cover_fetch_id:
            if cover:
                self._media_image_url = cover
                _LOGGER.debug(f"Cover found for {artist} - {title}")
            else:
                self._media_image_url = FALLBACK_COVER
                _LOGGER.debug(f"No cover found for {artist} - {title}, using fallback")
            
            self.async_write_ha_state()

    async def async_fetch_cover(self, artist: str, title: str) -> Optional[str]:
        """Multi-level cover fetch with fallbacks."""
        artist, title = parse_artist_title(artist, title)
        artist = clean_title(artist)
        title = clean_title(title)

        if not artist or not title:
            return None

        query = f"{artist} {title}"

        # Check cache
        cached = cover_cache_get(query)
        if cached is not None:
            return cached

        # Try configured sources in order
        for source in self._cover_sources:
            cover = None
            
            if source == "itunes":
                cover = await self._fetch_from_itunes(artist, title)
            elif source == "ui_avatars":
                cover = self._generate_ui_avatar(artist, title)
            elif source == "gravatar":
                cover = await self._fetch_from_gravatar(artist)
            
            if cover:
                cover_cache_set(query, cover)
                _LOGGER.debug(f"Cover found via {source} for {artist} - {title}")
                return cover

        return None

    async def _fetch_from_itunes(self, artist: str, title: str) -> Optional[str]:
        """Fetch cover from iTunes API."""
        session = async_get_clientsession(self.hass)
        params = {
            "term": f"{artist} {title}",
            "entity": "song",
            "limit": "1"
        }

        try:
            async with session.get(
                "https://itunes.apple.com/search",
                params=params,
                timeout=COVER_FETCH_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(f"iTunes API error: {resp.status}")
                    return None
                
                data = await resp.json(content_type=None)

            if data.get("resultCount", 0) == 0:
                return None

            art = data["results"][0].get("artworkUrl100")
            if not art:
                return None

            # Try to get better quality
            return art.replace("100x100", "400x400")

        except (ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            _LOGGER.debug(f"iTunes API error: {e}")
            return None

    def _generate_ui_avatar(self, artist: str, title: str) -> Optional[str]:
        """Generate avatar from initials using ui-avatars.com."""
        # Check cache for UI avatars
        cache_key = f"ui_{artist}_{title}"
        if cache_key in ui_avatar_cache:
            return ui_avatar_cache[cache_key]

        # Extract initials
        initials = ""
        artist_words = artist.split()[:2]
        title_words = title.split()[:2]

        for word in artist_words + title_words:
            if word and word[0].isalpha():
                initials += word[0].upper()
            if len(initials) >= 2:
                break

        if not initials:
            initials = artist[0].upper() if artist else "?"

        # Pick color based on hash
        color_index = hash(artist + title) % len(UI_AVATARS_COLORS)
        bg_color = UI_AVATARS_COLORS[color_index]

        # Build URL - using quote for safe URL encoding (string operation only)
        encoded_initials = quote(initials)
        
        url = (
            f"https://ui-avatars.com/api/"
            f"?name={encoded_initials}"
            f"&size=400"
            f"&background={bg_color}"
            f"&color=fff"
            f"&length={len(initials)}"
            f"&rounded=false"
            f"&bold=true"
            f"&format=svg"
        )
        
        # Cache the result
        if len(ui_avatar_cache) > UI_AVATAR_CACHE_MAX:
            ui_avatar_cache.popitem(last=False)
        ui_avatar_cache[cache_key] = url
        
        return url

    async def _fetch_from_gravatar(self, artist: str) -> Optional[str]:
        """Fetch avatar from Gravatar based on artist name."""
        # Create pseudo-email from artist name
        pseudo_email = f"{artist.lower().replace(' ', '')}@example.com".encode('utf-8')
        email_hash = hashlib.md5(pseudo_email).hexdigest()

        # Gravatar URL with size and 404 default
        gravatar_url = f"https://www.gravatar.com/avatar/{email_hash}?s=400&d=404"

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(gravatar_url, timeout=COVER_FETCH_TIMEOUT) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('Content-Type', '')
                    if content_type.startswith('image/'):
                        return gravatar_url
        except (ClientError, asyncio.TimeoutError) as e:
            _LOGGER.debug(f"Gravatar error: {e}")

        return None

    async def playlist_listener(self, msg) -> None:
        """Handle playlist updates via MQTT."""
        await self.api.load_playlist(msg)
        self.async_write_ha_state()

    async def volume_listener(self, msg) -> None:
        """Handle volume updates via MQTT."""
        try:
            volume_val = int(msg.payload)
            normalized = max(0, min(volume_val, self._max_volume)) / self._max_volume
            if abs(self._volume - normalized) > 0.01:  # Avoid unnecessary updates
                self._volume = normalized
                self.async_write_ha_state()
        except ValueError:
            _LOGGER.debug(f"Invalid volume value: {msg.payload}")

    @property
    def supported_features(self) -> int:
        """Return supported features."""
        return SUPPORT_YORADIO

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return self._name

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the device."""
        return self._state

    @property
    def volume_level(self) -> float:
        """Return the volume level."""
        return self._volume

    @property
    def media_title(self) -> str:
        """Return the media title."""
        return self._media_title

    @property
    def media_artist(self) -> str:
        """Return the media artist."""
        return self._track_artist

    @property
    def media_image_url(self) -> str:
        """Return the media image URL."""
        return self._media_image_url

    @property
    def source(self) -> Optional[str]:
        """Return the current source."""
        return self._current_source

    @property
    def source_list(self) -> List[str]:
        """Return the list of available sources."""
        return self.api.playlist

    @property
    def media_content_type(self) -> str:
        """Return the media content type."""
        return self._media_content_type

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes for diagnostics."""
        return {
            "mqtt_topic": self.api.root_topic,
            "last_track_id": self._last_track,
            "cover_fetch_id": self._cover_fetch_id,
            "cover_cache_size": len(cover_cache),
            "ui_avatar_cache_size": len(ui_avatar_cache),
            "cover_sources": self._cover_sources,
            "media_channel": self._media_channel,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={("yoradio", self.api.root_topic)},
            name=self._name,
            manufacturer="yoRadio",
            model="yoRadio Player",
            sw_version=VERSION,
        )

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level."""
        await self.api.set_volume(round(volume * self._max_volume))

    async def async_browse_media(self, media_content_type: str = None, media_content_id: str = None) -> BrowseMedia:
        """Browse media."""
        result = await media_source.async_browse_media(
            self.hass,
            media_content_id,
        )
        if result and result.children:
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
        media_type: str,
        media_id: str,
        enqueue: str = None,
        announce: bool = None,
        **kwargs,
    ) -> None:
        """Play media."""
        if media_source.is_media_source_id(media_id):
            media_type = MediaType.URL
            play_item = await media_source.async_resolve_media(
                self.hass,
                media_id,
                self.entity_id,
            )
            media_id = async_process_play_media_url(self.hass, play_item.url)

        await self.api.set_browse_media(media_id)

    async def async_select_source(self, source: str) -> None:
        """Select source/station."""
        await self.api.set_source(source)
        self._current_source = source
        self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        """Turn volume up."""
        new_vol = min(1.0, float(self._volume) + VOLUME_STEP)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_volume_down(self) -> None:
        """Turn volume down."""
        new_vol = max(0.0, float(self._volume) - VOLUME_STEP)
        await self.async_set_volume_level(new_vol)
        self._volume = new_vol

    async def async_media_next_track(self) -> None:
        """Skip to next track."""
        await self.api.set_command("next")

    async def async_media_previous_track(self) -> None:
        """Skip to previous track."""
        await self.api.set_command("prev")

    async def async_media_stop(self) -> None:
        """Stop playback."""
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        """Start playback."""
        await self.api.set_command("start")
        self._state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self.api.set_command("stop")
        self._state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off device."""
        await self.api.set_command("turnoff")
        self._state = MediaPlayerState.OFF
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on device."""
        await self.api.set_command("turnon")
        self._state = MediaPlayerState.IDLE
        self.async_write_ha_state()
