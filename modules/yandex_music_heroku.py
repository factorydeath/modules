# requires: requests websockets betterproto aiohttp aiofiles pillow yt-dlp curl_cffi
# meta developer: @yandex_music_sdk_pro
# scope: ffmpeg
# scope: heroku_min 1.5.0
# scope: hikka_min 1.6.0
"""🎧 YanMusic — Яндекс Музыка в стиле SpotifyMod: async SDK + PIL-баннеры.

Внутри: config, errors, utils, models, services (22 домена, Async*),
remote (snapshot, AsyncRemotePlayer, aonshot_*), клиент AsyncYandexMusic,
Banners (horizontal/vertical/ultra) + YanMusicMod (ym* команды).
"""

from __future__ import annotations

__version__ = (1, 5, 0)

import asyncio
import contextlib
import functools
import html
import io
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
import traceback
from dataclasses import dataclass, field
from types import FunctionType
from typing import Any

import requests

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:  # Heroku-форк coddrago
    from heroku import loader, utils
    from herokutl.tl.types import Message
except ImportError:  # Hikka / FTG fallback
    from .. import loader, utils  # type: ignore
    from hikkatl.types import Message  # type: ignore



# ==================== SDK: config.py ====================
"""Конфигурация SDK."""
from dataclasses import dataclass, field

@dataclass(slots=True)
class RetryConfig:
    """Параметры повторных попыток.

    Retry применяется только к идемпотентным/безопасным операциям
    (чтение: search, tracks, account...). Команды управления плеером
    (pause/next/volume) намеренно НЕ ретраятся автоматически, чтобы
    не получить двойную команду при обрыве соединения.
    """
    attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 4.0

@dataclass(slots=True)
class SDKConfig:
    """Глобальные настройки SDK."""
    token: str | None = None
    language: str = 'ru'
    base_url: str | None = None
    timeout: float = 20.0
    retry: RetryConfig = field(default_factory=RetryConfig)
    debug: bool = False
    report_unknown_fields: bool = False
    device_id: str | None = None
    device_title: str = 'Python SDK'
    max_reconnect_attempts: int | None = None

    def validate_token(self, *, allow_empty: bool=False) -> str:
        """Вернуть токен или кинуть понятную ошибку."""
        token = (self.token or '').strip()
        if not token and (not allow_empty):
            raise AuthenticationError("Missing Yandex Music token. Pass token='...' or set YANDEX_MUSIC_TOKEN env var.")
        return token

    def client_kwargs(self) -> dict:
        """Kwargs для upstream Client / ClientAsync."""
        kwargs: dict = {'language': self.language, 'report_unknown_fields': self.report_unknown_fields}
        if self.base_url:
            kwargs['base_url'] = self.base_url
        return kwargs

    def ynison_kwargs(self) -> dict:
        """Kwargs для upstream YnisonClient / YnisonClientAsync."""
        kwargs: dict = {'device_title': self.device_title}
        if self.device_id:
            kwargs['device_id'] = self.device_id
        if self.max_reconnect_attempts is not None:
            kwargs['max_reconnect_attempts'] = self.max_reconnect_attempts
        return kwargs

# ==================== SDK: errors.py ====================
"""Исключения SDK.

Все публичные ошибки SDK наследуются от SDKError, поэтому приложение может
обрабатывать только одну базовую ошибку, если ему не нужны детали.
"""
from contextlib import contextmanager
from typing import Iterator

class SDKError(Exception):
    """Базовая ошибка Yandex Music SDK."""

class ConfigurationError(SDKError):
    """Некорректная конфигурация клиента."""

class AuthenticationError(SDKError):
    """Ошибка авторизации или отсутствующий токен."""

class APIError(SDKError):
    """Ошибка upstream/API."""

class NotFoundError(APIError):
    """Сервер вернул 404."""

class RemoteError(SDKError):
    """Ошибка Ynison remote playback."""

class RemoteConnectionError(RemoteError):
    """Нет соединения / таймаут ожидания состояния."""

class NoActiveDeviceError(RemoteError):
    """Нет активного устройства (нечего ставить на паузу)."""

class QueueBoundaryError(RemoteError):
    """Граница очереди (next на последнем / prev на первом треке)."""

class UnsupportedOperation(RemoteError):
    """Операция не поддерживается текущей версией upstream."""

class RetryExhausted(APIError):
    """Все попытки повторного запроса исчерпаны."""

def map_upstream_error(exc: Exception) -> SDKError:
    """Преобразовать исключение upstream в ошибку SDK (без потери цепочки)."""
    name = type(exc).__name__
    msg = str(exc)
    if name in ('YnisonNoActiveDeviceError', 'NoActiveDeviceError'):
        return NoActiveDeviceError(msg)
    if name in ('YnisonQueueBoundaryError', 'QueueBoundaryError'):
        return QueueBoundaryError(msg)
    if name in ('YnisonTimeoutError', 'YnisonConnectionClosedError', 'YnisonError', 'YnisonUnauthorizedError', 'YnisonDeviceDisplacedError', 'YnisonServerError'):
        if 'nonauthor' in name.lower() or 'unauthorized' in msg.lower() or '401' in msg:
            return AuthenticationError(msg)
        if 'timeout' in name.lower():
            return RemoteConnectionError(msg)
        return RemoteError(msg)
    if name in ('UnauthorizedError', 'BadRequestError') or '401' in msg or 'unauthor' in msg.lower():
        return AuthenticationError(msg)
    if name == 'NotFoundError' or '404' in msg:
        return NotFoundError(msg)
    if name in ('YandexMusicError', 'NetworkError', 'TimedOutError', 'DeviceAuthError') or name.endswith('Error'):
        mapped: APIError = APIError(f'{name}: {msg}')
        return mapped
    return APIError(msg)

@contextmanager
def map_errors(*, remote: bool=False) -> Iterator[None]:
    """Контекст: любое исключение upstream -> SDKError."""
    try:
        yield
    except SDKError:
        raise
    except Exception as exc:
        mapped = map_upstream_error(exc)
        if remote and isinstance(mapped, APIError) and (not isinstance(mapped, RemoteError)):
            raise RemoteError(str(mapped)) from exc
        raise mapped from exc

# ==================== SDK: utils.py (async-only) ====================
"""Маленькие внутренние утилиты SDK."""
import asyncio
import inspect
import logging
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
T = TypeVar('T')
log = logging.getLogger('yandex_music_sdk')

def setup_logging(debug: bool=False) -> logging.Logger:
    """Настроить логгер SDK (идемпотентно)."""
    level = logging.DEBUG if debug else logging.INFO
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
        log.addHandler(handler)
    log.setLevel(level)
    logging.getLogger('yandex_music').setLevel(level)
    return log

def resolve_token(explicit: str | None=None, config_token: str | None=None) -> str | None:
    """Приоритет токена: явный -> конфиг -> env."""
    if explicit and explicit.strip():
        return explicit.strip()
    if config_token and config_token.strip():
        return config_token.strip()
    env = os.environ.get('YANDEX_MUSIC_TOKEN') or os.environ.get('TOKEN')
    return env.strip() if env and env.strip() else None

async def retry_async(fn: Callable[[], Awaitable[T]], attempts: int, base_delay: float, max_delay: float) -> T:
    """Async-вариант exponential backoff."""
    last: Exception | None = None
    total = max(1, attempts)
    for number in range(total):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            if number + 1 >= total:
                break
            delay = min(max_delay, base_delay * 2 ** number) * random.uniform(0.8, 1.2)
            log.debug('Async retry %s/%s after %.2fs: %r', number + 1, total, delay, exc)
            await asyncio.sleep(delay)
    assert last is not None
    raise RetryExhausted(f'Exhausted {total} attempts, last: {last}') from last

def is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)

async def await_if_needed(value: Any) -> Any:
    if callable(value):
        value = value()
    if inspect.isawaitable(value):
        return await value
    return value

def clamp_volume(value: float) -> float:
    """Upstream обрезает громкость, но SDK валидирует строго."""
    v = float(value)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f'volume must be between 0.0 and 1.0, got {value!r}')
    return v

# ==================== SDK: models.py ====================
"""Стабильные DTO поверх динамических upstream-моделей."""
from dataclasses import dataclass
from typing import Any

