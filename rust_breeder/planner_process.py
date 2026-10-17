"""Run CPU-bound route search outside the Qt interpreter.

The child reads one bounded JSON request and writes newline-delimited JSON.
There is no project path in the protocol: it cannot read or write user stock.
Only this parent function owns the process and always waits for its exit.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from time import monotonic

from .models import InventoryItem, Prediction, Recipe, SlotPrediction, Target
from .routes import RouteReport, RouteStep, plan_route


REQUEST_LIMIT = 4 * 1024 * 1024
MESSAGE_LIMIT = 4 * 1024 * 1024
OPTION_NAMES = frozenset({
    'center_id', 'max_rounds', 'clone_yield', 'max_donors',
    'max_combinations', 'beam_width', 'donor_pool_limit', 'max_seconds', 'objective',
})
PARALLEL_OPTION_NAMES = frozenset({
    'box_count', 'main_box', 'max_waves', 'clone_yield', 'max_donors',
    'max_combinations', 'max_seconds', 'catalog_limit', 'donor_pool_limit', 'objective',
})


def _report_from_dict(data):
    def recipe(value):
        prediction = value['prediction']
        prediction = Prediction(
            prediction['center_genes'], tuple(prediction['donors']),
            tuple(SlotPrediction(**{**slot, 'candidates': tuple(slot['candidates'])})
                  for slot in prediction['slots']),
        )
        return Recipe(InventoryItem(**value['center']),
                      tuple(InventoryItem(**row) for row in value['donors']),
                      prediction, Target(**value['target']))

    values = dict(data)
    values['steps'] = tuple(RouteStep(recipe(step['recipe']), step['expected_genes'],
                                      step['output_id'], step['clone_yield'])
                            for step in values['steps'])
    values['recipes'] = tuple(recipe(value) for value in values['recipes'])
    values['limits'] = tuple(values['limits'])
    return RouteReport(**values)


def _worker_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--route-worker']
    return [sys.executable, str(Path(__file__).resolve().parents[1] / 'main.py'), '--route-worker']


def _cancelled(options, evaluated=0, elapsed=0.0):
    return RouteReport((), (), False, False, evaluated, 'cancelled',
                       requested_max_rounds=options.get('max_rounds', 3),
                       clone_yield=options.get('clone_yield', 1),
                       elapsed_seconds=elapsed, objective=options.get('objective', 'plants'),
                       center_mode='fixed' if options.get('center_id') else 'auto')


def plan_route_process(inventory, target, *, cancel=None, progress=None, **options):
    """A cancellable ``plan_route`` replacement with no Qt or GIL work in UI."""
    return _plan_process(inventory, target, cancel=cancel, progress=progress, job='route', **options)


def plan_parallel_process(inventory, target, *, cancel=None, progress=None, **options):
    """Run shared-stock DAG planning in the same isolated worker protocol."""
    return _plan_process(inventory, target, cancel=cancel, progress=progress, job='parallel', **options)


def _plan_process(inventory, target, *, cancel=None, progress=None, job='route', **options):
    allowed = PARALLEL_OPTION_NAMES if job == 'parallel' else OPTION_NAMES
    if set(options) - allowed:
        raise ValueError('未知的路线计算参数。')

    def cancelled(evaluated=0, elapsed=0.0):
        if job == 'parallel':
            from .parallel_models import ParallelReport
            return ParallelReport((), False, False, evaluated, 'cancelled',
                                  options.get('box_count', 3), options.get('main_box', 1),
                                  options.get('max_waves', 3), options.get('clone_yield', 1), elapsed,
                                  objective=options.get('objective', 'rounds'))
        return _cancelled(options, evaluated, elapsed)

    if cancel and cancel():
        return cancelled()
    request = json.dumps({
        'inventory': [asdict(row) for row in inventory], 'target': asdict(target),
        'options': options, **({'job': 'parallel'} if job == 'parallel' else {}),
    }, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(request) > REQUEST_LIMIT:
        raise ValueError('种子库过大，无法开始计算。')
    started = monotonic()
    evaluated = 0
    process = subprocess.Popen(
        _worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    messages = Queue()

    def read_messages():
        try:
            while True:
                line = process.stdout.readline(MESSAGE_LIMIT + 1)
                if not line:
                    break
                if len(line) > MESSAGE_LIMIT:
                    raise RuntimeError('计算进程返回的数据过大。')
                messages.put(json.loads(line))
        except Exception as error:
            messages.put({'type': 'error', 'message': str(error)})
        finally:
            messages.put({'type': 'eof'})

    reader = Thread(target=read_messages, name='route-result-reader', daemon=True)
    reader.start()
    completed = False
    try:
        process.stdin.write(request + b'\n')
        process.stdin.close()
        deadline = started + max(15.0, float(options.get('max_seconds', 8)) + 10.0)
        while True:
            if cancel and cancel():
                return cancelled(evaluated, monotonic() - started)
            if monotonic() >= deadline:
                raise RuntimeError('计算时间过长，已停止本次搜索；种子库未更改。')
            try:
                message = messages.get(timeout=.05)
            except Empty:
                continue
            kind = message.get('type')
            if kind == 'progress':
                evaluated = int(message['evaluated'])
                if progress:
                    progress(evaluated, int(message['round']))
            elif kind == 'result':
                if job == 'parallel':
                    from .parallel_models import ParallelReport
                    report = ParallelReport.from_dict(message['report'])
                else:
                    report = _report_from_dict(message['report'])
                completed = True
                return report
            elif kind == 'error':
                raise RuntimeError(message.get('message') or '路线计算失败。')
            elif kind == 'eof':
                raise RuntimeError('计算进程意外结束；种子库未更改，请重试。')
    finally:
        if completed:
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        else:
            process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        reader.join(timeout=1.0)
        process.stdout.close()


def _standard_stream(name, mode):
    stream = getattr(sys, name)
    if stream is not None:
        return stream
    # A PyInstaller windowed application sets sys.stdin/stdout to None, even
    # when started with redirected Windows handles. Reopen only those handles;
    # do not allocate a console or import the desktop application in the child.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        get_handle = ctypes.WinDLL('kernel32', use_last_error=True).GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        handle = get_handle(-10 if name == 'stdin' else -11)
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise RuntimeError('计算进程缺少通信管道。')
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == 'r' else os.O_WRONLY)
        return os.fdopen(descriptor, mode, encoding='utf-8', newline='\n')
    return open(0 if name == 'stdin' else 1, mode, encoding='utf-8', closefd=False)


def worker_main():
    """Entry point selected by ``main.py --route-worker`` before any Qt import."""
    source = _standard_stream('stdin', 'r')
    destination = _standard_stream('stdout', 'w')

    def emit(kind, **values):
        destination.write(json.dumps({'type': kind, **values}, ensure_ascii=True,
                                     separators=(',', ':')) + '\n')
        destination.flush()

    try:
        raw = source.readline(REQUEST_LIMIT + 1)
        if len(raw.encode('utf-8')) > REQUEST_LIMIT:
            raise ValueError('种子库请求过大。')
        data = json.loads(raw)
        if set(data) not in ({'inventory', 'target', 'options'}, {'inventory', 'target', 'options', 'job'}):
            raise ValueError('无效的路线计算请求。')
        job = data.get('job', 'route')
        if job not in ('route', 'parallel'):
            raise ValueError('未知的计算任务。')
        options = data['options']
        allowed = PARALLEL_OPTION_NAMES if job == 'parallel' else OPTION_NAMES
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError('未知的路线计算参数。')
        inventory = tuple(InventoryItem(**row) for row in data['inventory'])
        target = Target(**data['target'])
        last_progress = -1.0

        def report_progress(evaluated, depth):
            nonlocal last_progress
            now = monotonic()
            if now - last_progress >= .08:
                emit('progress', evaluated=evaluated, round=depth)
                last_progress = now

        if job == 'parallel':
            from .parallel import plan_parallel
            report = plan_parallel(inventory, target, **options, progress=report_progress)
            emit('result', report=report.to_dict())
        else:
            report = plan_route(inventory, target, **options, progress=report_progress)
            emit('result', report=asdict(report))
        return 0
    except Exception as error:
        emit('error', message=str(error))
        return 1
