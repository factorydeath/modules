# requires: yandex-music websockets betterproto
# meta developer: @yandex_music_sdk_pro
# scope: heroku_min 1.5.0
# scope: hikka_min 1.6.0
"""🎧 Yandex Music — весь SDK в одном модуле + пульт.

Работает и на coddrago/Heroku, и на hikariatama/Hikka.
Ничего извне не нужно кроме `yandex-music[ynison]` (поставится сам).

Команды:
.ymtoken | .ymstatus | .ymcur | .ymdev | .ympause | .ymplay | .ymtoggle
.ymnext | .ymprev | .ymvol | .ymsearch | .ymacc | .ymlikes | .ymplaylists
"""

__version__ = (1, 1, 0)

import asyncio
import contextlib
import html
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:  # Heroku-форк coddrago
    from heroku import loader, utils
    from herokutl.tl.types import Message
except ImportError:  # Hikka / FTG fallback
    from .. import loader, utils  # type: ignore
    from hikkatl.types import Message  # type: ignore

log = logging.getLogger("yandex_music_allinone")

# ============================================================ SDK: config
@dataclass(slots=True)
class _Retry:
    attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 4.0


@dataclass(slots=True)
class _Cfg:
    token: str | None = None
    language: str = "ru"
    timeout: float = 20.0
    retry: _Retry = field(default_factory=_Retry)
    device_id: str | None = None
    device_title: str = "Python SDK"

    def client_kwargs(self) -> dict:
        return {"language": self.language}

    def ynison_kwargs(self) -> dict:
        kw = {"device_title": self.device_title}
        if self.device_id:
            kw["device_id"] = self.device_id
        return kw


# ============================================================ SDK: errors
class SDKError(Exception):
    pass


class AuthenticationError(SDKError):
    pass


class APIError(SDKError):
    pass


class NotFoundError(APIError):
    pass


class RemoteError(SDKError):
    pass


class RemoteConnectionError(RemoteError):
    pass


class NoActiveDeviceError(RemoteError):
    pass


class QueueBoundaryError(RemoteError):
    pass


class UnsupportedOperation(RemoteError):
    pass


class RetryExhausted(APIError):
    pass


def _map_upstream(exc: Exception) -> SDKError:
    name = type(exc).__name__
    msg = str(exc)
    if name in ("YnisonNoActiveDeviceError", "NoActiveDeviceError"):
        return NoActiveDeviceError(msg)
    if name in ("YnisonQueueBoundaryError", "QueueBoundaryError"):
        return QueueBoundaryError(msg)
    if name.startswith("Ynison"):
        if "unauthorized" in msg.lower() or "401" in msg:
            return AuthenticationError(msg)
        if "timeout" in name.lower():
            return RemoteConnectionError(msg)
        return RemoteError(msg)
    if "401" in msg or "unauthor" in msg.lower():
        return AuthenticationError(msg)
    if name == "NotFoundError" or "404" in msg:
        return NotFoundError(msg)
    return APIError(f"{name}: {msg}")


@contextlib.contextmanager
def _map_errors(remote: bool = False):
    try:
        yield
    except SDKError:
        raise
    except Exception as exc:
        mapped = _map_upstream(exc)
        if remote and isinstance(mapped, APIError) and not isinstance(mapped, RemoteError):
            raise RemoteError(str(mapped)) from exc
        raise mapped from exc


def _retry_sync(fn, attempts: int, base_delay: float, max_delay: float):
    last = None
    total = max(1, attempts)
    for n in range(total):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if n + 1 >= total:
                break
            time.sleep(min(max_delay, base_delay * (2 ** n)) * random.uniform(0.8, 1.2))
    raise RetryExhausted(f"Exhausted {total} attempts, last: {last}") from last