def _fmt_ms(value: int | None) -> str:
    if value is None:
        return '-'
    total_s = max(0, int(value) // 1000)
    return f'{total_s // 60}:{total_s % 60:02d}'

@dataclass(frozen=True, slots=True)
class Device:
    """Нормализованная информация об устройстве Ynison."""
    id: str
    title: str | None = None
    device_type: str | None = None
    active: bool = False
    raw: Any = None

    def describe(self) -> str:
        marker = '*' if self.active else ' '
        name = self.title or self.id
        dtype = f' [{self.device_type}]' if self.device_type else ''
        return f'{marker} {name}{dtype} ({self.id})'

@dataclass(frozen=True, slots=True)
class Playback:
    """Нормализованное состояние проигрывателя."""
    active_device_id: str | None
    paused: bool | None
    progress_ms: int | None
    duration_ms: int | None
    track_id: str | None
    track_title: str | None
    artist_title: str | None = None
    devices: tuple[Device, ...] = ()
    raw: Any = None

    def describe(self) -> str:
        if self.paused is True:
            state = 'paused'
        elif self.paused is False:
            state = 'playing'
        else:
            state = 'unknown'
        track = self.track_title or '-'
        if self.artist_title:
            track = f'{self.artist_title} - {track}'
        pos = f'{_fmt_ms(self.progress_ms)}/{_fmt_ms(self.duration_ms)}'
        dev = self.active_device_id or '(no active device)'
        lines = [f'track: {track}', f'state: {state} {pos}', f'device: {dev}']
        if self.devices:
            lines.append('devices:')
            lines.extend((f'  {d.describe()}' for d in self.devices))
        return '\n'.join(lines)

@dataclass(frozen=True, slots=True)
class SearchItem:
    """Минимальная нормализованная запись результата поиска."""
    kind: str
    id: str | None
    title: str | None
    raw: Any

# ==================== SDK: services.py (async-only) ====================
"""Доменные сервисы.

Источник истины для endpoint-ов — upstream `yandex-music`.
Сервисы тонкие: валидация + маппинг ошибок + retry для безопасных
операций чтения. Любой новый endpoint upstream сразу доступен через
`GenericService` (`ym.api.*` / `ym.raw.*`) без обновления SDK.
"""
from typing import Any

async def _call_read_async(config: SDKConfig | None, fn):
    if config is None:
        result = fn()
        if hasattr(result, '__await__'):
            return await result
        return result
    r = config.retry

    async def wrapper():
        result = fn()
        if hasattr(result, '__await__'):
            return await result
        return result
    return await retry_async(wrapper, r.attempts, r.base_delay, r.max_delay)

class _AsyncBase:

    def __init__(self, client: Any, config: SDKConfig | None=None):
        self._c = client
        self._config = config

    async def _read(self, fn):
        with map_errors():
            return await _call_read_async(self._config, fn)

    async def _write(self, fn):
        with map_errors():
            result = fn()
            if hasattr(result, '__await__'):
                return await result
            return result

class AsyncAccountService(_AsyncBase):

    async def status(self):
        return await self._read(lambda: self._c.account_status())

    async def settings(self):
        return await self._read(lambda: self._c.account_settings())

    async def set_settings(self, *args, **kwargs):
        return await self._write(lambda: self._c.account_settings_set(*args, **kwargs))

    async def experiments(self):
        return await self._read(lambda: self._c.account_experiments())

    async def experiments_details(self, *args, **kwargs):
        return await self._read(lambda: self._c.account_experiments_details(*args, **kwargs))

    async def permission_alerts(self, *args, **kwargs):
        return await self._read(lambda: self._c.permission_alerts(*args, **kwargs))

    async def settings_general(self, *args, **kwargs):
        return await self._read(lambda: self._c.settings(*args, **kwargs))

    async def consume_promo_code(self, *args, **kwargs):
        return await self._write(lambda: self._c.consume_promo_code(*args, **kwargs))

class AsyncSearchService(_AsyncBase):

    async def query(self, text: str, page: int=0, type_: str='all', nocorrect: bool=False, **kwargs):
        return await self._read(lambda: self._c.search(text, page=page, type_=type_, nocorrect=nocorrect, **kwargs))

    async def suggest(self, text: str, **kwargs):
        return await self._read(lambda: self._c.search_suggest(text, **kwargs))

class AsyncTrackService(_AsyncBase):

    async def get(self, track_ids):
        if isinstance(track_ids, (str, int)):
            track_ids = [track_ids]
        tids = list(track_ids)
        return await self._read(lambda: self._c.tracks(tids))

    async def full(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_full_info(track_id, *args, **kwargs))

    async def similar(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_similar(track_id, *args, **kwargs))

    async def lyrics(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_lyrics(track_id, *args, **kwargs))

    async def download_info(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_download_info(track_id, *args, **kwargs))

    async def trailer(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_trailer(track_id, *args, **kwargs))

    async def supplement(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.track_supplement(track_id, *args, **kwargs))

    async def after(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.after_track(track_id, *args, **kwargs))

    async def play_audio(self, *args, **kwargs):
        return await self._read(lambda: self._c.play_audio(*args, **kwargs))

    async def credits(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_credits(track_id, *args, **kwargs))

    async def disclaimer(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_disclaimer(track_id, *args, **kwargs))

class AsyncArtistService(_AsyncBase):

    async def get(self, artist_ids):
        if isinstance(artist_ids, (str, int)):
            artist_ids = [artist_ids]
        aids = list(artist_ids)
        return await self._read(lambda: self._c.artists(aids))

    async def brief_info(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_brief_info(artist_id, *args, **kwargs))

    async def tracks(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_tracks(artist_id, *args, **kwargs))

    async def track_ids(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_track_ids(artist_id, *args, **kwargs))

    async def similar(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_similar(artist_id, *args, **kwargs))

    async def info(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_info(artist_id, *args, **kwargs))

    async def about(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_about(artist_id, *args, **kwargs))

    async def links(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_links(artist_id, *args, **kwargs))

    async def clips(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_clips(artist_id, *args, **kwargs))

    async def concerts(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_concerts(artist_id, *args, **kwargs))

    async def direct_albums(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_direct_albums(artist_id, *args, **kwargs))

    async def discography(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_discography_albums(artist_id, *args, **kwargs))

    async def also_albums(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_also_albums(artist_id, *args, **kwargs))

    async def donation(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_donation(artist_id, *args, **kwargs))

    async def skeleton(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_skeleton(artist_id, *args, **kwargs))

    async def trailer(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_trailer(artist_id, *args, **kwargs))

    async def disclaimer(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_disclaimer(artist_id, *args, **kwargs))

class AsyncAlbumService(_AsyncBase):

    async def get(self, album_ids):
        if isinstance(album_ids, (str, int)):
            album_ids = [album_ids]
        aids = list(album_ids)
        return await self._read(lambda: self._c.albums(aids))

    async def with_tracks(self, album_id, *args, **kwargs):
        return await self._read(lambda: self._c.albums_with_tracks(album_id, *args, **kwargs))

    async def similar_entities(self, album_id, *args, **kwargs):
        return await self._read(lambda: self._c.albums_similar_entities(album_id, *args, **kwargs))

    async def trailer(self, album_id, *args, **kwargs):
        return await self._read(lambda: self._c.albums_trailer(album_id, *args, **kwargs))

    async def disclaimer(self, album_id, *args, **kwargs):
        return await self._read(lambda: self._c.albums_disclaimer(album_id, *args, **kwargs))

class AsyncPlaylistService(_AsyncBase):

    async def list(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists_list(user_id, **kwargs))

    async def kinds(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists_kinds(user_id, **kwargs))

    async def get(self, kind, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists(kind, user_id, **kwargs))

    async def tracks(self, kind, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists(kind, user_id, **kwargs))

    async def by_uuid(self, playlist_uuid, **kwargs):
        return await self._read(lambda: self._c.playlist(playlist_uuid, **kwargs))

    async def by_ids(self, playlist_ids, **kwargs):
        return await self._read(lambda: self._c.playlists(playlist_ids, **kwargs))

    async def short_list(self, playlist_ids, **kwargs):
        return await self._read(lambda: self._c.playlists_list(playlist_ids, **kwargs))

    async def personal(self, playlist_id, **kwargs):
        return await self._read(lambda: self._c.playlists_personal(playlist_id, **kwargs))

    async def recommendations(self, kind, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists_recommendations(kind, user_id, **kwargs))

    async def similar_entities(self, playlist_uuid, **kwargs):
        return await self._read(lambda: self._c.playlist_similar_entities(playlist_uuid, **kwargs))

    async def trailer(self, kind, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_playlists_trailer(kind, user_id, **kwargs))

    async def create(self, title: str, visibility: str='public', user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_create(title, visibility, user_id, **kwargs))

    async def delete(self, kind, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_delete(kind, user_id, **kwargs))

    async def rename(self, kind, name: str, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_name(kind, name, user_id, **kwargs))

    async def set_visibility(self, kind, visibility: str, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_visibility(kind, visibility, user_id, **kwargs))

    async def set_description(self, kind, description: str, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_description(kind, description, user_id, **kwargs))

    async def change(self, kind, diff, revision: int=1, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_change(kind, diff, revision, user_id, **kwargs))

    async def insert_track(self, kind, track_id, album_id, at: int=0, revision: int=1, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_insert_track(kind, track_id, album_id, at, revision, user_id, **kwargs))

    async def delete_track(self, kind, from_: int, to: int, revision: int=1, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_playlists_delete_track(kind, from_, to, revision, user_id, **kwargs))

    async def join_collective(self, user_id: int, token: str, **kwargs):
        return await self._write(lambda: self._c.playlists_collective_join(user_id, token, **kwargs))

class AsyncLikesService(_AsyncBase):

    async def tracks(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_likes_tracks(user_id, **kwargs))

    async def add_track(self, track_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_tracks_add(track_ids, user_id, **kwargs))

    async def remove_track(self, track_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_tracks_remove(track_ids, user_id, **kwargs))

    async def artists(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_likes_artists(user_id, **kwargs))

    async def add_artist(self, artist_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_artists_add(artist_ids, user_id, **kwargs))

    async def remove_artist(self, artist_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_artists_remove(artist_ids, user_id, **kwargs))

    async def albums(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_likes_albums(user_id, **kwargs))

    async def add_album(self, album_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_albums_add(album_ids, user_id, **kwargs))

    async def remove_album(self, album_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_albums_remove(album_ids, user_id, **kwargs))

    async def playlists(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_likes_playlists(user_id, **kwargs))

    async def add_playlist(self, playlist_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_playlists_add(playlist_ids, user_id, **kwargs))

    async def remove_playlist(self, playlist_ids, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_playlists_remove(playlist_ids, user_id, **kwargs))

    async def clips(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_likes_clips(user_id, **kwargs))

    async def add_clip(self, clip_id, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_clips_add(clip_id, user_id, **kwargs))

    async def remove_clip(self, clip_id, user_id=None, **kwargs):
        return await self._write(lambda: self._c.users_likes_clips_remove(clip_id, user_id, **kwargs))

    async def disliked_tracks(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_dislikes_tracks(user_id, **kwargs))

    async def add_disliked_track(self, *args, **kwargs):
        return await self._write(lambda: self._c.users_dislikes_tracks_add(*args, **kwargs))

    async def remove_disliked_track(self, *args, **kwargs):
        return await self._write(lambda: self._c.users_dislikes_tracks_remove(*args, **kwargs))

    async def disliked_artists(self, user_id=None, **kwargs):
        return await self._read(lambda: self._c.users_dislikes_artists(user_id, **kwargs))

    async def add_disliked_artist(self, *args, **kwargs):
        return await self._write(lambda: self._c.users_dislikes_artists_add(*args, **kwargs))

    async def remove_disliked_artist(self, *args, **kwargs):
        return await self._write(lambda: self._c.users_dislikes_artists_remove(*args, **kwargs))

class AsyncQueueService(_AsyncBase):

    async def list(self, *args, **kwargs):
        return await self._read(lambda: self._c.queues_list(*args, **kwargs))

    async def get(self, queue_id, *args, **kwargs):
        return await self._read(lambda: self._c.queue(queue_id, *args, **kwargs))

    async def create(self, *args, **kwargs):
        return await self._write(lambda: self._c.queue_create(*args, **kwargs))

    async def update_position(self, *args, **kwargs):
        return await self._write(lambda: self._c.queue_update_position(*args, **kwargs))

class AsyncRadioService(_AsyncBase):

    async def dashboard(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_stations_dashboard(*args, **kwargs))

    async def stations(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_stations_list(*args, **kwargs))

    async def info(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_station_info(*args, **kwargs))

    async def tracks(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_station_tracks(*args, **kwargs))

    async def feedback(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_station_feedback(*args, **kwargs))

    async def feedback_started(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_station_feedback_radio_started(*args, **kwargs))

    async def feedback_skip(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_station_feedback_skip(*args, **kwargs))

    async def feedback_finished(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_station_feedback_track_finished(*args, **kwargs))

    async def feedback_track_started(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_station_feedback_track_started(*args, **kwargs))

    async def account_status(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_account_status(*args, **kwargs))

    async def settings2(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_station_settings2(*args, **kwargs))

class AsyncRotorSessionService(_AsyncBase):

    async def new(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_session_new(*args, **kwargs))

    async def tracks(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_session_tracks(*args, **kwargs))

    async def feedback(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_session_feedback(*args, **kwargs))

    async def feedbacks(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_session_feedbacks(*args, **kwargs))

    async def clone(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_session_clone(*args, **kwargs))

    async def combined_new(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_combined_session_new(*args, **kwargs))

    async def combined_next(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_combined_session_next(*args, **kwargs))

    async def combined_landing(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_combined_session_landing(*args, **kwargs))

class AsyncWaveService(_AsyncBase):

    async def last(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_wave_last(*args, **kwargs))

    async def reset(self, *args, **kwargs):
        return await self._write(lambda: self._c.rotor_wave_last_reset(*args, **kwargs))

    async def settings(self, *args, **kwargs):
        return await self._read(lambda: self._c.rotor_wave_settings(*args, **kwargs))

class AsyncHistoryService(_AsyncBase):

    async def list(self, *args, **kwargs):
        return await self._read(lambda: self._c.music_history(*args, **kwargs))

    async def items(self, *args, **kwargs):
        return await self._read(lambda: self._c.music_history_items(*args, **kwargs))

class AsyncLandingService(_AsyncBase):

    async def landing(self, *args, **kwargs):
        return await self._read(lambda: self._c.landing(*args, **kwargs))

    async def feed(self, *args, **kwargs):
        return await self._read(lambda: self._c.feed(*args, **kwargs))

    async def chart(self, *args, **kwargs):
        return await self._read(lambda: self._c.chart(*args, **kwargs))

    async def genres(self, *args, **kwargs):
        return await self._read(lambda: self._c.genres(*args, **kwargs))

    async def tags(self, *args, **kwargs):
        return await self._read(lambda: self._c.tags(*args, **kwargs))

    async def podcasts(self, *args, **kwargs):
        return await self._read(lambda: self._c.podcasts(*args, **kwargs))

    async def new_releases(self, *args, **kwargs):
        return await self._read(lambda: self._c.new_releases(*args, **kwargs))

    async def new_playlists(self, *args, **kwargs):
        return await self._read(lambda: self._c.new_playlists(*args, **kwargs))

class AsyncClipsService(_AsyncBase):

    async def get(self, clip_ids, *args, **kwargs):
        if isinstance(clip_ids, (str, int)):
            clip_ids = [clip_ids]
        return await self._read(lambda: self._c.clips(list(clip_ids), *args, **kwargs))

    async def will_like(self, *args, **kwargs):
        return await self._read(lambda: self._c.clips_will_like(*args, **kwargs))

    async def credits(self, clip_id, *args, **kwargs):
        return await self._read(lambda: self._c.clips_credits(clip_id, *args, **kwargs))

    async def disclaimer(self, clip_id, *args, **kwargs):
        return await self._read(lambda: self._c.clips_disclaimer(clip_id, *args, **kwargs))

class AsyncConcertsService(_AsyncBase):

    async def info(self, concert_id, *args, **kwargs):
        return await self._read(lambda: self._c.concert_info(concert_id, *args, **kwargs))

    async def feed(self, *args, **kwargs):
        return await self._read(lambda: self._c.concerts_feed(*args, **kwargs))

    async def locations(self, *args, **kwargs):
        return await self._read(lambda: self._c.concerts_locations(*args, **kwargs))

    async def tab_config(self, *args, **kwargs):
        return await self._read(lambda: self._c.concerts_tab_config(*args, **kwargs))

    async def skeleton(self, concert_id, *args, **kwargs):
        return await self._read(lambda: self._c.concert_skeleton(concert_id, *args, **kwargs))

class AsyncCreditsService(_AsyncBase):

    async def track_credits(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_credits(track_id, *args, **kwargs))

    async def clip_credits(self, clip_id, *args, **kwargs):
        return await self._read(lambda: self._c.clips_credits(clip_id, *args, **kwargs))

class AsyncLabelsService(_AsyncBase):

    async def get(self, label_id, *args, **kwargs):
        return await self._read(lambda: self._c.label(label_id, *args, **kwargs))

    async def albums(self, label_id, *args, **kwargs):
        return await self._read(lambda: self._c.label_albums(label_id, *args, **kwargs))

    async def artists(self, label_id, *args, **kwargs):
        return await self._read(lambda: self._c.label_artists(label_id, *args, **kwargs))

class AsyncMetatagsService(_AsyncBase):

    async def list(self, *args, **kwargs):
        return await self._read(lambda: self._c.metatags(*args, **kwargs))

    async def get(self, metatag_id, *args, **kwargs):
        return await self._read(lambda: self._c.metatag(metatag_id, *args, **kwargs))

    async def albums(self, metatag_id, *args, **kwargs):
        return await self._read(lambda: self._c.metatag_albums(metatag_id, *args, **kwargs))

    async def artists(self, metatag_id, *args, **kwargs):
        return await self._read(lambda: self._c.metatag_artists(metatag_id, *args, **kwargs))

    async def playlists(self, metatag_id, *args, **kwargs):
        return await self._read(lambda: self._c.metatag_playlists(metatag_id, *args, **kwargs))

class AsyncPinsService(_AsyncBase):

    async def list(self, *args, **kwargs):
        return await self._read(lambda: self._c.pins(*args, **kwargs))

    async def pin_album(self, album_id, *args, **kwargs):
        return await self._write(lambda: self._c.pin_album(album_id, *args, **kwargs))

    async def unpin_album(self, album_id, *args, **kwargs):
        return await self._write(lambda: self._c.unpin_album(album_id, *args, **kwargs))

    async def pin_artist(self, artist_id, *args, **kwargs):
        return await self._write(lambda: self._c.pin_artist(artist_id, *args, **kwargs))

    async def unpin_artist(self, artist_id, *args, **kwargs):
        return await self._write(lambda: self._c.unpin_artist(artist_id, *args, **kwargs))

    async def pin_playlist(self, kind, user_id=None, *args, **kwargs):
        return await self._write(lambda: self._c.pin_playlist(kind, user_id, *args, **kwargs))

    async def unpin_playlist(self, kind, user_id=None, *args, **kwargs):
        return await self._write(lambda: self._c.unpin_playlist(kind, user_id, *args, **kwargs))

    async def pin_wave(self, *args, **kwargs):
        return await self._write(lambda: self._c.pin_wave(*args, **kwargs))

    async def unpin_wave(self, *args, **kwargs):
        return await self._write(lambda: self._c.unpin_wave(*args, **kwargs))

class AsyncPresavesService(_AsyncBase):

    async def list(self, *args, **kwargs):
        return await self._read(lambda: self._c.users_presaves(*args, **kwargs))

    async def add(self, album_id, *args, **kwargs):
        return await self._write(lambda: self._c.users_presaves_add(album_id, *args, **kwargs))

    async def remove(self, album_id, *args, **kwargs):
        return await self._write(lambda: self._c.users_presaves_remove(album_id, *args, **kwargs))

class AsyncDisclaimersService(_AsyncBase):

    async def track(self, track_id, *args, **kwargs):
        return await self._read(lambda: self._c.tracks_disclaimer(track_id, *args, **kwargs))

    async def album(self, album_id, *args, **kwargs):
        return await self._read(lambda: self._c.albums_disclaimer(album_id, *args, **kwargs))

    async def artist(self, artist_id, *args, **kwargs):
        return await self._read(lambda: self._c.artists_disclaimer(artist_id, *args, **kwargs))

    async def clip(self, clip_id, *args, **kwargs):
        return await self._read(lambda: self._c.clips_disclaimer(clip_id, *args, **kwargs))

class AsyncDeviceAuthService(_AsyncBase):

    async def request_code(self, *args, **kwargs):
        return await self._write(lambda: self._c.request_device_code(*args, **kwargs))

    async def poll_token(self, *args, **kwargs):
        return await self._write(lambda: self._c.poll_device_token(*args, **kwargs))

    async def device_auth(self, *args, **kwargs):
        return await self._write(lambda: self._c.device_auth(*args, **kwargs))

class AsyncGenericService:
    """Async escape hatch."""

    def __init__(self, client: Any, config: SDKConfig | None=None):
        self._c = client
        self._config = config

    def __getattr__(self, name: str):
        attr = getattr(self._c, name)
        if callable(attr):

            async def wrapper(*args, **kwargs):
                with map_errors():
                    result = attr(*args, **kwargs)
                    if hasattr(result, '__await__'):
                        return await result
                    return result
            return wrapper
        return attr

# ==================== SDK: remote.py (async-only) ====================
"""Ynison remote facade.

Не дублируем внутренний protobuf/WebSocket протокол. Используем реализацию
upstream yandex-music, потому что именно она следит за изменениями wire format.

Два режима (как в upstream):
- `remote_session()` — постоянное соединение: snapshot + команды без
  переподключения. Для серий команд и подписки на изменения.
- one-shot через `yandex_music.ynison.simple / simple_async` — открыть,
  выполнить одно действие, закрыть. Идеально для CLI/скриптов.
"""
import inspect
from collections.abc import Callable
from typing import Any

def _get(obj: Any, name: str, default=None):
    return getattr(obj, name, default) if obj is not None else default

def _track_text(playable: Any) -> tuple[str | None, str | None, str | None]:
    """Вернуть (track_id, title, artist) из Playable разных версий upstream."""
    if playable is None:
        return (None, None, None)
    track_id = _get(playable, 'track_id') or _get(playable, 'id')
    title = _get(playable, 'title')
    artist = None
    artists = _get(playable, 'artists') or []
    try:
        if artists:
            names = [a for a in (_get(x, 'title') or _get(x, 'name') for x in artists) if a]
            artist = ', '.join(names) if names else None
    except Exception:
        artist = None
    if track_id is not None:
        track_id = str(track_id)
    return (track_id, title, artist)

def snapshot_from_state(state: Any) -> Playback:
    """Преобразовать внутреннее состояние Ynison в стабильный DTO."""
    if state is None:
        return Playback(active_device_id=None, paused=None, progress_ms=None, duration_ms=None, track_id=None, track_title=None, artist_title=None, devices=(), raw=None)
    player = _get(state, 'player_state')
    status = _get(player, 'status')
    queue = _get(player, 'player_queue')
    items = _get(queue, 'playable_list', []) or []
    index = _get(queue, 'current_playable_index', -1)
    current = items[index] if isinstance(index, int) and 0 <= index < len(items) else None
    if current is None:
        try:
            from yandex_music.ynison import utils as _ynison_utils
            current = _ynison_utils.get_current_playable(state)
        except Exception:
            current = None
    track_id, title, artist = _track_text(current)
    active_id = _get(state, 'active_device_id_optional')
    if active_id is None:
        try:
            from yandex_music.ynison import utils as _ynison_utils
            active = _ynison_utils.get_active_device(state)
            active_id = _get(active, 'id') or _get(_get(active, 'info'), 'device_id')
        except Exception:
            active_id = None
    devices: list[Device] = []
    for raw_device in _get(state, 'devices', []) or []:
        info = _get(raw_device, 'info', raw_device)
        did = _get(raw_device, 'id') or _get(info, 'device_id')
        if did:
            devices.append(Device(id=str(did), title=_get(info, 'title'), device_type=_get(info, 'type'), active=str(did) == str(active_id) if active_id is not None else False, raw=raw_device))
    progress = _get(status, 'progress_ms')
    duration = _get(status, 'duration_ms')
    return Playback(active_device_id=str(active_id) if active_id is not None else None, paused=_get(status, 'paused'), progress_ms=int(progress) if progress is not None else None, duration_ms=int(duration) if duration is not None else None, track_id=track_id, track_title=title, artist_title=artist, devices=tuple(devices), raw=state)

def _require_ynison():
    try:
        import yandex_music.ynison
    except ImportError as exc:
        raise UnsupportedOperation('Ynison extras not installed. Install with: pip install -e ".[all]" or pip install "yandex-music[ynison]".') from exc

class AsyncRemotePlayer:
    """Асинхронный remote controller поверх YnisonClientAsync."""

    def __init__(self, upstream: Any):
        self._upstream = upstream
        self._dto_listeners: list[Callable[[Playback], Any]] = []

    @property
    def upstream(self) -> Any:
        return self._upstream

    @property
    def device_id(self):
        return _get(self._upstream, 'device_id')

    @property
    def is_running(self) -> bool:
        return bool(_get(self._upstream, 'is_running', True))

    async def snapshot(self) -> Playback:
        with map_errors(remote=True):
            state = getattr(self._upstream, 'state', None)
            if callable(state):
                try:
                    state = state()
                except Exception:
                    state = None
            if inspect.isawaitable(state):
                state = await state
            if state is None:
                state = getattr(self._upstream, 'latest_state', None)
                if callable(state):
                    state = state()
                if inspect.isawaitable(state):
                    state = await state
            if state is None:
                raise RemoteConnectionError('No Ynison state yet: client not connected.')
            return snapshot_from_state(state)

    @property
    def active_device(self):
        return _get(self._upstream, 'active_device')

    @property
    def current_track(self):
        return _get(self._upstream, 'current_playable')

    def on_state(self, callback: Callable[[Playback], Any]):

        async def _wrapper(state: Any):
            try:
                snapshot = snapshot_from_state(state)
            except Exception:
                return
            for listener in list(self._dto_listeners):
                try:
                    res = listener(snapshot)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    pass
            try:
                res = callback(snapshot)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass
        self._dto_listeners.append(callback)
        upstream_on = getattr(self._upstream, 'on_state', None)
        if callable(upstream_on):
            upstream_on(_wrapper)
        return callback

    def on_raw_state(self, callback: Callable[[Any], Any]):
        upstream_on = getattr(self._upstream, 'on_state', None)
        if not callable(upstream_on):
            raise UnsupportedOperation('Upstream client has no on_state().')
        return upstream_on(callback)

    def on_error(self, callback: Callable[[Exception], Any]):
        upstream_on = getattr(self._upstream, 'on_error', None)
        if not callable(upstream_on):
            raise UnsupportedOperation('Upstream client has no on_error().')
        return upstream_on(callback)

    async def send(self, request: Any):
        with map_errors(remote=True):
            result = self._upstream.send(request)
            if inspect.isawaitable(result):
                return await result
            return result

    async def pause(self):
        return await self._command('pause')

    async def resume(self):
        return await self._command('resume')

    async def toggle(self):
        state = await self.snapshot()
        return await self.pause() if state.paused is False else await self.resume()

    async def next(self):
        return await self._command('next_track')

    async def previous(self):
        return await self._command('previous_track')

    async def set_volume(self, value: float, target_device_id: str | None=None):
        volume = clamp_volume(value)
        with map_errors(remote=True):
            method = getattr(self._upstream, 'set_volume', None)
            if method is None:
                raise UnsupportedOperation('Ynison command is unavailable: set_volume')
            try:
                result = method(volume, target_device_id)
            except TypeError:
                result = method(volume)
            if inspect.isawaitable(result):
                return await result
            return result

    async def select_device(self, device_id: str):
        for name in ('set_active_device', 'select_device', 'set_device'):
            method = getattr(self._upstream, name, None)
            if method:
                with map_errors(remote=True):
                    result = method(device_id)
                    return await result if inspect.isawaitable(result) else result
        raise UnsupportedOperation('Current yandex-music upstream does not expose a device-selection method.')

    async def _command(self, name: str, *args):
        method = getattr(self._upstream, name, None)
        if method is None:
            raise UnsupportedOperation(f'Ynison command is unavailable: {name}')
        with map_errors(remote=True):
            result = method(*args)
            return await result if inspect.isawaitable(result) else result

def _simple_async():
    _require_ynison()
    from yandex_music.ynison import simple_async as _simple_async_mod
    return _simple_async_mod

async def aonshot_state(token: str, device_id: str | None=None, timeout: float=10.0) -> Playback:
    with map_errors(remote=True):
        state = await _simple_async().get_state(token, device_id=device_id, timeout=timeout)
        return snapshot_from_state(state)

async def aonshot_pause(token: str, device_id: str | None=None, timeout: float=10.0) -> None:
    with map_errors(remote=True):
        await _simple_async().pause(token, device_id=device_id, timeout=timeout)

async def aonshot_resume(token: str, device_id: str | None=None, timeout: float=10.0) -> None:
    with map_errors(remote=True):
        await _simple_async().resume(token, device_id=device_id, timeout=timeout)

async def aonshot_next(token: str, device_id: str | None=None, timeout: float=10.0) -> None:
    with map_errors(remote=True):
        await _simple_async().next_track(token, device_id=device_id, timeout=timeout)

async def aonshot_prev(token: str, device_id: str | None=None, timeout: float=10.0) -> None:
    with map_errors(remote=True):
        await _simple_async().previous_track(token, device_id=device_id, timeout=timeout)

async def aonshot_volume(token: str, volume: float, target_device_id: str | None=None, device_id: str | None=None, timeout: float=10.0) -> None:
    volume = clamp_volume(volume)
    with map_errors(remote=True):
        await _simple_async().set_volume(token, volume, target_device_id=target_device_id, device_id=device_id, timeout=timeout)

# ==================== SDK: client.py (async-only) ====================
"""Главные публичные клиенты SDK."""
from contextlib import contextmanager, asynccontextmanager
from collections.abc import Iterator, AsyncIterator
from typing import Any

def _build_config(token: str | None, config: SDKConfig | None) -> SDKConfig:
    resolved = resolve_token(token, config.token if config else None)
    if config is None:
        cfg = SDKConfig(token=resolved)
    else:
        cfg = config
        if resolved:
            cfg.token = resolved
    setup_logging(cfg.debug)
    return cfg

class _AsyncRemoteAccessor:

    def __init__(self, client: AsyncYandexMusic):
        self._client = client

    @asynccontextmanager
    async def session(self, timeout: float | None=None) -> AsyncIterator[AsyncRemotePlayer]:
        async with self._client.remote_session(timeout=timeout) as remote:
            yield remote
    context = session
    session_context = session

    async def state(self, timeout: float | None=None):
        return await self._client.remote_state(timeout=timeout)

    async def pause(self, timeout: float | None=None):
        return await self._client.remote_pause(timeout=timeout)

    async def resume(self, timeout: float | None=None):
        return await self._client.remote_resume(timeout=timeout)

    async def toggle(self, timeout: float | None=None):
        return await self._client.remote_toggle(timeout=timeout)

    async def next(self, timeout: float | None=None):
        return await self._client.remote_next(timeout=timeout)

    async def previous(self, timeout: float | None=None):
        return await self._client.remote_prev(timeout=timeout)

    async def set_volume(self, volume: float, target_device_id: str | None=None, timeout: float | None=None):
        return await self._client.remote_set_volume(volume, target_device_id, timeout)

class AsyncYandexMusic:
    """Главный async facade."""

    def __init__(self, token: str | None=None, *, config: SDKConfig | None=None, **kwargs):
        self.config = _build_config(token, config)
        from yandex_music import ClientAsync
        self.raw = ClientAsync(self.config.token, **{**self.config.client_kwargs(), **kwargs})
        self.raw_async = self.raw
        self.account = AsyncAccountService(self.raw, self.config)
        self.search = AsyncSearchService(self.raw, self.config)
        self.tracks = AsyncTrackService(self.raw, self.config)
        self.artists = AsyncArtistService(self.raw, self.config)
        self.albums = AsyncAlbumService(self.raw, self.config)
        self.playlists = AsyncPlaylistService(self.raw, self.config)
        self.likes = AsyncLikesService(self.raw, self.config)
        self.queue = AsyncQueueService(self.raw, self.config)
        self.radio = AsyncRadioService(self.raw, self.config)
        self.rotor = AsyncRotorSessionService(self.raw, self.config)
        self.wave = AsyncWaveService(self.raw, self.config)
        self.history = AsyncHistoryService(self.raw, self.config)
        self.landing = AsyncLandingService(self.raw, self.config)
        self.clips = AsyncClipsService(self.raw, self.config)
        self.concerts = AsyncConcertsService(self.raw, self.config)
        self.credits = AsyncCreditsService(self.raw, self.config)
        self.labels = AsyncLabelsService(self.raw, self.config)
        self.metatags = AsyncMetatagsService(self.raw, self.config)
        self.pins = AsyncPinsService(self.raw, self.config)
        self.presaves = AsyncPresavesService(self.raw, self.config)
        self.disclaimers = AsyncDisclaimersService(self.raw, self.config)
        self.device_auth = AsyncDeviceAuthService(self.raw, self.config)
        self.api = AsyncGenericService(self.raw, self.config)
        self.remote = _AsyncRemoteAccessor(self)
        self._initialized = False

    async def init(self, *args, **kwargs):
        with map_errors():
            await self.raw.init(*args, **kwargs)
        self._initialized = True
        return self

    async def health(self) -> bool:
        try:
            with map_errors():
                await self.raw.account_status()
            return True
        except Exception:
            return False

    @property
    def me(self) -> Any:
        return getattr(self.raw, 'me', None)

    @property
    def token(self) -> str | None:
        return self.config.token

    @asynccontextmanager
    async def remote_session(self, timeout: float | None=None) -> AsyncIterator[AsyncRemotePlayer]:
        from yandex_music.ynison import YnisonClientAsync
        token = self.config.validate_token()
        ynison_kwargs = self.config.ynison_kwargs()
        wait = self.config.timeout if timeout is None else timeout
        async with YnisonClientAsync(token, **ynison_kwargs).session(timeout=wait) as upstream:
            yield AsyncRemotePlayer(upstream)
    remote_context = remote_session
    remote_session_context = remote_session

    def _one_shot_timeout(self, timeout: float | None) -> float:
        return self.config.timeout if timeout is None else timeout

    async def remote_state(self, timeout: float | None=None):
        return await aonshot_state(self.config.validate_token(), device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def remote_pause(self, timeout: float | None=None):
        return await aonshot_pause(self.config.validate_token(), device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def remote_resume(self, timeout: float | None=None):
        return await aonshot_resume(self.config.validate_token(), device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def remote_toggle(self, timeout: float | None=None):
        snap = await self.remote_state(timeout=timeout)
        if snap.paused is False:
            return await self.remote_pause(timeout=timeout)
        return await self.remote_resume(timeout=timeout)

    async def remote_next(self, timeout: float | None=None):
        return await aonshot_next(self.config.validate_token(), device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def remote_prev(self, timeout: float | None=None):
        return await aonshot_prev(self.config.validate_token(), device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def remote_set_volume(self, volume: float, target_device_id: str | None=None, timeout: float | None=None):
        return await aonshot_volume(self.config.validate_token(), volume, target_device_id=target_device_id, device_id=self.config.device_id, timeout=self._one_shot_timeout(timeout))

    async def close(self):
        method = getattr(self.raw, 'close', None)
        if callable(method):
            try:
                result = method()
                if hasattr(result, '__await__'):
                    await result
            except Exception:
                pass

    async def __aenter__(self):
        if not self._initialized and (self.config.token or '').strip():
            await self.init()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False
__all__ = ['YandexMusic', 'AsyncYandexMusic']

# ============================ MODULE YanMusicMod (Heroku/Hikka, async-only, Spotify-style) ============================
# Механизм как в YandexMusicMod (async SDK + Ynison one-shot), оформление 1:1 как в SpotifyMod:
# Banners (horizontal/vertical/ultra), custom_text, yt-dlp скачивание, инлайн-поиск, плейлисты/лайки.

AUTH_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token"
    "&client_id=23cabbbdc6cd418abb4b39c32c41195d"
)
GIT_MAIN = "git+https://github.com/MarshalX/yandex-music-api.git"


def _has_ynison() -> bool:
    try:
        import yandex_music.ynison  # noqa: F401
        return True
    except ImportError:
        return False


async def _pip(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-q", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode()[-500:])


async def _ensure_ynison() -> tuple:
    """PyPI 3.0.0 НЕ содержит ynison (только main) — ставим с гитхаба."""
    if _has_ynison():
        return True, ""
    try:
        await _pip(GIT_MAIN)
        await _pip("websockets", "betterproto")
    except Exception as e:
        return False, f"pip упал: {e}"
    if _has_ynison():
        return True, ""
    return False, "restart"


class Banners:
    def __init__(
        self,
        title: str,
        artists: list,
        duration: int,
        progress: int,
        track_cover: bytes,
        font,
        blur,
        album_title: str = "",
        meta_info: str = "",
    ):
        self.title = title
        self.artists = ", ".join(artists) if isinstance(artists, list) else artists
        self.duration = duration
        self.progress = progress
        self.track_cover = track_cover
        self.font_url = font
        self.blur_intensity = blur
        self.album_title = album_title
        self.meta_info = meta_info

    def _get_font(self, size, font_bytes):
        return ImageFont.truetype(io.BytesIO(font_bytes), size)

    def _prepare_cover(self, size, radius):
        cover = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        cover = cover.resize((size, size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(cover, (0, 0), mask=mask)
        return output

    def _prepare_background(self, w, h):
        bg = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        bg = bg.resize((w, h), Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=self.blur_intensity))
        bg = ImageEnhance.Brightness(bg).enhance(0.35)
        return bg

    def _draw_progress_bar(self, draw, x, y, w, h, progress_pct, color="white", bg_color="#6b6b6b"):
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=bg_color)
        fill_w = int(w * progress_pct)
        if fill_w > 0:
            draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=h / 2, fill=color)

    def horizontal(self):
        W, H = 1500, 600
        padding = 60
        cover_size = 480
        font_bytes = requests.get(self.font_url).content
        title_font = self._get_font(55, font_bytes)
        artist_font = self._get_font(45, font_bytes)
        time_font = self._get_font(25, font_bytes)
        img = self._prepare_background(W, H)
        draw = ImageDraw.Draw(img)
        cover = self._prepare_cover(cover_size, 30)
        img.paste(cover, (padding, (H - cover_size) // 2), cover)
        text_x = padding + cover_size + 60
        text_y_start = 100
        text_width_limit = W - text_x - padding
        wrapper = textwrap.TextWrapper(width=23)
        title_lines = wrapper.wrap(self.title)
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            title_lines[-1] += "..."
        current_y = text_y_start
        title_height = title_font.getbbox("Ah")[3] + 15
        for line in title_lines:
            draw.text((text_x, current_y), line, font=title_font, fill="white")
            current_y += title_height
        display_artist = self.artists
        while artist_font.getlength(display_artist) > text_width_limit and len(display_artist) > 0:
            display_artist = display_artist[:-1]
        if len(display_artist) < len(self.artists):
            display_artist += "…"
        artist_y = current_y + 10
        draw.text((text_x, artist_y), display_artist, font=artist_font, fill="#b3b3b3")
        cur_time = f"{(self.progress // 1000 // 60):02}:{(self.progress // 1000 % 60):02}"
        dur_time = f"{(self.duration // 1000 // 60):02}:{(self.duration // 1000 % 60):02}"
        cur_w = time_font.getlength(cur_time)
        dur_w = time_font.getlength(dur_time)
        bar_y = 480
        bar_h = 8
        gap = 25
        draw.text((text_x, bar_y - 12), cur_time, font=time_font, fill="white")
        bar_start_x = text_x + cur_w + gap
        bar_end_x = text_x + text_width_limit - dur_w - gap
        bar_w = bar_end_x - bar_start_x
        prog_pct = self.progress / self.duration if self.duration > 0 else 0
        self._draw_progress_bar(draw, bar_start_x, bar_y, bar_w, bar_h, prog_pct)
        draw.text((bar_end_x + gap, bar_y - 12), dur_time, font=time_font, fill="white")
        by = io.BytesIO()
        img.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by

    def vertical(self):
        W, H = 1000, 1500
        padding = 80
        cover_size = 800
        font_bytes = requests.get(self.font_url).content
        title_font = self._get_font(60, font_bytes)
        artist_font = self._get_font(45, font_bytes)
        time_font = self._get_font(35, font_bytes)
        img = self._prepare_background(W, H)
        draw = ImageDraw.Draw(img)
        cover = self._prepare_cover(cover_size, 40)
        cover_x = (W - cover_size) // 2
        cover_y = 120
        img.paste(cover, (cover_x, cover_y), cover)
        text_area_y = cover_y + cover_size + 120
        text_width_limit = W - (padding * 2)
        wrapper = textwrap.TextWrapper(width=23)
        title_lines = wrapper.wrap(self.title)
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            title_lines[-1] += "..."
        current_y = text_area_y
        title_height = title_font.getbbox("Ah")[3] + 15
        for line in title_lines:
            w = title_font.getlength(line)
            draw.text(((W - w) / 2, current_y), line, font=title_font, fill="white")
            current_y += title_height
        display_artist = self.artists
        while artist_font.getlength(display_artist) > text_width_limit and len(display_artist) > 0:
            display_artist = display_artist[:-1]
        if len(display_artist) < len(self.artists):
            display_artist += "…"
        artist_w = artist_font.getlength(display_artist)
        draw.text(((W - artist_w) / 2, current_y + 15), display_artist, font=artist_font, fill="#b3b3b3")
        bar_y = text_area_y + 260
        if len(title_lines) > 1:
            bar_y += 60
        bar_h = 8
        bar_w = W - (padding * 2)
        prog_pct = self.progress / self.duration if self.duration > 0 else 0
        self._draw_progress_bar(draw, padding, bar_y, bar_w, bar_h, prog_pct, color="white", bg_color="#6b6b6b")
        cur_time = f"{(self.progress // 1000 // 60):02}:{(self.progress // 1000 % 60):02}"
        dur_time = f"{(self.duration // 1000 // 60):02}:{(self.duration // 1000 % 60):02}"
        draw.text((padding, bar_y + 40), cur_time, font=time_font, fill="white")
        dur_w = time_font.getlength(dur_time)
        draw.text((W - padding - dur_w, bar_y + 40), dur_time, font=time_font, fill="white")
        by = io.BytesIO()
        img.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by

    def ultra(self) -> io.BytesIO:
        WIDTH, HEIGHT = 2560, 1220
        font_bytes = requests.get(self.font_url).content

        def get_font(size):
            try:
                return ImageFont.truetype(io.BytesIO(font_bytes), size)
            except Exception:
                return ImageFont.load_default()

        try:
            original_cover = Image.open(io.BytesIO(self.track_cover)).convert("RGBA")
        except Exception:
            original_cover = Image.new("RGBA", (1000, 1000), "black")
        dominant_color_img = original_cover.resize((1, 1), Image.Resampling.LANCZOS)
        dominant_color = dominant_color_img.getpixel((0, 0))
        r, g, b, a = dominant_color
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        if brightness < 60:
            r = min(255, r + 60)
            g = min(255, g + 60)
            b = min(255, b + 60)
            dominant_color = (r, g, b, 255)
        background = original_cover.copy()
        bg_w, bg_h = background.size
        target_ratio = WIDTH / HEIGHT
        current_ratio = bg_w / bg_h
        if current_ratio > target_ratio:
            new_w = int(bg_h * target_ratio)
            offset = (bg_w - new_w) // 2
            background = background.crop((offset, 0, offset + new_w, bg_h))
        else:
            new_h = int(bg_w / target_ratio)
            offset = (bg_h - new_h) // 2
            background = background.crop((0, offset, bg_w, offset + new_h))
        background = background.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        if self.blur_intensity > 0:
            background = background.filter(ImageFilter.GaussianBlur(radius=self.blur_intensity))
        dark_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 180))
        background = Image.alpha_composite(background, dark_overlay)
        cover_size = 500
        cover_x = (WIDTH - cover_size) // 2
        cover_y = 160
        glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw_glow = ImageDraw.Draw(glow_layer)
        glow_rect_size = 620
        g_x = (WIDTH - glow_rect_size) // 2
        g_y = cover_y + (cover_size - glow_rect_size) // 2
        draw_glow.rounded_rectangle(
            (g_x, g_y, g_x + glow_rect_size, g_y + glow_rect_size),
            radius=50,
            fill=dominant_color,
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=60))
        glow_layer = ImageEnhance.Brightness(glow_layer).enhance(1.4)
        glow_layer = ImageEnhance.Color(glow_layer).enhance(1.2)
        background = Image.alpha_composite(background, glow_layer)
        cover_img = original_cover.resize((cover_size, cover_size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (cover_size, cover_size), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.rounded_rectangle((0, 0, cover_size, cover_size), radius=45, fill=255)
        background.paste(cover_img, (cover_x, cover_y), mask)
        draw = ImageDraw.Draw(background)
        center_x = WIDTH // 2
        current_y = cover_y + cover_size + 130

        def draw_text_shadow(text, pos, font, fill="white", anchor="ms"):
            x, y = pos
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 240), anchor=anchor)
            draw.text((x, y), text, font=font, fill=fill, anchor=anchor)

        font_title = get_font(100)
        title_text = self.title if len(self.title) <= 30 else self.title[:30] + "..."
        draw_text_shadow(title_text.upper(), (center_x, current_y), font_title)
        current_y += 85
        font_artist = get_font(65)
        artist_text = self.artists if len(self.artists) <= 45 else self.artists[:45] + "..."
        draw_text_shadow(artist_text.upper(), (center_x, current_y), font_artist, fill=(255, 255, 255, 240))
        current_y += 80
        bar_width = 800
        font_time = get_font(40)
        bar_start_x = center_x - (bar_width // 2)
        bar_end_x = center_x + (bar_width // 2)
        bar_y = current_y
        total_time_str = f"{self.duration // 1000 // 60:02d}:{(self.duration // 1000) % 60:02d}"
        cur_time_str = f"{self.progress // 1000 // 60:02d}:{(self.progress // 1000) % 60:02d}"
        draw_text_shadow(cur_time_str, (bar_start_x - 30, bar_y), font_time, anchor="rm")
        draw_text_shadow(total_time_str, (bar_end_x + 30, bar_y), font_time, anchor="lm")
        old_state = random.getstate()
        random.seed(self.title + str(self.duration))
        num_bars = 65
        bar_spacing = bar_width / num_bars
        bar_w = max(4, int(bar_spacing * 0.5))
        max_h, min_h = 50, 6
        active_bars = int(num_bars * (self.progress / self.duration)) if self.duration > 0 else 0
        for i in range(num_bars):
            base_h = random.randint(min_h, max_h)
            edge_factor = 1.0 - abs((i - num_bars / 2) / (num_bars / 2))
            h = max(min_h, int(base_h * 0.4 + max_h * edge_factor * 0.6))
            x_center = bar_start_x + i * bar_spacing
            color = (255, 255, 255, 255) if i < active_bars else (80, 80, 80, 100)
            draw.rounded_rectangle(
                (x_center - bar_w / 2, bar_y - h / 2, x_center + bar_w / 2, bar_y + h / 2),
                radius=int(bar_w / 2),
                fill=color,
            )
        random.setstate(old_state)
        current_y += 80
        if self.album_title:
            font_album = get_font(50)
            album_text = self.album_title if len(self.album_title) <= 50 else self.album_title[:50] + "..."
            draw_text_shadow(album_text, (center_x, current_y), font_album, fill=(230, 230, 230))
            current_y += 60
        if self.meta_info:
            font_meta = get_font(40)
            draw_text_shadow(self.meta_info, (center_x, current_y), font_meta, fill=(210, 210, 210))
        by = io.BytesIO()
        background.save(by, format="PNG")
        by.seek(0)
        by.name = "banner.png"
        return by


@loader.tds
class YanMusicMod(loader.Module):
    """Card with the currently playing track on Yandex Music."""

    strings = {
        "name": "YanMusic",
        "need_auth": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please execute"
            " </b><code>.ymauth</code><b> before performing this action.</b>"
        ),
        "skipped": (
            "<tg-emoji emoji-id=6037622221625626773>➡️</tg-emoji> <b>Skipped track.</b>"
        ),
        "playing": "<tg-emoji emoji-id=5773626993010546707>▶️</tg-emoji> <b>Playing...</b>",
        "back": (
            "<tg-emoji emoji-id=6039539366177541657>⬅️</tg-emoji> <b>Switched to previous"
            " track</b>"
        ),
        "paused": "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Pause</b>",
        "toggled": "<tg-emoji emoji-id=5843596438373667352>✅️</tg-emoji> <b>Toggled.</b>",
        "liked": (
            "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Liked current"
            " playback</b>"
        ),
        "unlike": (
            "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji>"
            " <b>Unliked current playback</b>"
        ),
        "err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>An error occurred."
            "</b>\n<code>{}</code>"
        ),
        "already_authed": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Already authorized</b>"
        ),
        "authed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Authentication"
            " successful</b>"
        ),
        "deauth": (
            "<tg-emoji emoji-id=5877341274863832725>🚪</tg-emoji> <b>Successfully logged out"
            " of account</b>"
        ),
        "auth": (
            "🔑 <b>Yandex Music authorization</b>\n\n"
            "1. Open the link and allow access:\n"
            f"{AUTH_URL}\n\n"
            "2. You will land on <code>music.yandex.ru/#access_token=...</code> — "
            "copy the part after <code>access_token=</code> up to <code>&</code>\n"
            "3. Save: <code>.ymtoken &lt;token&gt;</code>\n\n"
            "Or automatically: <code>.ymcode</code>"
        ),
        "no_music": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No music is playing!</b>"
        ),
        "no_device": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No active device — start music in the app.</b>"
        ),
        "queue_edge": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Queue boundary (first/last track).</b>"
        ),
        "dl_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Failed to download"
            " track.</b>"
        ),
        "volume_changed": (
            "<tg-emoji emoji-id=5890997763331591703>🔊</tg-emoji>"
            " <b>Volume changed to {}%.</b>"
        ),
        "volume_invalid": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Volume level must be"
            " a number between 0 and 100.</b>"
        ),
        "volume_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>An error occurred while"
            " changing volume.</b>"
        ),
        "no_volume_arg": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a"
            " volume level between 0 and 100.</b>"
        ),
        "searching_tracks": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Searching for tracks"
            " matching {}...</b>"
        ),
        "no_search_query": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a"
            " search query.</b>"
        ),
        "no_tracks_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No tracks found for"
            " {}.</b>"
        ),
        "search_results": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Search results for"
            " {}:</b>\n\n{}"
        ),
        "search_results_inline": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Found {count} results"
            " for {query}.</b>\n<b>Select a track:</b>"
        ),
        "downloading_search_track": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Downloading {}...</b>"
        ),
        "download_success": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Successfully downloaded {} - {}</b>"
        ),
        "invalid_track_number": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid track number."
            " Please search first or provide a valid number from the list.</b>"
        ),
        "device_list": (
            "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Available devices:</b>\n{}"
        ),
        "no_devices_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No devices found.</b>"
        ),
        "device_transfer_na": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Device switching is not exposed by upstream Ynison — control playback on the device itself.</b>"
        ),
        "invalid_device_id": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid device ID."
            " Use</b> <code>.ymdev</code> <b>to see available devices.</b>"
        ),
        "no_ytdlp": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>yt-dlp not found... Check config or install yt-dlp (<code>{}terminal pip install yt-dlp</code>)</b>",
        "snowt_failed": "\n\n<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Download failed</b>",
        "uploading_banner": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Uploading banner...</i>",
        "downloading_track": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Downloading track...</i>",
        "no_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>No playlists found.</b>",
        "playlists_list": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Your playlists:</b>\n\n{}",
        "added_to_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Added {} to {}</b>",
        "removed_from_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Removed {} from {}</b>",
        "invalid_playlist_index": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Invalid playlist number.</b>",
        "no_cached_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Use .ymplaylists first.</b>",
        "playlist_created": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Playlist {} created.</b>",
        "playlist_deleted": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Playlist {} deleted.</b>",
        "no_playlist_name": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Please specify a playlist name.</b>",
        "account": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Account:</b> <b>{}</b> <code>{}</code>",
        "likes_count": "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Liked tracks: {}</b>",
        "ynison_need_restart": "⏳ Ynison delivered, restart needed: <code>.restart</code> — then repeat the command.",
    }

    strings_ru = {
        "_cls_doc": "Карточка с играющим треком в Яндекс Музыке.",
        "need_auth": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Выполни"
            " </b><code>.ymauth</code><b> перед выполнением этого действия.</b>"
        ),
        "err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Произошла ошибка."
            "</b>\n<code>{}</code>"
        ),
        "skipped": (
            "<tg-emoji emoji-id=6037622221625626773>➡️</tg-emoji> <b>Трек пропущен.</b>"
        ),
        "playing": "<tg-emoji emoji-id=5773626993010546707>▶️</tg-emoji> <b>Играет...</b>",
        "back": (
            "<tg-emoji emoji-id=6039539366177541657>⬅️</tg-emoji> <b>Переключено на предыдущий трек</b>"
        ),
        "paused": "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Пауза</b>",
        "toggled": "<tg-emoji emoji-id=5843596438373667352>✅️</tg-emoji> <b>Переключено.</b>",
        "liked": (
            "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Текущий трек добавлен в избранное</b>"
        ),
        "unlike": (
            "<tg-emoji emoji-id=5774077015388852135>❌</tg-emoji> <b>Убрал лайк с текущего трека</b>"
        ),
        "already_authed": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Уже авторизован</b>"
        ),
        "authed": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Успешная аутентификация</b>"
        ),
        "deauth": (
            "<tg-emoji emoji-id=5877341274863832725>🚪</tg-emoji> <b>Успешный выход из аккаунта</b>"
        ),
        "auth": (
            "🔑 <b>Авторизация Яндекс Музыки</b>\n\n"
            "1. Открой ссылку и разреши доступ:\n"
            f"{AUTH_URL}\n\n"
            "2. Тебя кинет на <code>music.yandex.ru/#access_token=...</code> — "
            "скопируй кусок после <code>access_token=</code> до <code>&</code>\n"
            "3. Сохрани: <code>.ymtoken &lt;токен&gt;</code>\n\n"
            "Либо автоматически: <code>.ymcode</code>"
        ),
        "no_music": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Музыка не играет!</b>"
        ),
        "no_device": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Нет активного устройства — включи музыку в приложении.</b>"
        ),
        "queue_edge": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Граница очереди (первый/последний трек).</b>"
        ),
        "dl_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Не удалось скачать трек.</b>"
        ),
        "volume_changed": (
            "<tg-emoji emoji-id=5890997763331591703>🔊</tg-emoji>"
            " <b>Громкость изменена на {}%.</b>"
        ),
        "volume_invalid": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Уровень громкости должен"
            " быть числом от 0 до 100.</b>"
        ),
        "volume_err": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Произошла ошибка при"
            " изменении громкости.</b>"
        ),
        "no_volume_arg": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите"
            " уровень громкости от 0 до 100.</b>"
        ),
        "searching_tracks": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Идет поиск треков"
            " по запросу {}...</b>"
        ),
        "no_search_query": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите"
            " поисковый запрос.</b>"
        ),
        "no_tracks_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>По запросу '{}'"
            " ничего не найдено.</b>"
        ),
        "search_results": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Результаты поиска"
            " по запросу {}:</b>\n\n{}"
        ),
        "search_results_inline": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Найдено {count} результатов"
            " по запросу {query}.</b>\n<b>Выберите трек:</b>"
        ),
        "downloading_search_track": (
            "<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <b>Скачиваю {}...</b>"
        ),
        "download_success": (
            "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} - {} успешно скачан.</b>"
        ),
        "invalid_track_number": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Некорректный номер трека."
            " Сначала выполните поиск или укажите правильный номер из списка.</b>"
        ),
        "device_list": (
            "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Доступные устройства:</b>\n{}"
        ),
        "no_devices_found": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Устройства не найдены.</b>"
        ),
        "device_transfer_na": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Переключение устройств не поддерживается upstream Ynison — управляй воспроизведением на самом устройстве.</b>"
        ),
        "invalid_device_id": (
            "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Некорректный ID устройства."
            " Используйте</b> <code>.ymdev</code><b>, чтобы увидеть доступные устройства.</b>"
        ),
        "no_ytdlp": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>yt-dlp не найден... Проверьте конфиг или установите yt-dlp (<code>{}terminal pip install yt-dlp</code>)</b>",
        "snowt_failed": "\n\n<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Ошибка скачивания.</b>",
        "uploading_banner": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Загрузка баннера...</i>",
        "downloading_track": "\n\n<tg-emoji emoji-id=5841359499146825803>🕔</tg-emoji> <i>Скачивание трека...</i>",
        "no_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Плейлисты не найдены.</b>",
        "playlists_list": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Ваши плейлисты:</b>\n\n{}",
        "added_to_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} добавлен в {}</b>",
        "removed_from_playlist": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Трек {} удален из {}</b>",
        "invalid_playlist_index": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Неверный номер плейлиста.</b>",
        "no_cached_playlists": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Сначала используйте .ymplaylists.</b>",
        "playlist_created": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Плейлист {} создан.</b>",
        "playlist_deleted": "<tg-emoji emoji-id=5776375003280838798>✅</tg-emoji> <b>Плейлист {} удален.</b>",
        "no_playlist_name": "<tg-emoji emoji-id=5778527486270770928>❌</tg-emoji> <b>Пожалуйста, укажите название плейлиста.</b>",
        "account": "<tg-emoji emoji-id=5956561916573782596>📄</tg-emoji> <b>Аккаунт:</b> <b>{}</b> <code>{}</code>",
        "likes_count": "<tg-emoji emoji-id=5258179403652801593>❤️</tg-emoji> <b>Треков в лайках: {}</b>",
        "ynison_need_restart": "⏳ Ynison доставлен, нужен рестарт: <code>.restart</code> — затем повтори команду.",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "TOKEN",
                "",
                "OAuth-токен (или через .ymtoken / .ymcode)",
                validator=loader.validators.Hidden(loader.validators.String()),
            ),
            loader.ConfigValue(
                "DEVICE_ID",
                "",
                "Ynison device_id (пусто = детерминированный из токена)",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "TIMEOUT",
                20.0,
                "Таймаут Ynison, сек",
                validator=loader.validators.Float(minimum=5.0, maximum=120.0),
            ),
            loader.ConfigValue(
                "show_banner",
                True,
                "Show banner with track info",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "custom_text",
                (
                    "<tg-emoji emoji-id=6007938409857815902>🎧</tg-emoji> <b>Now playing:</b> {track} — {artists}\n"
                    "<tg-emoji emoji-id=5877465816030515018>🔗</tg-emoji> <b><a href='{yandex_url}'>yandex music</a></b>"
                ),
                "Custom text, supports {track}, {artists}, {album}, {progress}, {duration}, {device}, {yandex_url} placeholders.",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "font",
                "https://raw.githubusercontent.com/kamekuro/assets/master/fonts/Onest-Bold.ttf",
                "Custom font. Specify URL to .ttf file",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "ytdlp_path",
                "yt-dlp",
                "Path to ytdlp binary",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "cookies_path",
                "",
                "Path to your cookies for yt-dlp",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "banner_version",
                "horizontal",
                lambda: "Banner version",
                validator=loader.validators.Choice(["horizontal", "vertical", "ultra"]),
            ),
            loader.ConfigValue(
                "blur_intensity",
                40,
                lambda: "Blur intensity",
                validator=loader.validators.Integer(minimum=0),
            ),
        )
        self._ym_store = {}

    async def client_ready(self, client, db):
        self._client = client
        self._db = db

    def tokenized(func) -> FunctionType:
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            self = args[0]
            if not self._token():
                await utils.answer(args[1], self.strings("need_auth"))
                return
            return await func(*args, **kwargs)

        wrapped.__doc__ = func.__doc__
        wrapped.__module__ = func.__module__
        return wrapped

    def error_handler(func) -> FunctionType:
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            self = args[0]
            try:
                return await func(*args, **kwargs)
            except NoActiveDeviceError:
                with contextlib.suppress(Exception):
                    await utils.answer(args[1], self.strings("no_device"))
            except QueueBoundaryError:
                with contextlib.suppress(Exception):
                    await utils.answer(args[1], self.strings("queue_edge"))
            except Exception as e:
                error_msg = str(e)
                if "NO_ACTIVE_DEVICE" in error_msg or "NoActiveDevice" in type(e).__name__:
                    with contextlib.suppress(Exception):
                        await utils.answer(args[1], self.strings("no_device"))
                    return
                if "QueueBoundary" in type(e).__name__:
                    with contextlib.suppress(Exception):
                        await utils.answer(args[1], self.strings("queue_edge"))
                    return
                user_error = f"{type(e).__name__}: {error_msg[:200]}"
                with contextlib.suppress(Exception):
                    await utils.answer(args[1], self.strings("err").format(utils.escape_html(user_error)))

        wrapped.__doc__ = func.__doc__
        wrapped.__module__ = func.__module__
        return wrapped

    # ---------- base helpers ----------
    def _token(self):
        t = (self.config["TOKEN"] or "").strip() or (self.get("token") or "").strip()
        return t or None

    def _cfg(self) -> SDKConfig:
        try:
            timeout = float(self.config["TIMEOUT"])
        except Exception:
            timeout = 20.0
        dev = (self.config["DEVICE_ID"] or "").strip() or None
        return SDKConfig(token=self._token(), timeout=timeout, device_id=dev)

    def _rest(self) -> AsyncYandexMusic:
        token = self._token()
        if not token:
            raise AuthenticationError("no token")
        return AsyncYandexMusic(token=token)

    async def _need_remote(self, message: Message):
        if not self._token():
            await utils.answer(message, self.strings("need_auth"))
            return None
        ok, hint = await _ensure_ynison()
        if not ok:
            if hint == "restart":
                await utils.answer(message, self.strings("ynison_need_restart"))
            else:
                await utils.answer(message, self.strings("err").format(utils.escape_html(hint)))
            return None
        return self._cfg()

    def _get_chat_id(self, target):
        if isinstance(target, int):
            return target
        if not target:
            return None
        chat_id = getattr(target, "chat_id", None)
        if chat_id:
            return chat_id
        with contextlib.suppress(Exception):
            return utils.get_chat_id(target)
        return None

    def _reply_id(self, message):
        reply_to_id = getattr(message, "reply_to_msg_id", None)
        if reply_to_id:
            return reply_to_id
        reply_to = getattr(message, "reply_to", None)
        return getattr(reply_to, "reply_to_msg_id", None) if reply_to else None

    def _short_text(self, text: str, limit: int = 60) -> str:
        text = " ".join(str(text).split())
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3] + "..."

    def _track_info(self, track_info) -> tuple:
        if isinstance(track_info, dict):
            track_name = track_info.get("name", "Unknown")
            artists_list = [a.get("name") for a in track_info.get("artists", []) if a.get("name")]
            artists = ", ".join(artists_list) if artists_list else "Unknown Artist"
            return track_name, artists
        if isinstance(track_info, (list, tuple)):
            track_name = track_info[0] if len(track_info) > 0 else "Unknown"
            artists = track_info[1] if len(track_info) > 1 else "Unknown Artist"
            if not artists:
                artists = "Unknown Artist"
            return track_name or "Unknown", artists
        # Yandex upstream Track object
        title = getattr(track_info, "title", None) or getattr(track_info, "name", None) or "Unknown"
        raw_artists = getattr(track_info, "artists", None) or []
        names = []
        for a in raw_artists:
            n = getattr(a, "name", None) or getattr(a, "title", None)
            if n:
                names.append(n)
        artists = ", ".join(names) if names else "Unknown Artist"
        return title, artists

    def _cover_url_from_track(self, track) -> str | None:
        uri = getattr(track, "cover_uri", None)
        if not uri:
            # dict variant (rare)
            if isinstance(track, dict):
                images = (((track.get("album") or {}).get("images")) or [])
                if images:
                    return images[0].get("url")
            return None
        return "https://" + str(uri).replace("%%", "400x400")

    async def _cover_bytes(self, token: str, snap) -> bytes | None:
        """Обложка трека bytes через REST: сначала по id, потом поиском."""
        def _fetch(url: str) -> bytes | None:
            try:
                r = requests.get(url, timeout=15)
                if r.ok and r.content:
                    return r.content
            except Exception:
                pass
            return None

        try:
            async with AsyncYandexMusic(token=token) as ym:
                tid = getattr(snap, "track_id", None)
                if tid:
                    try:
                        tracks = await ym.tracks.get([str(tid)])
                        if tracks:
                            url = self._cover_url_from_track(tracks[0])
                            if url:
                                data = await asyncio.to_thread(_fetch, url)
                                if data:
                                    return data
                    except Exception:
                        pass
                query = " ".join(x for x in (snap.artist_title, snap.track_title) if x)
                if query:
                    try:
                        res = await ym.search.query(query)
                        results = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
                        if results:
                            url = self._cover_url_from_track(results[0])
                            if url:
                                data = await asyncio.to_thread(_fetch, url)
                                if data:
                                    return data
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    async def _placeholder_cover(self) -> bytes:
        def _make():
            img = Image.new("RGBA", (1000, 1000), (20, 20, 25, 255))
            by = io.BytesIO()
            img.save(by, format="PNG")
            return by.getvalue()
        return await asyncio.to_thread(_make)

    def _yandex_url(self, snap, track_name: str = "", artists: str = "") -> str:
        import urllib.parse
        q = " ".join(x for x in (artists, track_name) if x) or " ".join(
            x for x in (getattr(snap, "artist_title", "") or "", getattr(snap, "track_title", "") or "") if x
        )
        return "https://music.yandex.ru/search?text=" + urllib.parse.quote(q or "yandex music")

    async def _card_text(self, snap, album_title: str = "") -> str:
        track = getattr(snap, "track_title", None) or "-"
        artists = getattr(snap, "artist_title", None) or ""
        prog = getattr(snap, "progress_ms", None) or 0
        dur = getattr(snap, "duration_ms", None) or 0
        device = getattr(snap, "active_device_id", None) or ""
        sdata = {
            "track": utils.escape_html(str(track)),
            "artists": utils.escape_html(str(artists)),
            "album": utils.escape_html(str(album_title or "")),
            "duration": f"{dur // 1000 // 60}:{dur // 1000 % 60:02}",
            "progress": f"{prog // 1000 // 60}:{prog // 1000 % 60:02}",
            "device": utils.escape_html(str(device)),
            "yandex_url": self._yandex_url(snap, str(track), str(artists)),
            "songlink": self._yandex_url(snap, str(track), str(artists)),
            "spotify_url": self._yandex_url(snap, str(track), str(artists)),
            "playlist": "",
            "playlist_owner": "",
        }
        try:
            data = await utils.get_placeholders(sdata, self.config["custom_text"])
        except Exception:
            data = sdata
        try:
            return self.config["custom_text"].format(**data)
        except Exception:
            return f"🎧 <b>{sdata['track']}</b> — {sdata['artists']}"

    async def _show_card(self, message: Message, snap, token: str, album_title: str = "") -> None:
        text = await self._card_text(snap, album_title)
        if not self.config["show_banner"]:
            await utils.answer(message, text)
            return
        tmp_msg = await utils.answer(message, text + self.strings("uploading_banner"))
        try:
            cover = await self._cover_bytes(token, snap)
        except Exception:
            cover = None
        if not cover:
            cover = await self._placeholder_cover()
        try:
            banners = Banners(
                title=str(getattr(snap, "track_title", None) or "-"),
                artists=str(getattr(snap, "artist_title", None) or ""),
                duration=int(getattr(snap, "duration_ms", None) or 0),
                progress=int(getattr(snap, "progress_ms", None) or 0),
                track_cover=cover,
                font=self.config["font"],
                blur=int(self.config["blur_intensity"] or 0),
                album_title=str(album_title or ""),
                meta_info="Yandex Music",
            )
            version = self.config["banner_version"]
            if version == "ultra":
                file = await asyncio.to_thread(banners.ultra)
            elif version == "vertical":
                file = await asyncio.to_thread(banners.vertical)
            else:
                file = await asyncio.to_thread(banners.horizontal)
            await utils.answer(tmp_msg, text, file=file)
        except Exception as e:
            await utils.answer(message, text + f"\n<code>{html.escape(str(e)[:200])}</code>")

    async def _album_of_snap(self, token: str, snap) -> str:
        try:
            tid = getattr(snap, "track_id", None)
            if not tid:
                return ""
            async with AsyncYandexMusic(token=token) as ym:
                tracks = await ym.tracks.get([str(tid)])
                if not tracks:
                    return ""
                albums = getattr(tracks[0], "albums", None) or []
                if albums:
                    return str(getattr(albums[0], "title", "") or "")
        except Exception:
            pass
        return ""

    # ---------- yt-dlp download (1:1 SpotifyMod) ----------
    def _ytdlp_bin(self) -> str:
        return (self.config["ytdlp_path"] or "yt-dlp").strip() or "yt-dlp"

    async def _download_track(
        self,
        target,
        query,
        caption=None,
        track_name=None,
        artists=None,
        log_context=None,
        reply_to_id=None,
    ) -> bool:
        import shutil
        dl_dir = os.path.join(os.getcwd(), "yanmusicmod")
        if not os.path.exists(dl_dir):
            os.makedirs(dl_dir, exist_ok=True)
        for f in os.listdir(dl_dir):
            try:
                os.remove(os.path.join(dl_dir, f))
            except Exception:
                pass
        success = False
        if caption is None:
            safe_track = utils.escape_html(track_name or "Unknown")
            safe_artists = utils.escape_html(artists or "Unknown Artist")
            caption = self.strings["download_success"].format(safe_track, safe_artists)

        async def send_text(text: str) -> bool:
            if target is None:
                return False
            if isinstance(target, int):
                await self._client.send_message(target, text, reply_to=reply_to_id)
                return True
            try:
                await utils.answer(target, text)
                return True
            except Exception:
                chat_id = self._get_chat_id(target)
                if chat_id is None:
                    return False
                await self._client.send_message(chat_id, text, reply_to=reply_to_id)
                return True

        async def send_file(file_path: str) -> bool:
            if target is None:
                return False
            if isinstance(target, int):
                await self._client.send_file(target, file_path, caption=caption, reply_to=reply_to_id)
                return True
            try:
                await utils.answer(target, caption, file=file_path)
                return True
            except Exception:
                chat_id = self._get_chat_id(target)
                if chat_id is None:
                    return False
                await self._client.send_file(chat_id, file_path, caption=caption, reply_to=reply_to_id)
                return True

        ybin = self._ytdlp_bin()
        if not shutil.which(ybin) and not os.path.isfile(ybin):
            await send_text(self.strings["no_ytdlp"].format(""))
            return False
        try:
            squery = query.replace('"', "").replace("'", "")
            cookies = self.config["cookies_path"]
            if cookies:
                cmd = (
                    f'{ybin} -x --impersonate="" --cookies {cookies} --audio-format mp3 --add-metadata '
                    f'--audio-quality 0 -o "{dl_dir}/%(title)s [%(id)s].%(ext)s" '
                    f'"ytsearch1:{squery}"'
                )
            else:
                cmd = (
                    f'{ybin} -x --impersonate="" --audio-format mp3 --add-metadata '
                    f'--audio-quality 0 -o "{dl_dir}/%(title)s [%(id)s].%(ext)s" '
                    f'"ytsearch1:{squery}"'
                )
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            files = [f for f in os.listdir(dl_dir) if f.endswith(".mp3")]
            if files:
                first = files[0]
                target_file = os.path.join(dl_dir, first)
                success = await send_file(target_file)
                if not success:
                    await send_text(self.strings["dl_err"])
            else:
                await send_text(self.strings["snowt_failed"])
        except Exception:
            await send_text(self.strings["dl_err"])
        finally:
            if os.path.exists(dl_dir):
                for f in os.listdir(dl_dir):
                    try:
                        os.remove(os.path.join(dl_dir, f))
                    except Exception:
                        pass
        return success

    def _search_keyboard(self, tracks: list, chat_id=None, reply_to_id=None) -> list:
        keyboard = []
        for track in tracks:
            track_name, artists = self._track_info(track)
            label = f"{track_name} — {artists}" if artists else track_name
            keyboard.append(
                [
                    {
                        "text": self._short_text(label),
                        "callback": self._inline_download_track,
                        "args": (track_name, artists, reply_to_id, chat_id),
                    }
                ]
            )
        return keyboard

    async def _inline_download_track(self, call, track_name: str, artists: str, reply_to_id=None, chat_id=None):
        track_name = track_name or "Unknown"
        artists = artists or "Unknown Artist"
        with contextlib.suppress(Exception):
            await call.answer()
        with contextlib.suppress(Exception):
            await call.edit(self.strings["downloading_track"].lstrip(), reply_markup=None)
        target_message = getattr(call, "message", None)
        if reply_to_id is None:
            reply_to_id = self._reply_id(target_message)
        if chat_id is None:
            chat_id = self._get_chat_id(target_message)
        if chat_id is None:
            chat_id = getattr(call, "chat_id", None)
        if chat_id is None:
            chat_id = self._get_chat_id(call)
        if chat_id is None and target_message is None:
            with contextlib.suppress(Exception):
                await call.edit(self.strings["dl_err"], reply_markup=None)
            return
        target = chat_id if chat_id is not None else target_message
        success = await self._download_track(
            target, f"{artists} {track_name}", track_name=track_name, artists=artists,
            log_context=f"{track_name} - {artists}", reply_to_id=reply_to_id,
        )
        if success:
            with contextlib.suppress(Exception):
                await call.delete()
        else:
            with contextlib.suppress(Exception):
                await call.edit(self.strings["dl_err"], reply_markup=None)

    async def _inline_search_tracks(self, query):
        if not self._token():
            return {"title": "Auth required", "description": "Run .ymauth", "message": self.strings["need_auth"]}
        query_text = (query.args or "").strip()
        if not query_text:
            return {"title": "No query", "description": "Provide search query", "message": self.strings["no_search_query"]}
        try:
            async with self._rest() as ym:
                res = await ym.search.query(query_text)
            tracks = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
            tracks = list(tracks)[:5]
        except Exception as e:
            return {"title": "Search error", "description": "Try again", "message": self.strings["err"].format(utils.escape_html(str(e)[:100]))}
        if not tracks:
            return {"title": "No results", "description": self._short_text(query_text, limit=60), "message": self.strings["no_tracks_found"].format(utils.escape_html(query_text))}
        store_id = id(tracks)
        self._ym_store[store_id] = [self._track_info(t) for t in tracks]
        entries = []
        for i, track in enumerate(tracks):
            track_name, artists = self._track_info(track)
            thumb = self._cover_url_from_track(track)
            if thumb:
                thumb = thumb.replace("400x400", "200x200")
            entries.append(
                {
                    "title": self._short_text(track_name, limit=60),
                    "description": self._short_text(artists, limit=60) if artists else "",
                    "message": f"{self.strings['downloading_track'].lstrip()}\n<i>ymdl_{store_id}_{i}</i>",
                    "thumb": thumb,
                }
            )
        return entries

    @loader.inline_handler(ru_doc="<запрос> - поиск треков Yandex Music.")
    async def ymq(self, query):
        """<query> - search Yandex Music track"""
        return await self._inline_search_tracks(query)

    # ---------- auth ----------
    @loader.command(ru_doc="Ссылка для получения токена")
    async def ymauth(self, message: Message):
        """Ссылка для получения токена"""
        await utils.answer(message, self.strings["auth"])

    @loader.command(ru_doc="Вход кодом с сайта (автосохранение токена)")
    async def ymcode(self, message: Message):
        """Вход кодом с сайта (автосохранение токена)"""
        await utils.answer(message, "⏳ Запрашиваю код...")
        box: dict = {}

        def on_code(code):
            box["code"] = code

        try:
            from yandex_music import ClientAsync
            client = ClientAsync()
            task = asyncio.create_task(client.device_auth(on_code=on_code))
            shown = False
            while not task.done():
                await asyncio.sleep(2)
                code = box.get("code")
                if code is not None and not shown:
                    url = getattr(code, "verification_url", "?")
                    ucode = getattr(code, "user_code", "?")
                    await utils.answer(
                        message,
                        f"🔑 Открой <code>{html.escape(str(url))}</code> и введи код: "
                        f"<code>{html.escape(str(ucode))}</code>\n⏳ Жду подтверждения...",
                    )
                    shown = True
            token = await task
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(html.escape(f"{type(e).__name__}: {e}")))
            return
        access = getattr(token, "access_token", None)
        if not access:
            await utils.answer(message, self.strings["err"].format("empty token"))
            return
        self.set("token", access)
        with contextlib.suppress(Exception):
            await message.delete()
        await utils.answer(message, f"{self.strings['authed']}\n<code>.ymstatus</code> — проверить")

    @loader.command(ru_doc="Сохранить токен: .ymtoken <токен>")
    async def ymtoken(self, message: Message):
        """Сохранить токен: .ymtoken <токен или ссылка>"""
        raw = utils.get_args_raw(message).strip()
        if not raw:
            await utils.answer(message, self.strings["no_search_query"].replace("search query", "token"))
            return
        if "access_token=" in raw:
            raw = raw.split("access_token=")[1].split("&")[0].strip()
        self.set("token", raw.strip())
        with contextlib.suppress(Exception):
            await message.delete()
        await utils.answer(message, self.strings["authed"])

    @error_handler
    @loader.command(ru_doc="- Выйти из аккаунта")
    async def ymunauth(self, message: Message):
        """- Log out"""
        self.set("token", None)
        try:
            self.config["TOKEN"] = ""
        except Exception:
            pass
        await utils.answer(message, self.strings["deauth"])

    # ---------- now playing card (Spotify snow analog) ----------
    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymstatus - 🎧 Показать карточку играющего трека", alias="ymn")
    async def ymstatus(self, message: Message):
        """| .ymstatus - 🎧 View current track card."""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        if not getattr(snap, "track_title", None):
            await utils.answer(message, self.strings["no_music"])
            return
        album = await self._album_of_snap(cfg.token, snap)
        await self._show_card(message, snap, cfg.token, album)

    @error_handler
    @tokenized
    @loader.command(ru_doc="Что играет + баннер")
    async def ymcur(self, message: Message):
        """Что играет + баннер"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        if not getattr(snap, "track_title", None):
            await utils.answer(message, self.strings["no_music"])
            return
        album = await self._album_of_snap(cfg.token, snap)
        await self._show_card(message, snap, cfg.token, album)

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymd - 🎧 Скачать играющий трек", alias="ymd")
    async def ymdown(self, message: Message):
        """| .ymd - 🎧 Download current track."""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        track = getattr(snap, "track_title", None) or "Unknown"
        artists = getattr(snap, "artist_title", None) or "Unknown Artist"
        if not getattr(snap, "track_title", None):
            await utils.answer(message, self.strings["no_music"])
            return
        album = await self._album_of_snap(cfg.token, snap)
        text = await self._card_text(snap, album)
        msg = await utils.answer(message, text + self.strings["downloading_track"])
        await self._download_track(msg, f"{artists} {track}", caption=text, track_name=str(track), artists=str(artists))

    # ---------- devices ----------
    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymdev - 🎵 Список устройств")
    async def ymdev(self, message: Message):
        """| .ymdev - 🎵 Devices"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        args = utils.get_args_raw(message).strip()
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        devices = list(getattr(snap, "devices", ()) or [])
        if args:
            await utils.answer(message, self.strings["device_transfer_na"])
            return
        if not devices:
            await utils.answer(message, self.strings["no_devices_found"])
            return
        lines = ""
        for i, d in enumerate(devices):
            title = getattr(d, "title", None) or getattr(d, "id", "?")
            active = "(active)" if getattr(d, "active", False) else ""
            lines += f"<b>{i + 1}.</b> {utils.escape_html(str(title))} {active}\n"
        await utils.answer(message, self.strings["device_list"].format(lines.strip()))

    # ---------- transport ----------
    async def _do(self, coro, message: Message, ok_key: str):
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            await coro(cfg)
            await utils.answer(message, self.strings[ok_key])
        except (NoActiveDeviceError, RemoteError, SDKError) as e:
            name = type(e).__name__
            if "NoActiveDevice" in name or isinstance(e, NoActiveDeviceError):
                await utils.answer(message, self.strings["no_device"])
            elif "QueueBoundary" in name or isinstance(e, QueueBoundaryError):
                await utils.answer(message, self.strings["queue_edge"])
            else:
                await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ⏸ Пауза")
    async def ympause(self, message: Message):
        """- ⏸ Pause"""
        await self._do(lambda cfg: aonshot_pause(cfg.token, cfg.device_id, cfg.timeout), message, "paused")

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ▶️ Продолжить")
    async def ymplay(self, message: Message):
        """- ▶️ Resume"""
        await self._do(lambda cfg: aonshot_resume(cfg.token, cfg.device_id, cfg.timeout), message, "playing")

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ⏯ Пауза/продолжить")
    async def ymtoggle(self, message: Message):
        """- ⏯ Toggle"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
            if snap.paused:
                await aonshot_resume(cfg.token, cfg.device_id, cfg.timeout)
            else:
                await aonshot_pause(cfg.token, cfg.device_id, cfg.timeout)
            await utils.answer(message, self.strings["toggled"])
        except (NoActiveDeviceError, RemoteError, SDKError) as e:
            name = type(e).__name__
            if "NoActiveDevice" in name:
                await utils.answer(message, self.strings["no_device"])
            elif "QueueBoundary" in name:
                await utils.answer(message, self.strings["queue_edge"])
            else:
                await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ⏭ Следующий трек + баннер")
    async def ymnext(self, message: Message):
        """- ⏭ Next track"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            await aonshot_next(cfg.token, cfg.device_id, cfg.timeout)
            await asyncio.sleep(2.5)
            try:
                snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
                album = await self._album_of_snap(cfg.token, snap)
                await self._show_card(message, snap, cfg.token, album)
                return
            except Exception:
                pass
            await utils.answer(message, self.strings["skipped"])
        except (NoActiveDeviceError, RemoteError, SDKError) as e:
            name = type(e).__name__
            if "NoActiveDevice" in name:
                await utils.answer(message, self.strings["no_device"])
            elif "QueueBoundary" in name:
                await utils.answer(message, self.strings["queue_edge"])
            else:
                await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ⏮ Предыдущий трек + баннер")
    async def ymprev(self, message: Message):
        """- ⏮ Previous track"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            await aonshot_prev(cfg.token, cfg.device_id, cfg.timeout)
            await asyncio.sleep(2.5)
            try:
                snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
                album = await self._album_of_snap(cfg.token, snap)
                await self._show_card(message, snap, cfg.token, album)
                return
            except Exception:
                pass
            await utils.answer(message, self.strings["back"])
        except (NoActiveDeviceError, RemoteError, SDKError) as e:
            name = type(e).__name__
            if "NoActiveDevice" in name:
                await utils.answer(message, self.strings["no_device"])
            elif "QueueBoundary" in name:
                await utils.answer(message, self.strings["queue_edge"])
            else:
                await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymvol - 🔊 Громкость 0-100", alias="ymv")
    async def ymvol(self, message: Message):
        """| .ymvol - 🔊 Volume"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        args = utils.get_args_raw(message).strip()
        if args == "":
            await utils.answer(message, self.strings["no_volume_arg"])
            return
        try:
            v = float(args.split()[0].replace(",", "."))
        except ValueError:
            await utils.answer(message, self.strings["volume_invalid"])
            return
        if v > 1.0:
            v = v / 100.0
        if not 0.0 <= v <= 1.0:
            await utils.answer(message, self.strings["volume_invalid"])
            return
        try:
            await aonshot_volume(cfg.token, v, None, cfg.device_id, cfg.timeout)
            await utils.answer(message, self.strings["volume_changed"].format(int(round(v * 100))))
        except (SDKError, ValueError) as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    # ---------- likes (current track via REST) ----------
    async def _current_track_ids(self, cfg) -> tuple | None:
        snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
        tid = getattr(snap, "track_id", None)
        if not tid:
            return None
        album_id = None
        try:
            async with AsyncYandexMusic(token=cfg.token) as ym:
                tracks = await ym.tracks.get([str(tid)])
                if tracks:
                    albums = getattr(tracks[0], "albums", None) or []
                    if albums:
                        album_id = getattr(albums[0], "id", None)
        except Exception:
            pass
        return str(tid), album_id

    @error_handler
    @tokenized
    @loader.command(ru_doc="- ❤️ Лайкнуть играющий трек")
    async def ymlike(self, message: Message):
        """- ❤️ Like current track"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            ids = await self._current_track_ids(cfg)
            if not ids:
                await utils.answer(message, self.strings["no_music"])
                return
            tid, _ = ids
            async with self._rest() as ym:
                await ym.likes.add_track([tid])
            await utils.answer(message, self.strings["liked"])
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="- 💔 Убрать лайк с играющего трека")
    async def ymunlike(self, message: Message):
        """- 💔 Unlike current track"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            ids = await self._current_track_ids(cfg)
            if not ids:
                await utils.answer(message, self.strings["no_music"])
                return
            tid, _ = ids
            async with self._rest() as ym:
                await ym.likes.remove_track([tid])
            await utils.answer(message, self.strings["unlike"])
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="Лайки-треки")
    async def ymlikes(self, message: Message):
        """Лайки-треки"""
        try:
            async with self._rest() as ym:
                liked = await ym.likes.tracks()
            try:
                n = len(liked)
            except Exception:
                n = str(liked)[:100]
            await utils.answer(message, self.strings["likes_count"].format(n))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    # ---------- search + download by number (Spotify ssearch analog) ----------
    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymsearch - 🔍 Поиск треков.", alias="yms")
    async def ymsearch(self, message: Message):
        """| .yms - 🔍 Search for tracks."""
        args = utils.get_args_raw(message).strip()
        if not args:
            await utils.answer(message, self.strings["no_search_query"])
            return
        search_results = self.get("last_search_results", [])
        if args.isdigit() and search_results:
            track_number = int(args)
            if 0 < track_number <= len(search_results):
                msg = await utils.answer(message, self.strings["downloading_track"])
                track_info = search_results[track_number - 1]
                track_name, artists = self._track_info(track_info)
                reply_to_id = self._reply_id(message)
                chat_id = self._get_chat_id(message)
                target = chat_id if chat_id is not None else msg
                success = await self._download_track(
                    target, f"{artists} {track_name}", track_name=track_name, artists=artists,
                    log_context=f"{track_name} - {artists}", reply_to_id=reply_to_id,
                )
                if success:
                    with contextlib.suppress(Exception):
                        await msg.delete()
                self.set("last_search_results", [])
                return
        await utils.answer(message, self.strings["searching_tracks"].format(utils.escape_html(args)))
        try:
            async with self._rest() as ym:
                res = await ym.search.query(args)
            tracks = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
            tracks = list(tracks)[:5]
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        if not tracks:
            await utils.answer(message, self.strings["no_tracks_found"].format(utils.escape_html(args)))
            return
        # cache minimal serializable form
        cached = []
        for t in tracks:
            name, arts = self._track_info(t)
            cached.append((name, arts))
        self.set("last_search_results", cached)
        # monkey: keep objects in memory for buttons via _ym_store
        store_id = id(tracks)
        self._ym_store[store_id] = [(self._track_info(t)[0], self._track_info(t)[1]) for t in tracks]
        # inline buttons expect objects with _track_info support -> pass cached tuples
        await self.inline.form(
            self.strings["search_results_inline"].format(count=len(tracks), query=utils.escape_html(args)),
            message=message,
            reply_markup=self._search_keyboard(cached, self._get_chat_id(message), self._reply_id(message)),
        )

    # ---------- playlists ----------
    def _cache_playlists(self, pls) -> list:
        out = []
        for p in pls or []:
            out.append(
                {
                    "kind": getattr(p, "kind", None),
                    "title": str(getattr(p, "title", "?")),
                    "revision": getattr(p, "revision", 1) or 1,
                }
            )
        self.set("last_playlists", out)
        return out

    async def _uid(self) -> int | None:
        try:
            async with self._rest() as ym:
                st = await ym.account.status()
            acc = getattr(st, "account", None)
            uid = getattr(acc, "uid", None)
            return int(uid) if uid is not None else None
        except Exception:
            return None

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymplaylists - 📃 Мои плейлисты", alias="ympls")
    async def ymplaylists(self, message: Message):
        """| .ympls - 📃 Playlists"""
        try:
            async with self._rest() as ym:
                pls = await ym.playlists.list()
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))
            return
        cached = self._cache_playlists(pls)
        if not cached:
            await utils.answer(message, self.strings["no_playlists"])
            return
        text = ""
        for i, p in enumerate(cached[:20]):
            text += f"<b>{i + 1}.</b> {utils.escape_html(p['title'])} <code>{p['kind']}</code>\n"
        await utils.answer(message, self.strings["playlists_list"].format(text.strip()))

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ympla - ➕ Добавить текущий трек в плейлист (номер из .ymplaylists)", alias="ympla")
    async def ympla(self, message: Message):
        """| .ympla - ➕ Add current track to playlist"""
        args = utils.get_args_raw(message).strip()
        if not args or not args.split()[0].isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        index = int(args.split()[0]) - 1
        playlists = self.get("last_playlists", []) or []
        if not playlists or not (0 <= index < len(playlists)):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            ids = await self._current_track_ids(cfg)
            if not ids:
                await utils.answer(message, self.strings["no_music"])
                return
            tid, album_id = ids
            if not album_id:
                await utils.answer(message, self.strings["err"].format("no album_id for track"))
                return
            pl = playlists[index]
            async with self._rest() as ym:
                uid = await self._uid()
                await ym.playlists.insert_track(pl["kind"], tid, album_id, at=0, revision=int(pl.get("revision", 1) or 1), user_id=uid)
            await utils.answer(message, self.strings["added_to_playlist"].format(utils.escape_html(f"{tid}"), utils.escape_html(pl["title"])))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymplr - ➖ Убрать текущий трек из плейлиста", alias="ymplr")
    async def ymplr(self, message: Message):
        """| .ymplr - ➖ Remove current track from playlist"""
        args = utils.get_args_raw(message).strip()
        if not args or not args.split()[0].isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        # NOTE: Yandex remove needs position range; find track position first
        index = int(args.split()[0]) - 1
        playlists = self.get("last_playlists", []) or []
        if not playlists or not (0 <= index < len(playlists)):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            ids = await self._current_track_ids(cfg)
            if not ids:
                await utils.answer(message, self.strings["no_music"])
                return
            tid, _ = ids
            pl = playlists[index]
            async with self._rest() as ym:
                uid = await self._uid()
                full = await ym.playlists.get(pl["kind"], user_id=uid)
                tracks = getattr(full, "tracks", None) or []
                pos = None
                for i, tr in enumerate(tracks):
                    t = getattr(tr, "track", None) or tr
                    if str(getattr(t, "id", "")) == str(tid):
                        pos = i
                        break
                if pos is None:
                    await utils.answer(message, self.strings["no_tracks_found"].format(utils.escape_html(str(tid))))
                    return
                await ym.playlists.delete_track(pl["kind"], pos, pos + 1, revision=int(pl.get("revision", 1) or 1), user_id=uid)
            await utils.answer(message, self.strings["removed_from_playlist"].format(utils.escape_html(str(tid)), utils.escape_html(pl["title"])))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ymplc - 🆕 Создать плейлист", alias="ymplc")
    async def ymplc(self, message: Message):
        """| .ymplc - 🆕 Create playlist"""
        name = utils.get_args_raw(message).strip()
        if not name:
            await utils.answer(message, self.strings["no_playlist_name"])
            return
        try:
            async with self._rest() as ym:
                uid = await self._uid()
                await ym.playlists.create(name, "public", user_id=uid)
            await utils.answer(message, self.strings["playlist_created"].format(utils.escape_html(name)))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="| .ympld - 🗑 Удалить плейлист (номер из .ymplaylists)", alias="ympld")
    async def ympld(self, message: Message):
        """| .ympld - 🗑 Delete playlist"""
        args = utils.get_args_raw(message).strip()
        if not args or not args.split()[0].isdigit():
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        index = int(args.split()[0]) - 1
        playlists = self.get("last_playlists", []) or []
        if not playlists or not (0 <= index < len(playlists)):
            await utils.answer(message, self.strings["invalid_playlist_index"])
            return
        pl = playlists[index]
        try:
            async with self._rest() as ym:
                uid = await self._uid()
                await ym.playlists.delete(pl["kind"], user_id=uid)
            await utils.answer(message, self.strings["playlist_deleted"].format(utils.escape_html(pl["title"])))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    @error_handler
    @tokenized
    @loader.command(ru_doc="Аккаунт")
    async def ymacc(self, message: Message):
        """Аккаунт"""
        try:
            async with self._rest() as ym:
                st = await ym.account.status()
            acc = getattr(st, "account", None)
            login = getattr(acc, "login", "?")
            uid = getattr(acc, "uid", "?")
            await utils.answer(message, self.strings["account"].format(utils.escape_html(str(login)), uid))
        except Exception as e:
            await utils.answer(message, self.strings["err"].format(utils.escape_html(str(e)[:200])))

    async def watcher(self, message: Message):
        """Watcher for inline download tags."""
        raw = getattr(message, "raw_text", "") or ""
        if "ymdl_" in raw:
            try:
                tag = raw.split("ymdl_")[1].split("</i>")[0]
                sid, idx = tag.split("_")
                store_id, index = int(sid), int(idx)
            except Exception:
                return
            data = self._ym_store.pop(store_id, [])
            if not data or index >= len(data):
                return
            track_name, artists = data[index]
            chat_id = self._get_chat_id(message)
            if not chat_id:
                return
            reply_to_id = self._reply_id(message)
            success = await self._download_track(
                chat_id, f"{artists} {track_name}", track_name=track_name, artists=artists,
                log_context=f"{track_name} - {artists}", reply_to_id=reply_to_id,
            )
            if success:
                with contextlib.suppress(Exception):
                    await message.delete()
            return

