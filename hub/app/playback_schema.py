"""Wire-only data: canonical media identities, never resolved URLs or local paths."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DeviceId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Revision = Annotated[int, Field(ge=0)]
Seconds = Annotated[float, Field(ge=0, le=604800, allow_inf_nan=False)]
_REF = re.compile(r"(?:(?:dz|hm|mb|nd):[A-Za-z0-9_-]+|file:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+)\Z")


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Track(WireModel):
    ref: str = Field(max_length=256)
    nd_song_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(max_length=512)
    artist: str = Field(max_length=512)
    album: str = Field(max_length=512)
    duration_s: Seconds

    @model_validator(mode="after")
    def canonical_identity(self) -> Self:
        if (self.ref and not _REF.fullmatch(self.ref)) or (not self.ref and not self.nd_song_id):
            raise ValueError("Нужен идентификатор каталога или библиотеки, не путь или URL")
        if self.ref.startswith("nd:") and self.nd_song_id and self.ref[3:] != self.nd_song_id:
            raise ValueError("Идентификаторы библиотеки не совпадают")
        return self


class Snapshot(WireModel):
    queue: list[Track] = Field(max_length=1000)
    index: int = Field(ge=-1, le=999)
    occurrence: str = Field(max_length=128, pattern=r"^[A-Za-z0-9_.:-]*$")
    position: Seconds
    playing: bool
    volume: float = Field(ge=0, le=1, allow_inf_nan=False)
    repeat: Literal["off", "all", "one"]
    shuffle: bool

    @model_validator(mode="after")
    def valid_cursor(self) -> Self:
        if self.queue:
            if not 0 <= self.index < len(self.queue) or not self.occurrence:
                raise ValueError("Неверная позиция в очереди")
        elif self.index not in (-1, 0) or self.occurrence or self.playing or self.position:
            raise ValueError("Пустая очередь должна быть остановлена")
        return self


class Heartbeat(WireModel):
    device_id: DeviceId
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f/\\]+$")
    kind: Literal["desktop", "web"]
    state: Snapshot | None = None
    revision: Revision | None = None


class Claim(WireModel):
    device_id: DeviceId
    state: Snapshot
    revision: Revision


class Handoff(WireModel):
    device_id: DeviceId
    target: DeviceId
    revision: Revision


class Control(Handoff):
    kind: Literal["play", "pause", "next", "prev", "seek", "volume"]
    value: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_value(self) -> Self:
        if self.kind in ("seek", "volume"):
            maximum = 1 if self.kind == "volume" else 604800
            if self.value is None or not 0 <= self.value <= maximum:
                raise ValueError("Неверное значение команды")
        else:
            self.value = None  # Button commands may carry a generic numeric transport field.
        return self


class Ack(WireModel):
    device_id: DeviceId
    command_id: DeviceId
    ok: bool
    error: str | None = Field(default=None, max_length=512)
    state: Snapshot | None = None