# ============================================================ SDK: models
def _fmt_ms(value) -> str:
    if value is None:
        return "-"
    s = max(0, int(value) // 1000)
    return f"{s // 60}:{s % 60:02d}"


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    title: str | None = None
    device_type: str | None = None
    active: bool = False
    raw: Any = None


@dataclass(frozen=True, slots=True)
class Playback:
    active_device_id: str | None
    paused: bool | None
    progress_ms: int | None
    duration_ms: int | None
    track_id: str | None
    track_title: str | None
    artist_title: str | None = None
    devices: tuple = ()
    raw: Any = None


def _g(obj: Any, name: str, default=None):
    return getattr(obj, name, default) if obj is not None else default


def snapshot_from_state(state: Any) -> Playback:
    if state is None:
        return Playback(None, None, None, None, None, None, None, (), None)
    player = _g(state, "player_state")
    status = _g(player, "status")
    queue = _g(player, "player_queue")
    items = _g(queue, "playable_list", []) or []
    index = _g(queue, "current_playable_index", -1)
    cur = items[index] if isinstance(index, int) and 0 <= index < len(items) else None
    if cur is None:
        try:
            from yandex_music.ynison import utils as _u

            cur = _u.get_current_playable(state)
        except Exception:
            cur = None
    tid = _g(cur, "track_id") or _g(cur, "id")
    title = _g(cur, "title")
    artist = None
    try:
        arts = _g(cur, "artists") or []
        names = [x for x in (_g(a, "title") or _g(a, "name") for a in arts) if x]
        artist = ", ".join(names) or None
    except Exception:
        pass
    active = _g(state, "active_device_id_optional")
    devs = []
    for rd in _g(state, "devices", []) or []:
        info = _g(rd, "info", rd)
        did = _g(rd, "id") or _g(info, "device_id")
        if did:
            devs.append(
                Device(str(did), _g(info, "title"), _g(info, "type"),
                       str(did) == str(active) if active is not None else False, rd)
            )
    prog = _g(status, "progress_ms")
    dur = _g(status, "duration_ms")
    return Playback(
        str(active) if active is not None else None,
        _g(status, "paused"),
        int(prog) if prog is not None else None,
        int(dur) if dur is not None else None,
        str(tid) if tid is not None else None,
        title, artist, tuple(devs), state,
    )


def _clamp_vol(value: float) -> float:
    v = float(value)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"volume must be 0.0-1.0, got {value!r}")
    return v


# ============================================================ SDK: services
class _Svc:
    """Домен с явными методами + fallback на любой новый метод upstream."""

    def __init__(self, client: Any, cfg: _Cfg):
        self._c = client
        self._cfg = cfg

    def _read(self, fn):
        with _map_errors():
            r = self._cfg.retry
            last = None
            for n in range(max(1, r.attempts)):
                try:
                    return fn()
                except SDKError:
                    raise
                except Exception as exc:
                    last = exc
                    if n + 1 >= max(1, r.attempts):
                        break
                    time.sleep(min(r.max_delay, r.base_delay * (2 ** n)))
            raise _map_upstream(last) from last

    def _write(self, fn):
        with _map_errors():
            return fn()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._c, name, None)
        if attr is None:
            raise AttributeError(f"Upstream has no method {name!r}")
        if not callable(attr):
            return attr

        def _wrapper(*a, **k):
            with _map_errors():
                return attr(*a, **k)

        return _wrapper


class AccountSvc(_Svc):
    def status(self): return self._read(lambda: self._c.account_status())
    def settings(self): return self._read(lambda: self._c.account_settings())
    def experiments(self): return self._read(lambda: self._c.account_experiments())


class SearchSvc(_Svc):
    def query(self, text, page=0, type_="all", nocorrect=False, **k):
        return self._read(lambda: self._c.search(text, page=page, type_=type_, nocorrect=nocorrect, **k))
    def suggest(self, text, **k):
        return self._read(lambda: self._c.search_suggest(text, **k))


class TrackSvc(_Svc):
    def get(self, ids):
        if isinstance(ids, (str, int)): ids = [ids]
        ids = list(ids)
        return self._read(lambda: self._c.tracks(ids))
    def full(self, tid, *a, **k): return self._read(lambda: self._c.tracks_full_info(tid, *a, **k))
    def similar(self, tid, *a, **k): return self._read(lambda: self._c.tracks_similar(tid, *a, **k))
    def lyrics(self, tid, *a, **k): return self._read(lambda: self._c.tracks_lyrics(tid, *a, **k))
    def download_info(self, tid, *a, **k): return self._read(lambda: self._c.tracks_download_info(tid, *a, **k))


class ArtistSvc(_Svc):
    def get(self, ids):
        if isinstance(ids, (str, int)): ids = [ids]
        ids = list(ids)
        return self._read(lambda: self._c.artists(ids))
    def brief_info(self, aid, *a, **k): return self._read(lambda: self._c.artists_brief_info(aid, *a, **k))
    def tracks(self, aid, *a, **k): return self._read(lambda: self._c.artists_tracks(aid, *a, **k))
    def similar(self, aid, *a, **k): return self._read(lambda: self._c.artists_similar(aid, *a, **k))


class AlbumSvc(_Svc):
    def get(self, ids):
        if isinstance(ids, (str, int)): ids = [ids]
        ids = list(ids)
        return self._read(lambda: self._c.albums(ids))
    def with_tracks(self, aid, *a, **k): return self._read(lambda: self._c.albums_with_tracks(aid, *a, **k))


