"""Vigilante del archivo de control: activar fallas y correcciones editando un YAML.

El archivo describe el estado deseado (``enabled`` + parámetros por falla y por mecanismo). Cada
``FAULTS_POLL_S`` segundos se revisa; si cambió, se compara con la versión aplicada anterior y solo se
actúa sobre las entradas que cambiaron. Así conviven el archivo y la API /admin: una falla prendida
por la API no se apaga porque el archivo la tenga en ``enabled: false`` sin tocarla.

Usa las mismas funciones que la API (FaultManager y la configuración de resiliencia), de modo que
métricas, logs, kill switch y ``duration_s`` funcionan igual. Un YAML inválido no cambia nada.
"""
import copy
import hashlib
import logging
import os
import threading

import yaml

from . import telemetry as t
from .faults import DISABLE_ORDER, ENABLE_ORDER, FaultsDisarmed
from .resilience import DEFAULTS as RESILIENCE_DEFAULTS

log = logging.getLogger('e3app.control_file')
RESERVED = {'enabled', 'duration_s'}


class ControlFileWatcher:
    def __init__(self, engine, apply_resilience, path, poll_s=1.0):
        self.engine = engine
        self.apply_resilience = apply_resilience
        self.path = path
        self.poll_s = poll_s
        self.applied_faults = {}
        self.applied_resilience = {}
        self._digest = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='control-file', daemon=True)

    def start(self):
        t.log_event(log, 'control_file_watching', path=self.path, poll_s=self.poll_s)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as error:  # noqa: BLE001 - el vigilante nunca debe morir
                t.log_event(log, 'control_file_error', level=logging.ERROR, error=repr(error))
            self._stop.wait(self.poll_s)

    def check(self):
        try:
            with open(self.path, 'rb') as stream:
                raw = stream.read()
        except FileNotFoundError:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._digest:
            return
        self._digest = digest
        try:
            doc = yaml.safe_load(raw) or {}
            faults, resilience = self._validate(doc)
        except (yaml.YAMLError, ValueError) as error:
            t.log_event(log, 'control_file_invalid', level=logging.ERROR, error=str(error))
            return
        changes = self._apply_faults(faults) + self._apply_resilience(resilience)
        t.log_event(log, 'control_file_applied', changes=changes or ['sin cambios'])

    def _validate(self, doc):
        if not isinstance(doc, dict):
            raise ValueError('el archivo debe ser un mapa con las claves fallas y resiliencia')
        faults = doc.get('fallas') or {}
        resilience = doc.get('resiliencia') or {}
        if not isinstance(faults, dict) or not isinstance(resilience, dict):
            raise ValueError('fallas y resiliencia deben ser mapas')
        for name, spec in faults.items():
            if name not in self.engine.faults.faults:
                raise ValueError(f'falla desconocida: {name}')
            if not isinstance(spec, dict) or not isinstance(spec.get('enabled', False), bool):
                raise ValueError(f'{name}: se espera un mapa con enabled: true/false')
        for name, values in resilience.items():
            if name not in RESILIENCE_DEFAULTS:
                raise ValueError(f'mecanismo desconocido: {name}')
            if not isinstance(values, dict):
                raise ValueError(f'{name}: se espera un mapa de parámetros')
        return faults, resilience

    def _apply_faults(self, faults):
        changes = []
        manager = self.engine.faults
        names = set(self.applied_faults) | set(faults)
        # Primero se apagan y después se prenden, cada grupo en su orden seguro (ver faults.py):
        # así nunca se recrea un índice mientras otra falla retiene una transacción abierta.
        turn_off = [n for n in DISABLE_ORDER if n in names and not self._wanted(faults.get(n))
                    and self._wanted(self.applied_faults.get(n))]
        turn_on = [n for n in ENABLE_ORDER if n in names and self._wanted(faults.get(n))
                   and faults.get(n) != self.applied_faults.get(n)]
        for name in turn_off:
            try:
                manager.disable(name, reason='control_file')
                changes.append(f'{name}: desactivada')
            except Exception as error:  # noqa: BLE001 - una falla que no se apaga no detiene a las demás
                t.log_event(log, 'control_file_error', level=logging.ERROR, fault=name, error=repr(error))
        for name in turn_on:
            new = faults[name]
            params = {k: v for k, v in new.items() if k not in RESERVED}
            try:
                manager.enable(name, params, new.get('duration_s'))
            except FaultsDisarmed:
                # No se marca como aplicada: se reintenta en el próximo cambio del archivo.
                t.log_event(log, 'control_file_rejected', level=logging.WARNING, fault=name,
                            reason='kill switch accionado: rearme con POST /admin/arm')
                continue
            except Exception as error:  # noqa: BLE001 - se registra y se sigue con las demás
                t.log_event(log, 'control_file_error', level=logging.ERROR, fault=name, error=repr(error))
                continue
            changes.append(f'{name}: activada {params}')
        for name in names:  # el estado aplicado sigue al archivo, salvo las activaciones que fallaron
            if name in turn_on and f'{name}: activada' not in ' '.join(changes):
                continue
            if faults.get(name) is None:
                self.applied_faults.pop(name, None)
            else:
                self.applied_faults[name] = copy.deepcopy(faults[name])
        return changes

    @staticmethod
    def _wanted(spec):
        return bool(spec and spec.get('enabled'))

    def _apply_resilience(self, resilience):
        changes, patch = [], {}
        for name, values in resilience.items():
            old = self.applied_resilience.get(name)
            if old is None:  # primera lectura: solo se aplica lo que difiere de la configuración del baseline
                old = {k: RESILIENCE_DEFAULTS[name].get(k) for k in values}
            if values != old:
                patch[name] = values
                changes.append(f'{name}: {values}')
            self.applied_resilience[name] = copy.deepcopy(values)
        if patch:
            self.apply_resilience(patch)
        return changes


def from_env(engine, apply_resilience):
    path = os.getenv('FAULTS_FILE')
    if not path:
        return None
    return ControlFileWatcher(engine, apply_resilience, path, float(os.getenv('FAULTS_POLL_S', '1.0')))
