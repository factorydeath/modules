# requires: requests websockets betterproto aiohttp aiofiles
# meta developer: @yandex_music_sdk_pro
# scope: heroku_min 1.5.0
# scope: hikka_min 1.6.0
"""🎧 Yandex Music — ПОЛНЫЙ ASYNC SDK в одном файле + пульт юзербота.

Внутри (всё async, синхрона нет): config, errors, utils, models,
services (все 22 домена, Async*), remote (snapshot, AsyncRemotePlayer,
aonshot_*), клиент AsyncYandexMusic. Работает на coddrago/Heroku и Hikka.
"""

from __future__ import annotations

__version__ = (1, 3, 0)

import asyncio
import contextlib
import html
import logging
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

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

# ============================ MODULE (Heroku/Hikka, async-only) ============================
# Всё выше — полный ASYNC SDK. Ниже — команды юзербота на его основе.
# Никакого синхрона: только await, ClientAsync, aonshot_*.

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
    """PyPI 3.0.0 НЕ содержит ynison (только main #716) — ставим с гитхаба."""
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


def _bar(progress, duration, width=12):
    if not progress or not duration:
        return "▱" * width
    r = max(0.0, min(1.0, progress / duration))
    f = int(round(r * width))
    return "▰" * f + "▱" * (width - f)


def _cover_url_from_track(track) -> str | None:
    uri = getattr(track, "cover_uri", None)
    if not uri:
        return None
    return "https://" + str(uri).replace("%%", "400x400")


async def _resolve_cover(token: str, snap) -> str | None:
    """Обложка трека через REST: сначала по id, потом поиском. None если не вышло."""
    try:
        async with AsyncYandexMusic(token=token) as ym:
            tid = getattr(snap, "track_id", None)
            if tid:
                try:
                    tracks = await ym.tracks.get([str(tid)])
                    if tracks:
                        url = _cover_url_from_track(tracks[0])
                        if url:
                            return url
                except Exception:
                    pass
            query = " ".join(x for x in (snap.artist_title, snap.track_title) if x)
            if query:
                try:
                    res = await ym.search.query(query)
                    results = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
                    if results:
                        url = _cover_url_from_track(results[0])
                        if url:
                            return url
                except Exception:
                    pass
    except Exception:
        pass
    return None


