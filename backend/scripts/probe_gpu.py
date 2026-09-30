"""Stress existing ONNX models in disposable children, without importing the application.

Run from an isolated container with models mounted read-only. The supervisor never loads
OpenVINO, so a native abort or timeout becomes a failed report instead of killing it.
This cannot isolate a host-wide kernel GPU reset; keep production inference on CPU while
testing and inspect kernel errors before and after the experiment.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPORT_PREFIX = "GPU_PROBE_REPORT "
EVENT_PREFIX = "GPU_PROBE_EVENT "
MAX_LOG_BYTES = 1024 * 1024


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def parse_shape(value: str) -> tuple[str, tuple[int, ...]]:
    try:
        name, dimensions = value.split("=", 1)
        shape = tuple(int(part) for part in dimensions.split(","))
        if not name or not shape or any(dimension < 1 for dimension in shape):
            raise ValueError
        return name, shape
    except ValueError:
        raise argparse.ArgumentTypeError(
            "use INPUT_NAME=1,3,384,384 with positive dimensions"
        ) from None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--model",
        action="append",
        type=Path,
        default=[],
        help="model path or quoted glob; repeatable",
    )
    result.add_argument(
        "--model-dir",
        action="append",
        type=Path,
        default=[],
        help="recursively select *.onnx models in this directory",
    )
    result.add_argument("--device", default="GPU", help="exact OpenVINO device; no CPU fallback")
    result.add_argument("--iterations", type=positive_int, default=100)
    result.add_argument("--warmup", type=int, default=3)
    result.add_argument(
        "--timeout", type=positive_int, default=180, help="hard timeout per model, seconds"
    )
    result.add_argument("--max-input-mib", type=positive_int, default=256)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument(
        "--fixed-inputs",
        action="store_true",
        help="reuse inputs instead of deterministic varying values",
    )
    result.add_argument("--shape", action="append", type=parse_shape, default=[])
    result.add_argument(
        "--static-shapes",
        action="store_true",
        help="reshape graph to resolved inputs before compile",
    )
    result.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return result


def emit(prefix: str, data: dict) -> None:
    print(prefix + json.dumps(data, allow_nan=False), flush=True)


def _input_shape(
    port, overrides: dict[str, tuple[int, ...]], index: int
) -> tuple[str, tuple[int, ...]]:
    names = port.get_names()
    name = sorted(names)[0] if names else f"input_{index}"
    override = next((overrides[item] for item in names if item in overrides), overrides.get(name))
    partial = port.get_partial_shape()
    if override is not None:
        if not partial.compatible(type(partial)(override)):
            raise ValueError(f"Shape override for {name} is incompatible with {partial}")
        return name, override
    if partial.rank.is_dynamic:
        raise ValueError(f"Input {name} has dynamic rank; supply --shape {name}=...")
    dimensions = []
    for dimension_index, dimension in enumerate(partial):
        if dimension.is_static:
            dimensions.append(dimension.get_length())
        elif dimension_index == 0 and dimension.compatible(type(dimension)(1)):
            dimensions.append(1)
        else:
            raise ValueError(
                f"Input {name} has dynamic non-batch dimensions; supply --shape {name}=..."
            )
    if any(value < 1 for value in dimensions):
        raise ValueError(f"Input {name} has an empty dimension")
    return name, tuple(dimensions)


def child_probe(args) -> dict:
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Deliberately imported only in the child process.
    import numpy as np
    import openvino as ov

    model_path = args.model[0].resolve(strict=True)
    overrides = dict(args.shape)
    rng = np.random.default_rng(args.seed)
    core = ov.Core()
    emit(EVENT_PREFIX, {"phase": "read_model", "model": model_path.name})
    model = core.read_model(str(model_path))
    tensors = {}
    input_report = []
    input_bytes = 0
    shape_map = {}
    known_names = set()
    for index, port in enumerate(model.inputs):
        name, shape = _input_shape(port, overrides, index)
        known_names.update(port.get_names())
        known_names.add(name)
        dtype = np.dtype(port.get_element_type().to_dtype())
        size = math.prod(shape) * dtype.itemsize
        input_bytes += size
        if input_bytes > args.max_input_mib * 1024**2:
            raise ValueError("Resolved model inputs exceed --max-input-mib")
        if np.issubdtype(dtype, np.floating):
            source_dtype = np.float64 if dtype == np.float64 else np.float32
            values = rng.random(size=shape, dtype=source_dtype).astype(dtype, copy=False)
        elif np.issubdtype(dtype, np.integer):
            values = rng.integers(0, 16, size=shape, dtype=dtype)
        elif np.issubdtype(dtype, np.bool_):
            values = np.zeros(shape, dtype=dtype)
        else:
            raise ValueError(f"Unsupported probe input dtype {dtype} for {name}")
        tensors[index] = values
        shape_map[index] = shape
        input_report.append(
            {"name": name, "shape": list(shape), "dtype": str(dtype), "bytes": size}
        )
    if unknown := set(overrides) - known_names:
        raise ValueError(f"Unknown --shape inputs: {', '.join(sorted(unknown))}")
    if args.static_shapes:
        model.reshape(shape_map)

    with tempfile.TemporaryDirectory(prefix="dashcam-gpu-probe-") as cache:
        config = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1", "CACHE_DIR": cache}
        if args.device.upper().startswith("CPU"):
            config["INFERENCE_NUM_THREADS"] = "2"
        emit(EVENT_PREFIX, {"phase": "compile", "device": args.device, "inputs": input_report})
        started = time.monotonic()
        compiled = core.compile_model(model, args.device, config)
        compile_seconds = time.monotonic() - started
        execution_devices = list(compiled.get_property("EXECUTION_DEVICES"))
        properties = {}
        for key in ("FULL_DEVICE_NAME", "DRIVER_VERSION"):
            try:
                properties[key.lower()] = str(core.get_property(args.device, key))
            except Exception:
                pass
        request = compiled.create_infer_request()
        durations = []
        output_report = []
        for index in range(args.warmup + args.iterations):
            if not args.fixed_inputs:
                # Fill outside the inference timer; shape/allocation stays bounded while
                # content-dependent paths such as NMS see different values each time.
                for values in tensors.values():
                    if np.issubdtype(values.dtype, np.floating):
                        source_dtype = np.float64 if values.dtype == np.float64 else np.float32
                        values[...] = rng.random(size=values.shape, dtype=source_dtype)
                    elif np.issubdtype(values.dtype, np.integer):
                        values[...] = rng.integers(0, 16, size=values.shape, dtype=values.dtype)
                    else:
                        values[...] = rng.integers(0, 2, size=values.shape, dtype=np.uint8)
            started = time.monotonic()
            outputs = request.infer(tensors)
            duration = time.monotonic() - started
            output_report = []
            for port, output in outputs.items():
                array = np.asarray(output)
                if np.issubdtype(array.dtype, np.number) and not np.isfinite(array).all():
                    raise ValueError(f"Non-finite output at iteration {index + 1}")
                try:
                    name = port.get_any_name()
                except Exception:
                    name = f"output_{len(output_report)}"
                output_report.append(
                    {
                        "name": name,
                        "shape": list(array.shape),
                        "dtype": str(array.dtype),
                        "finite": True,
                    }
                )
            if index >= args.warmup:
                durations.append(duration)
            if index == 0 or (index + 1) % 25 == 0:
                emit(
                    EVENT_PREFIX,
                    {
                        "phase": "infer",
                        "completed": index + 1,
                        "total": args.warmup + args.iterations,
                    },
                )
        ordered = sorted(durations)
        return {
            "status": "passed",
            "model": model_path.name,
            "openvino_version": ov.__version__,
            "device": args.device,
            "execution_devices": execution_devices,
            "device_properties": properties,
            "iterations": args.iterations,
            "warmup": args.warmup,
            "static_shapes": args.static_shapes,
            "varying_inputs": not args.fixed_inputs,
            "input_bytes": input_bytes,
            "inputs": input_report,
            "outputs": output_report,
            "compile_seconds": round(compile_seconds, 6),
            "inference_seconds": {
                "min": min(durations),
                "median": ordered[len(ordered) // 2],
                "max": max(durations),
                "total": sum(durations),
            },
        }


def _read_tail(pipe, chunks: collections.deque, forward_events: bool = False) -> None:
    pending = b""
    try:
        while chunk := pipe.read1(4096):
            chunks.append(chunk)
            if forward_events:
                pending = (pending + chunk)[-MAX_LOG_BYTES:]
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    if line.startswith(EVENT_PREFIX.encode()):
                        print(line.decode("utf-8", errors="replace"), flush=True)
    finally:
        pipe.close()


def _kill_child(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # A kernel driver hang may leave an uninterruptible task. The supervisor must
        # still return; the surrounding disposable container owns final cleanup.
        pass


def supervise(command: list[str], timeout: float) -> dict:
    """Bound native execution and diagnostic capture without importing its runtime."""
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    stdout = collections.deque(maxlen=MAX_LOG_BYTES // 4096)
    stderr = collections.deque(maxlen=MAX_LOG_BYTES // 4096)
    readers = [
        threading.Thread(
            target=_read_tail, args=(pipe, target, pipe is process.stdout), daemon=True
        )
        for pipe, target in ((process.stdout, stdout), (process.stderr, stderr))
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_child(process)
    except BaseException:
        _kill_child(process)
        raise
    for reader in readers:
        reader.join(timeout=5)
    out = b"".join(stdout).decode("utf-8", errors="replace")
    err = b"".join(stderr).decode("utf-8", errors="replace")
    report = None
    for line in out.splitlines():
        if line.startswith(REPORT_PREFIX):
            try:
                report = json.loads(line[len(REPORT_PREFIX) :])
            except ValueError:
                pass
    if not isinstance(report, dict):
        report = {"status": "failed", "error": "Child exited without a valid result"}
    if timed_out:
        report.update(status="timeout", error=f"Child exceeded {timeout} seconds")
    elif process.returncode:
        report["status"] = "failed"
        if process.returncode < 0:
            try:
                report["signal"] = signal.Signals(-process.returncode).name
            except ValueError:
                report["signal"] = str(process.returncode)
    if process.returncode is None:
        report["unreaped_child_pid"] = process.pid
    report.update(returncode=process.returncode, wall_seconds=round(time.monotonic() - started, 6))
    report["events"] = [
        line[len(EVENT_PREFIX) :] for line in out.splitlines() if line.startswith(EVENT_PREFIX)
    ]
    if err:
        report["stderr_tail"] = err[-12000:]
    return report


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not re.fullmatch(r"(?:GPU|CPU|NPU)(?:\.\d+)?", args.device):
        parser().error(
            "--device must name an exact GPU, CPU or NPU device; AUTO/HETERO fallback is not allowed"
        )
    if args.warmup < 0:
        parser().error("--warmup must be non-negative")
    if args.child:
        if len(args.model) != 1:
            parser().error("child requires one --model")
        try:
            result = child_probe(args)
        except Exception as exc:
            emit(REPORT_PREFIX, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            return 1
        emit(REPORT_PREFIX, result)
        return 0
    if not args.model and not args.model_dir:
        parser().error("select --model or --model-dir")
    models = []
    for directory in args.model_dir:
        models.extend(sorted(directory.rglob("*.onnx")))
    for model in args.model:
        if glob.has_magic(str(model)):
            models.extend(Path(path) for path in sorted(glob.glob(str(model), recursive=True)))
        elif model.is_dir():
            models.extend(sorted(model.rglob("*.onnx")))
        else:
            models.append(model)
    if not models:
        parser().error("model selection matched no files")
    reports = []
    models = list(dict.fromkeys(models))
    for model_index, model in enumerate(models):
        if not model.is_file():
            reports.append(
                {"status": "failed", "model": str(model), "error": "Model file does not exist"}
            )
            continue
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--model",
            str(model.resolve()),
            "--device",
            args.device,
            "--iterations",
            str(args.iterations),
            "--warmup",
            str(args.warmup),
            "--seed",
            str(args.seed),
            "--max-input-mib",
            str(args.max_input_mib),
        ]
        if args.static_shapes:
            command.append("--static-shapes")
        if args.fixed_inputs:
            command.append("--fixed-inputs")
        for name, shape in args.shape:
            command.extend(("--shape", name + "=" + ",".join(map(str, shape))))
        emit(EVENT_PREFIX, {"phase": "starting_child", "model": model.name})
        result = supervise(command, args.timeout)
        result.setdefault("model", model.name)
        reports.append(result)
        emit(REPORT_PREFIX, result)
        if (
            result["status"] == "timeout"
            or result.get("signal")
            or result.get("unreaped_child_pid")
            or (args.device.startswith("GPU") and result["status"] != "passed")
        ):
            for remaining in models[model_index + 1 :]:
                skipped = {
                    "status": "skipped",
                    "model": remaining.name,
                    "error": "A previous GPU probe failure or native timeout stopped probing",
                }
                reports.append(skipped)
                emit(REPORT_PREFIX, skipped)
            break
    return 0 if reports and all(report["status"] == "passed" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