class PlaylistSvc(_Svc):
    def list(self, user_id=None, **k):
        return self._read(lambda: self._c.users_playlists_list(user_id, **k))
    def get(self, kind, user_id=None, **k):
        return self._read(lambda: self._c.users_playlists(kind, user_id, **k))
    def create(self, title, visibility="public", user_id=None, **k):
        return self._write(lambda: self._c.users_playlists_create(title, visibility, user_id, **k))
    def delete(self, kind, user_id=None, **k):
        return self._write(lambda: self._c.users_playlists_delete(kind, user_id, **k))


class LikesSvc(_Svc):
    def tracks(self, user_id=None, **k): return self._read(lambda: self._c.users_likes_tracks(user_id, **k))
    def artists(self, user_id=None, **k): return self._read(lambda: self._c.users_likes_artists(user_id, **k))
    def albums(self, user_id=None, **k): return self._read(lambda: self._c.users_likes_albums(user_id, **k))
    def playlists(self, user_id=None, **k): return self._read(lambda: self._c.users_likes_playlists(user_id, **k))
    def add_track(self, ids, user_id=None, **k): return self._write(lambda: self._c.users_likes_tracks_add(ids, user_id, **k))
    def remove_track(self, ids, user_id=None, **k): return self._write(lambda: self._c.users_likes_tracks_remove(ids, user_id, **k))


class QueueSvc(_Svc):
    def list(self, *a, **k): return self._read(lambda: self._c.queues_list(*a, **k))
    def get(self, qid, *a, **k): return self._read(lambda: self._c.queue(qid, *a, **k))


class RadioSvc(_Svc):
    def dashboard(self, *a, **k): return self._read(lambda: self._c.rotor_stations_dashboard(*a, **k))
    def stations(self, *a, **k): return self._read(lambda: self._c.rotor_stations_list(*a, **k))
    def tracks(self, *a, **k): return self._read(lambda: self._c.rotor_station_tracks(*a, **k))
    def feedback(self, *a, **k): return self._write(lambda: self._c.rotor_station_feedback(*a, **k))


class LandingSvc(_Svc):
    def landing(self, *a, **k): return self._read(lambda: self._c.landing(*a, **k))
    def feed(self, *a, **k): return self._read(lambda: self._c.feed(*a, **k))
    def chart(self, *a, **k): return self._read(lambda: self._c.chart(*a, **k))


class HistorySvc(_Svc):
    def list(self, *a, **k): return self._read(lambda: self._c.music_history(*a, **k))


class PinsSvc(_Svc):
    def list(self, *a, **k): return self._read(lambda: self._c.pins(*a, **k))


class GenericSvc(_Svc):
    pass


# ============================================================ SDK: lite client
class _LiteYM:
    def __init__(self, token, language="ru", timeout=20.0, device_id=None):
        self.cfg = _Cfg(token=token, language=language, timeout=timeout, device_id=device_id)
        from yandex_music import Client

        self.raw = Client(token, language=language)
        self.raw_async = self.raw
        self.account = AccountSvc(self.raw, self.cfg)
        self.search = SearchSvc(self.raw, self.cfg)
        self.tracks = TrackSvc(self.raw, self.cfg)
        self.artists = ArtistSvc(self.raw, self.cfg)
        self.albums = AlbumSvc(self.raw, self.cfg)
        self.playlists = PlaylistSvc(self.raw, self.cfg)
        self.likes = LikesSvc(self.raw, self.cfg)
        self.queue = QueueSvc(self.raw, self.cfg)
        self.radio = RadioSvc(self.raw, self.cfg)
        self.landing = LandingSvc(self.raw, self.cfg)
        self.history = HistorySvc(self.raw, self.cfg)
        self.pins = PinsSvc(self.raw, self.cfg)
        self.api = GenericSvc(self.raw, self.cfg)
        # Остальные домены — через api/raw (полный upstream без дыр).

    def init(self):
        with _map_errors():
            self.raw.init()
        return self

    # ---- Ynison one-shot (для юзербота идеально: без висящей сессии) ----
    def _ynison_simple(self):
        try:
            from yandex_music.ynison import simple as _s
            return _s
        except ImportError as exc:
            raise UnsupportedOperation(
                "Нужен pip install 'yandex-music[ynison]' на хосте юзербота."
            ) from exc

    def rstate(self) -> Playback:
        with _map_errors(remote=True):
            st = self._ynison_simple().get_state(self.cfg.token, self.cfg.device_id, self.cfg.timeout)
            return snapshot_from_state(st)

    def rpause(self): return self._rcmd("pause")
    def rresume(self): return self._rcmd("resume")
    def rnext(self): return self._rcmd("next_track")
    def rprev(self): return self._rcmd("previous_track")

    def _rcmd(self, name):
        with _map_errors(remote=True):
            return getattr(self._ynison_simple(), name)(self.cfg.token, self.cfg.device_id, self.cfg.timeout)

    def rvol(self, volume: float):
        volume = _clamp_vol(volume)
        with _map_errors(remote=True):
            return self._ynison_simple().set_volume(
                self.cfg.token, volume, None, self.cfg.device_id, self.cfg.timeout
            )

    def rtoggle(self):
        snap = self.rstate()
        if snap.paused is False:
            return self.rpause()
        return self.rresume()