@loader.tds
class YandexMusicMod(loader.Module):
    """🎧 Яндекс Музыка: полный async-SDK + пульт в одном файле"""

    strings = {
        "name": "YandexMusic",
        "no_token": "❌ Нет токена. Сначала: <code>.ymauth</code> или <code>.ymtoken &lt;токен&gt;</code>",
        "saved": "✅ Токен сохранён",
        "need_args": "❌ Нужен аргумент",
        "state_title": "🎧 <b>Сейчас играет</b>",
        "paused": "⏸ Пауза",
        "playing": "▶️ Играет",
        "sent": "✅ {}",
        "vol": "🔊 Громкость → {}",
        "bad_vol": "❌ Громкость: 0-100 или 0.0-1.0",
        "devices_title": "📱 <b>Устройства:</b>",
        "auth_text": (
            "🔑 <b>Авторизация Яндекс Музыки</b>\n\n"
            "1. Открой ссылку и разреши доступ:\n"
            f"{AUTH_URL}\n\n"
            "2. Тебя кинет на <code>music.yandex.ru/#access_token=...</code> — "
            "скопируй кусок после <code>access_token=</code> до <code>&</code>\n"
            "3. Сохрани: <code>.ymtoken &lt;токен&gt;</code>\n\n"
            "Либо автоматически: <code>.ymcode</code> (придёт код — введи его на сайте)"
        ),
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
        )

    async def client_ready(self, client, db):
        self._client = client
        self._db = db

    # ---------- helpers ----------
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

    def _snap_text(self, snap: Playback) -> str:
        if snap.paused is True:
            st = self.strings["paused"]
        elif snap.paused is False:
            st = self.strings["playing"]
        else:
            st = "❓"
        title = snap.track_title or "-"
        if snap.artist_title:
            title = f"{snap.artist_title} — {title}"
        bar = _bar(snap.progress_ms, snap.duration_ms)
        out = [
            "🪐 <b>Yandex Music</b>",
            f"🎵 <b>{html.escape(str(title))}</b>",
            f"{st} | <code>{_fmt_ms(snap.progress_ms)} / {_fmt_ms(snap.duration_ms)}</code>",
            f"<code>{bar}</code>",
            f"📱 <code>{html.escape(str(snap.active_device_id or '-'))}</code>",
        ]
        if snap.devices:
            out.append("")
            out.append(self.strings["devices_title"])
            for d in snap.devices[:10]:
                mark = "🟢" if d.active else "⚪"
                out.append(f"{mark} {html.escape(str(d.title or d.id))} <code>{html.escape(d.id)}</code>")
        out.append("")
        out.append("⚡ <i>YandexMusic v1.3.0</i>")
        return "\n".join(out)

    async def _banner(self, message: Message, snap: Playback, token: str) -> None:
        """Баннер трека: обложка + подпись. Без обложки — просто текст."""
        caption = self._snap_text(snap)
        try:
            cover = await _resolve_cover(token, snap)
        except Exception:
            cover = None
        if cover:
            try:
                await utils.answer_file(message, cover, caption)
                return
            except Exception:
                pass
        await utils.answer(message, caption)

    async def _need_remote(self, message: Message):
        """Токен + ynison. Возвращает cfg или None (ответ уже отправлен)."""
        if not self._token():
            await utils.answer(message, self.strings["no_token"])
            return None
        ok, hint = await _ensure_ynison()
        if not ok:
            if hint == "restart":
                await utils.answer(
                    message,
                    "⏳ Ynison доставлен, нужен рестарт: <code>.restart</code> — затем повтори команду.",
                )
            else:
                await utils.answer(message, f"❌ Ynison не встал: <code>{html.escape(hint)}</code>")
            return None
        return self._cfg()

    # ---------- auth ----------
    @loader.command(ru_doc="Ссылка для получения токена")
    async def ymauth(self, message: Message):
        """Ссылка для получения токена"""
        await utils.answer(message, self.strings["auth_text"])

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
            await utils.answer(message, f"❌ <code>{html.escape(f'{type(e).__name__}: {e}')}</code>")
            return
        access = getattr(token, "access_token", None)
        if not access:
            await utils.answer(message, "❌ Пустой токен")
            return
        self.set("token", access)
        with contextlib.suppress(Exception):
            await message.delete()
        await utils.answer(message, f"{self.strings['saved']} ✅\n<code>.ymstatus</code> — проверить")

    @loader.command(ru_doc="Сохранить токен")
    async def ymtoken(self, message: Message):
        """Сохранить токен: .ymtoken <токен или ссылка>"""
        raw = utils.get_args_raw(message).strip()
        if not raw:
            await utils.answer(message, self.strings["need_args"])
            return
        if "access_token=" in raw:
            raw = raw.split("access_token=")[1].split("&")[0].strip()
        self.set("token", raw.strip())
        with contextlib.suppress(Exception):
            await message.delete()
        await utils.answer(message, self.strings["saved"])

    # ---------- remote (async one-shot) ----------
    @loader.command(ru_doc="Статус плеера")
    async def ymstatus(self, message: Message):
        """Статус плеера + баннер"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        await utils.answer(message, "⏳ ...")
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
            await self._banner(message, snap, cfg.token)
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Что играет + баннер")
    async def ymcur(self, message: Message):
        """Что играет + баннер"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
            await self._banner(message, snap, cfg.token)
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Устройства")
    async def ymdev(self, message: Message):
        """Устройства"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
            if not snap.devices:
                await utils.answer(message, "(нет устройств)")
                return
            out = [self.strings["devices_title"]]
            for d in snap.devices:
                mark = "🟢" if d.active else "⚪"
                out.append(f"{mark} {html.escape(str(d.title or d.id))} <code>{html.escape(d.id)}</code>")
            await utils.answer(message, "\n".join(out))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    async def _do(self, coro, message: Message, label: str, banner: bool = False):
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            await coro(cfg)
            if banner:
                # Даём серверу переключить трек — затем баннер нового трека.
                await asyncio.sleep(2.5)
                try:
                    snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
                    await self._banner(message, snap, cfg.token)
                    return
                except Exception:
                    pass
            await utils.answer(message, self.strings["sent"].format(label))
        except NoActiveDeviceError:
            await utils.answer(message, "⏹ Нет активного устройства — включи музыку в приложении")
        except QueueBoundaryError:
            await utils.answer(message, "⛔ Граница очереди")
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Пауза")
    async def ympause(self, message: Message):
        """Пауза"""
        await self._do(
            lambda cfg: aonshot_pause(cfg.token, cfg.device_id, cfg.timeout),
            message, "⏸ пауза",
        )

    @loader.command(ru_doc="Продолжить")
    async def ymplay(self, message: Message):
        """Продолжить"""
        await self._do(
            lambda cfg: aonshot_resume(cfg.token, cfg.device_id, cfg.timeout),
            message, "▶️ play",
        )

    @loader.command(ru_doc="Тоггл")
    async def ymtoggle(self, message: Message):
        """Пауза/продолжить"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        try:
            snap = await aonshot_state(cfg.token, cfg.device_id, cfg.timeout)
            if snap.paused:
                await aonshot_resume(cfg.token, cfg.device_id, cfg.timeout)
            else:
                await aonshot_pause(cfg.token, cfg.device_id, cfg.timeout)
            await utils.answer(message, self.strings["sent"].format("⏯ toggle"))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Следующий трек + баннер")
    async def ymnext(self, message: Message):
        """Следующий трек + баннер"""
        await self._do(
            lambda cfg: aonshot_next(cfg.token, cfg.device_id, cfg.timeout),
            message, "⏭ next", banner=True,
        )

    @loader.command(ru_doc="Предыдущий трек + баннер")
    async def ymprev(self, message: Message):
        """Предыдущий трек + баннер"""
        await self._do(
            lambda cfg: aonshot_prev(cfg.token, cfg.device_id, cfg.timeout),
            message, "⏮ prev", banner=True,
        )

    @loader.command(ru_doc="Громкость: .ymvol 50")
    async def ymvol(self, message: Message):
        """Громкость: .ymvol 50"""
        cfg = await self._need_remote(message)
        if cfg is None:
            return
        raw = utils.get_args_raw(message).strip().split()
        if not raw:
            await utils.answer(message, "❌ Громкость: 0-100 или 0.0-1.0")
            return
        try:
            v = float(raw[0].replace(",", "."))
        except Exception:
            await utils.answer(message, "❌ Громкость: 0-100 или 0.0-1.0")
            return
        if v > 1.0:
            v /= 100.0
        try:
            await aonshot_volume(cfg.token, v, None, cfg.device_id, cfg.timeout)
            filled = int(round(v * 10))
            await utils.answer(
                message, f"🔊 <code>{'▰' * filled}{'▱' * (10 - filled)}</code> {v:.0%}"
            )
        except ValueError:
            await utils.answer(message, "❌ Громкость: 0-100 или 0.0-1.0")
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    # ---------- REST (async, ynison не нужен) ----------
    def _rest(self) -> AsyncYandexMusic:
        token = self._token()
        if not token:
            raise AuthenticationError("no token")
        return AsyncYandexMusic(token=token)

    @loader.command(ru_doc="Поиск: .ymsearch Miyagi")
    async def ymsearch(self, message: Message):
        """Поиск: .ymsearch Miyagi"""
        q = utils.get_args_raw(message).strip()
        if not q:
            await utils.answer(message, self.strings["need_args"])
            return
        try:
            ym = self._rest()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        await utils.answer(message, "🔎 ...")
        try:
            async with ym:
                res = await ym.search.query(q)
            tracks = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
            if not tracks:
                await utils.answer(message, "🔎 Ничего не найдено")
                return
            out = ["🔎 <b>Поиск:</b>"]
            for t in tracks[:7]:
                title = getattr(t, "title", "?")
                arts = ", ".join(a.name for a in (getattr(t, "artists", None) or []) if getattr(a, "name", None))
                name = f"{arts} — {title}" if arts else str(title)
                out.append(f"🎵 {html.escape(name)} <code>{getattr(t, 'id', '?')}</code>")
            await utils.answer(message, "\n".join(out))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Лайки-треки")
    async def ymlikes(self, message: Message):
        """Лайки-треки"""
        try:
            ym = self._rest()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            async with ym:
                liked = await ym.likes.tracks()
            try:
                await utils.answer(message, f"❤️ Треков в лайках: <b>{len(liked)}</b>")
            except Exception:
                await utils.answer(message, f"❤️ <code>{html.escape(str(liked)[:500])}</code>")
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Мои плейлисты")
    async def ymplaylists(self, message: Message):
        """Мои плейлисты"""
        try:
            ym = self._rest()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            async with ym:
                pls = await ym.playlists.list()
            out = ["📀 <b>Плейлисты:</b>"]
            for p in (pls or [])[:15]:
                out.append(f"• {html.escape(str(getattr(p, 'title', '?')))} <code>{getattr(p, 'kind', '?')}</code>")
            await utils.answer(message, "\n".join(out))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Аккаунт")
    async def ymacc(self, message: Message):
        """Аккаунт"""
        try:
            ym = self._rest()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            async with ym:
                st = await ym.account.status()
            acc = getattr(st, "account", None)
            await utils.answer(
                message,
                f"👤 <b>{html.escape(str(getattr(acc, 'login', '?')))}</b> "
                f"<code>{getattr(acc, 'uid', '?')}</code>",
            )
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

