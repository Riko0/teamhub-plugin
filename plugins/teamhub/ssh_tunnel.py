"""SSH-туннель к хабу, поднимаемый и закрываемый самим плагином.

Нужен там, где хаб недоступен напрямую: коллеге достаточно доступа по ключу,
никаких ручных команд. Порт на локальной стороне выбирается свободный, поэтому
несколько сессий не мешают друг другу.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import sys
import time
from typing import Final

CONNECT_POLL_S: Final[float] = 0.25
READY_TIMEOUT_S: Final[float] = 20.0
PROBE_TIMEOUT_S: Final[float] = 1.0
TERMINATE_TIMEOUT_S: Final[float] = 5.0


def _log(message: str) -> None:
    """Пишет диагностику в stderr: stdout занят протоколом MCP."""
    sys.stderr.write(f"[teamhub] {message}\n")
    sys.stderr.flush()


def _free_port() -> int:
    """Возвращает свободный порт на локальном интерфейсе."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Гасит процесс и дожидается его, чтобы не оставлять зомби."""
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _log("процесс ssh не завершился даже по kill")


def _port_accepts(port: int) -> bool:
    """Проверяет, принимает ли порт соединения."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(PROBE_TIMEOUT_S)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class SshTunnel:
    """Проброс локального порта на порт хаба через SSH."""

    def __init__(self, destination: str, remote_port: int, identity_file: str | None = None) -> None:
        self._destination = destination
        self._remote_port = remote_port
        self._identity_file = identity_file
        self._process: subprocess.Popen[bytes] | None = None
        self._local_port: int = 0
        self._stopped = False
        self._lock = threading.RLock()

    @property
    def local_port(self) -> int:
        """Локальный порт, на который проброшен хаб."""
        return self._local_port

    def _command(self, local_port: int) -> list[str]:
        """Собирает команду ssh для фонового проброса порта."""
        command = [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",  # TTY здесь нет, пароль спросить не у кого — только ключ
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{self._remote_port}",
        ]
        if self._identity_file:
            command += ["-o", "IdentitiesOnly=yes", "-i", self._identity_file]
        command.append(self._destination)
        return command

    def start(self) -> None:
        """Поднимает туннель и ждёт готовности порта.

        Повторный вызов при живом туннеле ничего не делает: иначе каждая
        попытка переподключения оставляла бы за собой лишний процесс ssh.

        Raises:
            RuntimeError: если ssh не запустился или порт не открылся вовремя.
        """
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """Поднимает туннель; вызывать только под блокировкой.

        После stop() больше не поднимаемся: иначе фоновый поток, доживающий
        свой круг при завершении, оставил бы за собой осиротевший ssh.
        """
        if self._stopped:
            raise RuntimeError("туннель остановлен")
        if self._process is not None and self._process.poll() is None:
            return
        self._reap()
        local_port = _free_port()
        try:
            process = subprocess.Popen(
                self._command(local_port), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )
        except OSError as exc:
            raise RuntimeError(f"не удалось запустить ssh: {exc}") from exc

        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"ssh завершился с кодом {process.returncode}; проверьте доступ по ключу")
            if _port_accepts(local_port):
                self._process, self._local_port = process, local_port
                _log(f"туннель поднят: 127.0.0.1:{local_port} -> {self._destination}:{self._remote_port}")
                return
            time.sleep(CONNECT_POLL_S)

        _terminate(process)  # иначе останется зомби
        raise RuntimeError(f"туннель до {self._destination} не открылся за {READY_TIMEOUT_S:.0f} с")

    def _reap(self) -> None:
        """Дожидается завершившегося процесса, чтобы не копить зомби."""
        if self._process is not None and self._process.poll() is not None:
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            self._process = None

    def ensure_alive(self) -> None:
        """Перезапускает туннель, если процесс ssh умер."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            _log("туннель отвалился, поднимаю заново")
            self._start_locked()

    def stop(self) -> None:
        """Закрывает туннель."""
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Закрывает туннель; вызывать только под блокировкой."""
        self._stopped = True
        if self._process is None:
            return
        _terminate(self._process)
        self._process = None