# ============================================================ Module
def _bar(progress, duration, width=10):
    if not progress or not duration:
        return "─" * width
    r = max(0.0, min(1.0, progress / duration))
    f = int(round(r * width))
    return "●" * f + "─" * (width - f)


@loader.tds
class YandexMusicMod(loader.Module):
    """🎧 Яндекс Музыка: весь SDK + пульт в одном файле"""

    strings = {
        "name": "YandexMusic",
        "no_token": "❌ Нет токена. Сначала: <code>.ymtoken &lt;токен&gt;</code>",
        "saved": "✅ Токен сохранён",
        "need_args": "❌ Нужен аргумент",
        "state_title": "🎧 <b>Сейчас играет</b>",
        "paused": "⏸ Пауза",
        "playing": "▶️ Играет",
        "sent": "✅ {}",
        "vol": "🔊 Громкость → {}",
        "bad_vol": "❌ Громкость: 0-100 или 0.0-1.0",
        "devices_title": "📱 <b>Устройства:</b>",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "TOKEN",
                "",
                "OAuth-токен (или через .ymtoken)",
                validator=loader.validators.Hidden(loader.validators.String()),
            ),
            loader.ConfigValue(
                "DEVICE_ID",
                "",
                "Ynison device_id",
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
        try:
            import yandex_music  # noqa
        except ImportError:
            await asyncio.to_thread(
                subprocess.check_call,
                [sys.executable, "-m", "pip", "install", "-q", "yandex-music[ynison]"],
            )

    def _token(self):
        t = (self.config["TOKEN"] or "").strip() or (self.get("token") or "").strip()
        return t or None

    def _ym(self) -> _LiteYM:
        token = self._token()
        if not token:
            raise AuthenticationError("no token")
        try:
            timeout = float(self.config["TIMEOUT"])
        except Exception:
            timeout = 20.0
        dev = (self.config["DEVICE_ID"] or "").strip() or None
        return _LiteYM(token, timeout=timeout, device_id=dev)

    def _text(self, snap: Playback) -> str:
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
            self.strings["state_title"],
            f"🎵 <b>{html.escape(str(title))}</b>",
            f"{st} | <code>{_fmt_ms(snap.progress_ms)}/{_fmt_ms(snap.duration_ms)}</code>",
            f"<code>{bar}</code>",
            f"📱 <code>{html.escape(str(snap.active_device_id or '-'))}</code>",
        ]
        if snap.devices:
            out.append("")
            out.append(self.strings["devices_title"])
            for d in snap.devices[:10]:
                mark = "●" if d.active else "○"
                out.append(f"{mark} {html.escape(str(d.title or d.id))} <code>{html.escape(d.id)}</code>")
        return "\n".join(out)

    @loader.command(ru_doc="Сохранить токен")
    async def ymtoken(self, message: Message):
        """Сохранить токен: .ymtoken <токен>"""
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

    @loader.command(ru_doc="Статус плеера")
    async def ymstatus(self, message: Message):
        """Статус плеера"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        await utils.answer(message, "⏳ ...")
        try:
            snap = await asyncio.to_thread(ym.rstate)
            await utils.answer(message, self._text(snap))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Что играет")
    async def ymcur(self, message: Message):
        """Что играет"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            snap = await asyncio.to_thread(ym.rstate)
            t = snap.track_title or "-"
            if snap.artist_title:
                t = f"{snap.artist_title} — {t}"
            await utils.answer(message, f"🎵 <b>{html.escape(t)}</b>")
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Устройства")
    async def ymdev(self, message: Message):
        """Устройства"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            snap = await asyncio.to_thread(ym.rstate)
            if not snap.devices:
                await utils.answer(message, "(нет устройств)")
                return
            out = [self.strings["devices_title"]]
            for d in snap.devices:
                mark = "●" if d.active else "○"
                out.append(f"{mark} {html.escape(str(d.title or d.id))} <code>{html.escape(d.id)}</code>")
            await utils.answer(message, "\n".join(out))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    async def _do(self, message: Message, fn_name: str, label: str):
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            await asyncio.to_thread(getattr(ym, fn_name))
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
        await self._do(message, "rpause", "⏸ пауза")

    @loader.command(ru_doc="Продолжить")
    async def ymplay(self, message: Message):
        """Продолжить"""
        await self._do(message, "rresume", "▶️ play")

    @loader.command(ru_doc="Тоггл")
    async def ymtoggle(self, message: Message):
        """Пауза/продолжить"""
        await self._do(message, "rtoggle", "⏯ toggle")

    @loader.command(ru_doc="Следующий трек")
    async def ymnext(self, message: Message):
        """Следующий трек"""
        await self._do(message, "rnext", "⏭ next")

    @loader.command(ru_doc="Предыдущий трек")
    async def ymprev(self, message: Message):
        """Предыдущий трек"""
        await self._do(message, "rprev", "⏮ prev")

    @loader.command(ru_doc="Громкость: .ymvol 50")
    async def ymvol(self, message: Message):
        """Громкость: .ymvol 50"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        raw = utils.get_args_raw(message).strip().split()
        if not raw:
            await utils.answer(message, self.strings["bad_vol"])
            return
        try:
            v = float(raw[0].replace(",", "."))
        except Exception:
            await utils.answer(message, self.strings["bad_vol"])
            return
        if v > 1.0:
            v /= 100.0
        if not 0.0 <= v <= 1.0:
            await utils.answer(message, self.strings["bad_vol"])
            return
        try:
            await asyncio.to_thread(ym.rvol, v)
            await utils.answer(message, self.strings["vol"].format(v))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    def _search_sync(self, token: str, query: str) -> str:
        from yandex_music import Client

        res = Client(token).init().search(query)
        tracks = (getattr(res, "tracks", None) and getattr(res.tracks, "results", None)) or []
        if not tracks:
            return "🔎 Ничего не найдено"
        out = ["🔎 <b>Поиск:</b>"]
        for t in tracks[:7]:
            title = getattr(t, "title", "?")
            arts = ", ".join(a.name for a in (getattr(t, "artists", None) or []) if getattr(a, "name", None))
            name = f"{arts} — {title}" if arts else str(title)
            out.append(f"🎵 {html.escape(name)} <code>{getattr(t, 'id', '?')}</code>")
        return "\n".join(out)

    @loader.command(ru_doc="Поиск: .ymsearch Miyagi")
    async def ymsearch(self, message: Message):
        """Поиск: .ymsearch Miyagi"""
        token = self._token()
        if not token:
            await utils.answer(message, self.strings["no_token"])
            return
        q = utils.get_args_raw(message).strip()
        if not q:
            await utils.answer(message, self.strings["need_args"])
            return
        await utils.answer(message, "🔎 ...")
        try:
            text = await asyncio.to_thread(self._search_sync, token, q)
            await utils.answer(message, text)
        except Exception as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Лайки-треки")
    async def ymlikes(self, message: Message):
        """Лайки-треки"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            liked = await asyncio.to_thread(ym.likes.tracks)
            try:
                n = len(liked)
            except Exception:
                n = "?"
            await utils.answer(message, f"❤️ Треков в лайках: <b>{n}</b>")
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Мои плейлисты")
    async def ymplaylists(self, message: Message):
        """Мои плейлисты"""
        try:
            ym = self._ym()
        except AuthenticationError:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            pls = await asyncio.to_thread(ym.playlists.list)
            out = ["📀 <b>Плейлисты:</b>"]
            for p in (pls or [])[:15]:
                title = html.escape(str(getattr(p, "title", "?")))
                out.append(f"• {title} <code>{getattr(p, 'kind', '?')}</code>")
            await utils.answer(message, "\n".join(out))
        except SDKError as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")

    @loader.command(ru_doc="Аккаунт")
    async def ymacc(self, message: Message):
        """Аккаунт"""
        token = self._token()
        if not token:
            await utils.answer(message, self.strings["no_token"])
            return
        try:
            def _acc():
                from yandex_music import Client

                c = Client(token).init()
                st = c.account_status()
                acc = getattr(st, "account", None)
                return f"👤 <b>{html.escape(str(getattr(acc, 'login', '?')))}</b> <code>{getattr(acc, 'uid', '?')}</code>"

            await utils.answer(message, await asyncio.to_thread(_acc))
        except Exception as e:
            await utils.answer(message, f"❌ <code>{html.escape(str(e))}</code>")
